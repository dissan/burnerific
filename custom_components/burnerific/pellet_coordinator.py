"""Pellet stock, learning and forecasting, wired into Home Assistant."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from functools import partial
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTemperature
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import TemperatureConverter

from . import open_meteo
from .const import (
    CLIMATE_MAX_AGE_DAYS,
    CONF_ANNUAL_KG,
    CONF_BAG_KG,
    CONF_INITIAL_STORAGE,
    CONF_SEASON_END,
    CONF_SILO_CAPACITY,
    CONF_SILO_TRACKING,
    CONF_TEMPERATURE_ENTITY,
    CONF_USE_OPEN_METEO,
    CONF_WEATHER_ENTITY,
    DEFAULT_ANNUAL_KG,
    DEFAULT_BAG_KG,
    DOMAIN,
    FORECAST_INTERVAL_MIN,
    SILO_BOILER,
    STORAGE_VERSION,
    WEATHER_MAX_AGE_MIN,
)
from .coordinator import BoilerCoordinator
from .nbe import BoilerData
from .pellets import consumption, forecast
from .pellets.climate import ClimateModel
from .pellets.consumption import ConsumptionModel, MonthObs, effective_temps
from .pellets.forecast import ForecastInput, ForecastResult, default_season_end
from .pellets.history import History
from .pellets.inventory import Inventory, StockError

_LOGGER = logging.getLogger(__name__)

KEEP_RECENT_TEMPS = 430  # days of measured temperatures kept for monthly backfill


@dataclass(slots=True)
class PelletState:
    inventory: Inventory
    model: ConsumptionModel | None
    forecast: ForecastResult | None
    climate: ClimateModel
    burned_today: float | None
    computed_at: datetime | None


class PelletCoordinator(DataUpdateCoordinator[PelletState]):
    """Owns the stock ledger and the forecast. Recomputes every 30 minutes and on changes."""

    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, boiler: BoilerCoordinator) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} pellets",
            update_interval=timedelta(minutes=FORECAST_INTERVAL_MIN),
        )
        self.boiler = boiler
        self._store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}")
        self.inventory = Inventory()
        self.history = History.empty()
        self.climate = ClimateModel.default(hass.config.latitude)
        self.climate_at: datetime | None = None
        self.measured: dict[str, float] = {}  # date -> daily mean °C from Open-Meteo (past days)
        self.om_forecast: dict[date, float] = {}
        self.om_at: datetime | None = None
        self.model: ConsumptionModel | None = None
        self.result: ForecastResult | None = None
        self.computed_at: datetime | None = None
        self._last_counter: float | None = None
        self.fill_amount_kg = 0.0  # set by the "Fill amount" number; 0 = whole bags that fit
        self._unsub_boiler: CALLBACK_TYPE | None = None

    # -- lifecycle -----------------------------------------------------------

    async def async_load(self) -> None:
        stored = await self._store.async_load() or {}
        self.inventory = Inventory.from_dict(stored.get("inventory"))
        self.history = History.from_dict(stored.get("history"))
        if climate := ClimateModel.from_dict(stored.get("climate")):
            self.climate = climate
        if at := stored.get("climate_at"):
            self.climate_at = dt_util.parse_datetime(at)
        self.measured = stored.get("measured", {})
        self._last_counter = stored.get("last_counter")
        if not stored and (initial := self.config_entry.data.get(CONF_INITIAL_STORAGE)):
            self.inventory.set_storage(float(initial), dt_util.now(), note="setup")
        self._apply_options()
        self._unsub_boiler = self.boiler.async_add_listener(self._on_boiler_update)
        # The boiler's first refresh already happened; take its reading now instead of a poll later.
        self._on_boiler_update()

    async def async_unload(self) -> None:
        if self._unsub_boiler:
            self._unsub_boiler()
        await self._store.async_save(self._serialize())

    @callback
    def _serialize(self) -> dict[str, Any]:
        return {
            "inventory": self.inventory.to_dict(),
            "history": self.history.to_dict(),
            "climate": self.climate.to_dict(),
            "climate_at": self.climate_at.isoformat() if self.climate_at else None,
            "measured": self.measured,
            "last_counter": self._last_counter,
        }

    def _save_soon(self) -> None:
        self._store.async_delay_save(self._serialize, 30)

    def _apply_options(self) -> None:
        opts = self.config_entry.options
        self.inventory.bag_kg = float(opts.get(CONF_BAG_KG, DEFAULT_BAG_KG))
        cap = opts.get(CONF_SILO_CAPACITY)
        self.inventory.silo_capacity_kg = float(cap) if cap else None

    @property
    def tracks_silo_on_boiler(self) -> bool:
        data = self.boiler.data
        mode = self.config_entry.options.get(CONF_SILO_TRACKING, SILO_BOILER)
        return mode == SILO_BOILER and data is not None and data.hopper_kg is not None

    # -- live feed from the boiler -------------------------------------------

    @callback
    def _on_boiler_update(self) -> None:
        data = self.boiler.data
        if data is None or not self.boiler.last_update_success:
            return
        now = dt_util.now()
        closed = self.history.observe(now, data.total_kg, self._outdoor_temp(data), data.kg_on)

        if self.tracks_silo_on_boiler:
            self.inventory.silo_kg = data.hopper_kg
        elif data.total_kg is not None and self._last_counter is not None:
            burned = data.total_kg - self._last_counter
            if 0 < burned < 200:  # ignore counter resets and absurd jumps
                self.inventory.consume(burned)
        if data.total_kg is not None:
            self._last_counter = data.total_kg

        self._save_soon()
        self._push()
        if closed:
            self.hass.async_create_task(self.async_request_refresh())

    def _outdoor_temp(self, data: BoilerData | None) -> float | None:
        opts = self.config_entry.options
        entity_id = opts.get(CONF_TEMPERATURE_ENTITY)
        if entity_id and (t := self._state_temp(entity_id, None)) is not None:
            return t
        if data is not None and data.outdoor_temp is not None:
            return data.outdoor_temp
        if entity_id := opts.get(CONF_WEATHER_ENTITY):
            return self._state_temp(entity_id, "temperature")
        return None

    def _state_temp(self, entity_id: str, attribute: str | None) -> float | None:
        state = self.hass.states.get(entity_id)
        if state is None:
            return None
        raw = state.state if attribute is None else state.attributes.get(attribute)
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return None
        unit = state.attributes.get("unit_of_measurement" if attribute is None else "temperature_unit")
        if unit == UnitOfTemperature.FAHRENHEIT:
            value = TemperatureConverter.convert(value, UnitOfTemperature.FAHRENHEIT, UnitOfTemperature.CELSIUS)
        return value

    # -- stock operations ----------------------------------------------------

    def _kg(self, kg: float | None, bags: float | None) -> float:
        if kg is None and bags is None:
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="amount_required")
        return (kg or 0.0) + (bags or 0.0) * self.inventory.bag_kg

    async def async_add_delivery(
        self,
        kg: float | None,
        bags: float | None,
        price_per_kg: float | None,
        total_price: float | None,
        note: str | None,
    ) -> None:
        amount = self._kg(kg, bags)
        if price_per_kg is None and total_price is not None and amount > 0:
            price_per_kg = total_price / amount
        try:
            self.inventory.deliver(amount, dt_util.now(), price_per_kg=price_per_kg, note=note)
        except StockError as err:
            raise ServiceValidationError(str(err)) from err
        await self._changed()

    async def async_fill_silo(self, kg: float | None, bags: float | None, from_storage: bool, note: str | None) -> None:
        amount = self._kg(kg, bags)
        if from_storage and amount > self.inventory.storage_kg + 1e-6:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="not_enough_storage",
                translation_placeholders={"storage": f"{self.inventory.storage_kg:.0f}", "kg": f"{amount:.0f}"},
            )
        silo_after: float | None = None
        if self.tracks_silo_on_boiler:
            # The controller counts the silo itself; tell it about the new pellets. Its refresh
            # then mirrors the new total, so the ledger must not add the amount a second time.
            current = self.boiler.data.hopper_kg if self.boiler.data else None
            silo_after = float(round((current or 0.0) + amount))
            if self.boiler.can_write:
                await self.boiler.async_write("hopper", "content", round(silo_after))
            else:
                _LOGGER.info("No PIN configured: remember to set the silo content on the controller to %.0f kg", silo_after)
        try:
            self.inventory.fill_silo(amount, dt_util.now(), from_storage=from_storage, silo_after=silo_after, note=note)
        except StockError as err:
            raise ServiceValidationError(str(err)) from err
        await self._changed()

    async def async_set_storage(self, kg: float | None, bags: float | None, note: str | None = None) -> None:
        amount = 0.0 if kg is None and bags is None else self._kg(kg, bags)
        try:
            self.inventory.set_storage(amount, dt_util.now(), note=note)
        except StockError as err:
            raise ServiceValidationError(str(err)) from err
        await self._changed()

    async def async_set_silo(self, kg: float, note: str | None = None) -> None:
        if self.tracks_silo_on_boiler:
            if not self.boiler.can_write:
                raise HomeAssistantError(translation_domain=DOMAIN, translation_key="pin_required")
            await self.boiler.async_write("hopper", "content", round(kg))
        try:
            self.inventory.set_silo(kg, dt_util.now(), note=note)
        except StockError as err:
            raise ServiceValidationError(str(err)) from err
        await self._changed()

    async def _changed(self) -> None:
        self._save_soon()
        self._push()
        await self.async_request_refresh()

    @callback
    def _push(self) -> None:
        """Show stock changes right away. Not async_set_updated_data: that would keep
        postponing the scheduled forecast refresh on every boiler poll."""
        if self.data is not None:
            self.data = self._state()
            self.async_update_listeners()

    def _state(self) -> PelletState:
        counter = self.boiler.data.total_kg if self.boiler.data else None
        burned = self.history.burned_today(counter)
        if burned is None and self.boiler.data is not None:
            burned = self.boiler.data.kg_on(dt_util.now().date())
        return PelletState(self.inventory, self.model, self.result, self.climate, burned, self.computed_at)

    # -- the heavy part ------------------------------------------------------

    async def _async_update_data(self) -> PelletState:
        self._apply_options()
        now = dt_util.now()
        today = now.date()
        await self._refresh_climate(now)
        await self._refresh_open_meteo(now)
        temp_fc = await self._weather_entity_forecast(today)

        self._backfill(today)
        days = self.history.observations()
        months = self._months(today)
        annual = float(self.config_entry.options.get(CONF_ANNUAL_KG, DEFAULT_ANNUAL_KG))
        self.model = await self.hass.async_add_executor_job(
            partial(consumption.fit, days, self.climate, today=today, prior_annual_kg=annual, months=months)
        )

        # Forecast temperatures: Open-Meteo, overridden by the user's weather entity where it has days.
        merged = {d: t for d, t in self.om_forecast.items() if d >= today} | temp_fc
        temps = self.history.temps()
        teff = effective_temps([(d, t) for d, t in temps if d < today])
        yesterday = today - timedelta(days=1)
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        state = self._state()
        inp = ForecastInput(
            today=today,
            day_fraction_left=1.0 - (now - midnight).total_seconds() / 86400.0,
            stock_kg=self.inventory.total_kg,
            silo_kg=self.inventory.silo_kg,
            temp_forecast=merged,
            last_temp_eff=teff.get(yesterday),
            burned_today_kg=state.burned_today or 0.0,
            season_end=self._season_end(today),
            seed=today.toordinal(),
        )
        self.result = await self.hass.async_add_executor_job(forecast.run, inp, self.model, self.climate)
        self.computed_at = now
        self._save_soon()
        return self._state()

    def _season_end(self, today: date) -> date:
        text = self.config_entry.options.get(CONF_SEASON_END)
        if text:
            try:
                month, day = (int(x) for x in str(text).split("-")[-2:])
                end = date(today.year, month, day)
                return end if end > today else date(today.year + 1, month, day)
            except ValueError:
                _LOGGER.warning("Ignoring season end %r, expected MM-DD", text)
        return default_season_end(today, self.hass.config.latitude)

    async def _refresh_climate(self, now: datetime) -> None:
        if not self.config_entry.options.get(CONF_USE_OPEN_METEO, True):
            return
        if self.climate_at and now - self.climate_at < timedelta(days=CLIMATE_MAX_AGE_DAYS):
            return
        session = async_get_clientsession(self.hass)
        try:
            history = await open_meteo.fetch_history(
                session, self.hass.config.latitude, self.hass.config.longitude, now.date()
            )
            self.climate = await self.hass.async_add_executor_job(ClimateModel.fit, history)
            self.climate_at = now
            cutoff = (now.date() - timedelta(days=KEEP_RECENT_TEMPS)).isoformat()
            self.measured |= {d.isoformat(): round(t, 2) for d, t in history if d.isoformat() >= cutoff}
            _LOGGER.debug("Climate fitted from %.1f years of history", self.climate.years)
        except (open_meteo.OpenMeteoError, ValueError) as err:
            _LOGGER.info("Using default climate, Open-Meteo history unavailable: %s", err)
            # Try again in a day rather than every refresh.
            self.climate_at = now - timedelta(days=CLIMATE_MAX_AGE_DAYS - 1)

    async def _refresh_open_meteo(self, now: datetime) -> None:
        if not self.config_entry.options.get(CONF_USE_OPEN_METEO, True):
            return
        if self.om_at and now - self.om_at < timedelta(minutes=WEATHER_MAX_AGE_MIN):
            return
        session = async_get_clientsession(self.hass)
        try:
            daily = await open_meteo.fetch_recent_and_forecast(
                session, self.hass.config.latitude, self.hass.config.longitude
            )
        except open_meteo.OpenMeteoError as err:
            _LOGGER.debug("Open-Meteo forecast unavailable: %s", err)
            self.om_at = now - timedelta(minutes=WEATHER_MAX_AGE_MIN - 30)
            return
        today = now.date()
        self.om_forecast = {d: t for d, t in daily if d >= today}
        self.measured |= {d.isoformat(): round(t, 2) for d, t in daily if d < today}
        cutoff = (today - timedelta(days=KEEP_RECENT_TEMPS)).isoformat()
        self.measured = {k: v for k, v in self.measured.items() if k >= cutoff}
        self.om_at = now

    async def _weather_entity_forecast(self, today: date) -> dict[date, float]:
        entity_id = self.config_entry.options.get(CONF_WEATHER_ENTITY)
        if not entity_id or self.hass.states.get(entity_id) is None:
            return {}
        try:
            resp = await self.hass.services.async_call(
                "weather",
                "get_forecasts",
                {"type": "daily"},
                target={"entity_id": entity_id},
                blocking=True,
                return_response=True,
            )
        except HomeAssistantError as err:
            _LOGGER.debug("No daily forecast from %s: %s", entity_id, err)
            return {}
        state = self.hass.states.get(entity_id)
        unit = state.attributes.get("temperature_unit") if state else None
        out: dict[date, float] = {}
        for item in (resp or {}).get(entity_id, {}).get("forecast", []):
            stamp = dt_util.parse_datetime(str(item.get("datetime", "")))
            high = item.get("temperature")
            low = item.get("templow")
            if stamp is None or high is None:
                continue
            # Daily mean from high/low. Without a low, nights run ~4 °C under the high's average.
            mean = (float(high) + float(low)) / 2.0 if low is not None else float(high) - 4.0
            if unit == UnitOfTemperature.FAHRENHEIT:
                mean = TemperatureConverter.convert(mean, UnitOfTemperature.FAHRENHEIT, UnitOfTemperature.CELSIUS)
            d = dt_util.as_local(stamp).date()
            if d >= today:
                out[d] = mean
        return out

    def _backfill(self, today: date) -> None:
        """Teach the model from the controller's own day array and measured temperatures."""
        data = self.boiler.data
        if data is not None:
            for back in range(1, 32):
                day = today - timedelta(days=back)
                kg = data.kg_on(day)
                if kg is not None:
                    self.history.backfill(day, kg, self.measured.get(day.isoformat()), "controller")
        for key, rec in self.history.days.items():
            if rec.temp is None and key in self.measured:
                rec.temp_override = self.measured[key]

    def _months(self, today: date) -> list[MonthObs]:
        """Closed months from the controller that the daily history does not already cover."""
        data = self.boiler.data
        if data is None:
            return []
        covered: dict[tuple[int, int], int] = {}
        for obs in self.history.observations():
            key = (obs.day.year, obs.day.month)
            covered[key] = covered.get(key, 0) + 1
        out: list[MonthObs] = []
        first = today.replace(day=1)
        for _ in range(11):
            end = first - timedelta(days=1)
            first = end.replace(day=1)
            kg = data.kg_in_month(first.year, first.month)
            if kg is None or covered.get((first.year, first.month), 0) >= 5:
                continue
            days_in = [first + timedelta(days=i) for i in range(end.day)]
            temps = [self.measured.get(d.isoformat()) for d in days_in]
            measured = tuple(t for t in temps if t is not None) if all(t is not None for t in temps) else None
            out.append(MonthObs(first.year, first.month, kg, end.day, measured))
        return out
