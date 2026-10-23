"""Pellet stock, learning, forecasting, maintenance and money, wired into Home Assistant."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from functools import partial
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTemperature
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import TemperatureConverter

from . import open_meteo
from .const import (
    CLIMATE_MAX_AGE_DAYS,
    CONF_ANNUAL_KG,
    CONF_APPLY_CALIBRATION,
    CONF_ASH_CAPACITY,
    CONF_ASH_PCT,
    CONF_BAG_KG,
    CONF_BOILER_EFFICIENCY,
    CONF_CLEAN_INTERVAL_KG,
    CONF_ELECTRICITY_PRICE,
    CONF_HEAT_PUMP_COP,
    CONF_INITIAL_STORAGE,
    CONF_KWH_PER_KG,
    CONF_ORDER_LEAD_DAYS,
    CONF_RESTOCK_DATE,
    CONF_SEASON_END,
    CONF_SERVICE_INTERVAL_DAYS,
    CONF_SERVICE_INTERVAL_KG,
    CONF_SILO_CAPACITY,
    CONF_SILO_SENSOR,
    CONF_SILO_SENSOR_EMPTY,
    CONF_SILO_SENSOR_FULL,
    CONF_SILO_TRACKING,
    CONF_SPLIT_HOT_WATER,
    CONF_SWEEP_INTERVAL_DAYS,
    CONF_TEMPERATURE_ENTITY,
    CONF_USE_OPEN_METEO,
    CONF_WEATHER_ENTITY,
    DEFAULT_ANNUAL_KG,
    DEFAULT_ASH_CAPACITY,
    DEFAULT_ASH_PCT,
    DEFAULT_BAG_KG,
    DEFAULT_BOILER_EFFICIENCY,
    DEFAULT_CLEAN_INTERVAL_KG,
    DEFAULT_HEAT_PUMP_COP,
    DEFAULT_KWH_PER_KG,
    DEFAULT_ORDER_LEAD_DAYS,
    DEFAULT_SERVICE_INTERVAL_DAYS,
    DEFAULT_SERVICE_INTERVAL_KG,
    DEFAULT_SWEEP_INTERVAL_DAYS,
    DOMAIN,
    EVENT_ALARM,
    FORECAST_INTERVAL_MIN,
    SILO_BOILER,
    SILO_SENSOR,
    STORAGE_VERSION,
    TASK_ASH,
    TASK_CLEAN,
    TASK_SERVICE,
    TASK_SWEEP,
    TASKS,
    WEATHER_MAX_AGE_MIN,
)
from .coordinator import BoilerCoordinator
from .nbe import BoilerData
from .nbe.states import state_key
from .pellets import consumption, economy, fills, forecast, maintenance
from .pellets.climate import ClimateModel
from .pellets.consumption import ConsumptionModel, DayObs, PeriodObs, Weather, effective_temps
from .pellets.forecast import ForecastInput, ForecastResult, default_season_end
from .pellets.history import History
from .pellets.inventory import UNKNOWN_LOT, Inventory, StockError

_LOGGER = logging.getLogger(__name__)

KEEP_RECENT_WEATHER = 430  # days of measured weather kept for monthly backfill
IGNITION_ALARMS = {13, 20, 27, 44}
SILO_EMPTY_HINT_KG = 10.0


@dataclass(slots=True)
class Insights:
    """Everything derived in the heavy refresh besides the forecast itself."""

    expected_daily: list[float] = field(default_factory=list)  # expected kg/day from today, ~400 days
    lots: list[economy.LotQuality] = field(default_factory=list)
    seasons: list[economy.SeasonTrend] = field(default_factory=list)
    normal_year_kg: float | None = None
    calibration: fills.Calibration | None = None
    dhw_daily_kg: float | None = None
    dhw_share: float | None = None


@dataclass(slots=True)
class _Snapshot:
    days: dict
    current: str | None
    lots: dict[int, tuple[str | None, str | None]]
    fill_events: list[tuple[date, float, float]]
    fill_log: list[fills.Fill]
    silo_capacity: float | None
    month_kg: Any


@dataclass(slots=True)
class PelletState:
    inventory: Inventory
    model: ConsumptionModel | None
    forecast: ForecastResult | None
    climate: ClimateModel
    burned_today: float | None
    computed_at: datetime | None
    insights: Insights
    tasks: dict[str, maintenance.TaskStatus]
    restock: date
    order_by: date | None
    heat: economy.HeatPrice
    cost_today: float | None
    cost_month: float | None
    cost_season: float | None
    cost_next_30: float | None
    burned_total: float
    on_cloud: bool


def _weather_row(value: Any) -> list[float | None] | None:
    """Stored weather: ``[temp, wind, sun]``; older stores kept just the temperature."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return [float(value), None, None]
    return list(value) + [None] * (3 - len(value))


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
        self.measured: dict[str, list[float | None]] = {}  # date -> [temp, wind, sun] (Open-Meteo, past days)
        self.om_forecast: dict[date, list[float | None]] = {}
        self.om_at: datetime | None = None
        self.fill_log: list[fills.Fill] = []
        self.tasks: dict[str, maintenance.TaskState] = {}
        self.burned_total = 0.0
        self.flags: dict[str, bool] = {}
        self.model: ConsumptionModel | None = None
        self.result: ForecastResult | None = None
        self.insights = Insights()
        self.computed_at: datetime | None = None
        self._last_counter: float | None = None
        self._alarm_state: int | None = None
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
        self.measured = {k: r for k, v in stored.get("measured", {}).items() if (r := _weather_row(v))}
        self.fill_log = [fills.Fill(date.fromisoformat(d), kg) for d, kg in stored.get("fill_log", [])]
        self.tasks = maintenance.from_dict(stored.get("tasks"))
        self.burned_total = float(stored.get("burned_total", 0.0))
        self.flags: dict[str, bool] = stored.get("flags", {})  # one-time housekeeping already done
        self._last_counter = stored.get("last_counter")
        if not stored and (initial := self.config_entry.data.get(CONF_INITIAL_STORAGE)):
            self.inventory.set_storage(float(initial), dt_util.now(), note="setup")
        for key in TASKS:
            state = self.tasks.setdefault(key, maintenance.TaskState())
            if state.burned_at is None:
                state.burned_at = self.burned_total  # count from when Burnerific started watching
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
            "fill_log": [[f.day.isoformat(), f.kg] for f in self.fill_log],
            "tasks": maintenance.to_dict(self.tasks),
            "burned_total": self.burned_total,
            "flags": self.flags,
            "last_counter": self._last_counter,
        }

    def _save_soon(self) -> None:
        self._store.async_delay_save(self._serialize, 30)

    def _opt(self, key: str, default: Any = None) -> Any:
        return self.config_entry.options.get(key, default)

    def _apply_options(self) -> None:
        self.inventory.bag_kg = float(self._opt(CONF_BAG_KG, DEFAULT_BAG_KG))
        cap = self._opt(CONF_SILO_CAPACITY)
        self.inventory.silo_capacity_kg = float(cap) if cap else None

    @property
    def silo_mode(self) -> str:
        mode = self._opt(CONF_SILO_TRACKING, SILO_BOILER)
        if mode == SILO_SENSOR and self._opt(CONF_SILO_SENSOR):
            return SILO_SENSOR
        data = self.boiler.data
        if mode == SILO_BOILER and data is not None and data.hopper_kg is not None:
            return SILO_BOILER
        return "integration"

    @property
    def tracks_silo_on_boiler(self) -> bool:
        return self.silo_mode == SILO_BOILER

    @property
    def calibration_factor(self) -> float:
        cal = self.insights.calibration
        if cal is None or not self._opt(CONF_APPLY_CALIBRATION, False):
            return 1.0
        return cal.factor

    @property
    def _issue_id(self) -> str:
        return f"alarm_{self.config_entry.entry_id}"

    # -- live feed from the boiler -------------------------------------------

    @callback
    def _on_boiler_update(self) -> None:
        data = self.boiler.data
        if data is None or not self.boiler.last_update_success:
            return
        now = dt_util.now()
        closed = self.history.observe(now, data.total_kg, self._outdoor_temp(data), data.kg_on)
        for day in closed:
            rec = self.history.days.get(day.isoformat())
            if rec is not None:
                rec.dhw_kg = data.dhw_kg_on(day)
                lot = self.inventory.burning_lot
                rec.lot = lot.id if lot else UNKNOWN_LOT

        burned = 0.0
        if data.total_kg is not None and self._last_counter is not None:
            delta = data.total_kg - self._last_counter
            if 0 < delta < 200:  # ignore counter resets and absurd jumps
                burned = delta * self.calibration_factor
        if data.total_kg is not None:
            self._last_counter = data.total_kg
        self.burned_total += burned

        mode = self.silo_mode
        if mode == SILO_BOILER:
            self.inventory.mirror_silo(data.hopper_kg)  # type: ignore[arg-type]
        elif mode == SILO_SENSOR:
            if (kg := self._silo_from_sensor()) is not None:
                self.inventory.mirror_silo(kg)
        else:
            self.inventory.consume(burned)

        self._handle_alarm(data)
        self._save_soon()
        self._push()
        if closed:
            self.hass.async_create_task(self.async_request_refresh())

    def _silo_from_sensor(self) -> float | None:
        """Silo kg from a level sensor: a percentage, or a distance between 'empty' and 'full' readings."""
        cap = self.inventory.silo_capacity_kg
        state = self.hass.states.get(self._opt(CONF_SILO_SENSOR))
        if state is None or not cap:
            return None
        try:
            value = float(state.state)
        except ValueError:
            return None
        if state.attributes.get("unit_of_measurement") == "%":
            frac = value / 100.0
        else:
            empty, full = self._opt(CONF_SILO_SENSOR_EMPTY), self._opt(CONF_SILO_SENSOR_FULL)
            if empty is None or full is None or float(empty) == float(full):
                return None
            frac = (float(empty) - value) / (float(empty) - float(full))
        return round(min(1.0, max(0.0, frac)) * cap, 1)

    def _handle_alarm(self, data: BoilerData) -> None:
        """Controller alarms become a Repairs issue with a useful hint, and an event."""
        if not data.alarm:
            if self._alarm_state is not None:
                ir.async_delete_issue(self.hass, DOMAIN, self._issue_id)
                self._alarm_state = None
            return
        if self._alarm_state == data.state:
            return
        self._alarm_state = data.state
        silo = self.inventory.silo_kg
        if data.state in IGNITION_ALARMS and silo is not None and silo <= SILO_EMPTY_HINT_KG:
            hint = "out_of_pellets"
        elif data.state in IGNITION_ALARMS:
            hint = "ignition"
        elif data.state in (11, 12, 17):
            hint = "burner_hot"
        elif data.state == 29:
            hint = "overheat"
        else:
            hint = "generic"
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            self._issue_id,
            is_fixable=False,
            is_persistent=False,
            severity=ir.IssueSeverity.ERROR,
            translation_key=f"alarm_{hint}",
            translation_placeholders={
                "boiler": self.config_entry.title,
                "code": str(data.state),
                "silo": "?" if silo is None else f"{silo:.0f}",
            },
        )
        self.hass.bus.async_fire(
            EVENT_ALARM,
            {"entry_id": self.config_entry.entry_id, "boiler": self.config_entry.title, "state": data.state, "state_key": state_key(data.state), "hint": hint},
        )

    def _outdoor_temp(self, data: BoilerData | None) -> float | None:
        entity_id = self._opt(CONF_TEMPERATURE_ENTITY)
        if entity_id and (t := self._state_temp(entity_id, None)) is not None:
            return t
        if data is not None and data.outdoor_temp is not None:
            return data.outdoor_temp
        if entity_id := self._opt(CONF_WEATHER_ENTITY):
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

    def _state_number(self, entity_id: str | None) -> float | None:
        state = self.hass.states.get(entity_id) if entity_id else None
        try:
            return float(state.state) if state else None
        except ValueError:
            return None

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
        supplier: str | None = None,
    ) -> None:
        amount = self._kg(kg, bags)
        if price_per_kg is None and total_price is not None and amount > 0:
            price_per_kg = total_price / amount
        try:
            self.inventory.deliver(amount, dt_util.now(), price_per_kg=price_per_kg, supplier=supplier, note=note)
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
        counter = self.boiler.data.total_kg if self.boiler.data else None
        try:
            self.inventory.fill_silo(
                amount, dt_util.now(), from_storage=from_storage, silo_after=silo_after, counter_kg=counter, note=note
            )
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

    async def async_import_fills(self, text: str, unit_kg: float) -> dict[str, Any]:
        """Merge a fill log (date + amount per line) into the training data."""
        try:
            new = fills.parse_log(text, unit_kg)
        except fills.FillLogError as err:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="bad_fill_log", translation_placeholders={"error": str(err)}
            ) from err
        if not new:
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="empty_fill_log")
        merged = {f.day: f.kg for f in self.fill_log} | {f.day: f.kg for f in new}
        self.fill_log = [fills.Fill(d, kg) for d, kg in sorted(merged.items())]
        await self._ensure_weather(min(merged), max(merged))
        await self._changed()
        periods = fills.periods(self.fill_log, self._weather)
        return {
            "fills": len(self.fill_log),
            "first": self.fill_log[0].day.isoformat(),
            "last": self.fill_log[-1].day.isoformat(),
            "total_kg": round(sum(f.kg for f in self.fill_log), 1),
            "periods": len(periods),
            "calibration": None
            if self.insights.calibration is None
            else {
                "factor": round(self.insights.calibration.factor, 3),
                "uncertainty": round(self.insights.calibration.uncertainty, 3),
                "source": self.insights.calibration.source,
            },
        }

    async def async_maintenance_done(self, task: str, day: date | None = None) -> None:
        state = self.tasks.setdefault(task, maintenance.TaskState())
        maintenance.mark_done(state, day or dt_util.now().date(), self.burned_total)
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

    # -- derived state -------------------------------------------------------

    def _restock(self, today: date) -> date:
        text = self._opt(CONF_RESTOCK_DATE) or self._opt(CONF_SEASON_END)
        if text:
            try:
                month, day = (int(x) for x in str(text).split("-")[-2:])
                end = date(today.year, month, day)
                return end if end > today else date(today.year + 1, month, day)
            except ValueError:
                _LOGGER.warning("Ignoring restock date %r, expected MM-DD", text)
        return default_season_end(today, self.hass.config.latitude)

    def _task_specs(self) -> dict[str, maintenance.TaskSpec]:
        ash = maintenance.ash_interval_kg(
            float(self._opt(CONF_ASH_CAPACITY, DEFAULT_ASH_CAPACITY)), float(self._opt(CONF_ASH_PCT, DEFAULT_ASH_PCT))
        )
        return {
            TASK_ASH: maintenance.TaskSpec(TASK_ASH, interval_kg=ash),
            TASK_CLEAN: maintenance.TaskSpec(
                TASK_CLEAN, interval_kg=float(self._opt(CONF_CLEAN_INTERVAL_KG, DEFAULT_CLEAN_INTERVAL_KG)) or None
            ),
            TASK_SWEEP: maintenance.TaskSpec(
                TASK_SWEEP, interval_days=int(self._opt(CONF_SWEEP_INTERVAL_DAYS, DEFAULT_SWEEP_INTERVAL_DAYS)) or None
            ),
            TASK_SERVICE: maintenance.TaskSpec(
                TASK_SERVICE,
                interval_kg=float(self._opt(CONF_SERVICE_INTERVAL_KG, DEFAULT_SERVICE_INTERVAL_KG)) or None,
                interval_days=int(self._opt(CONF_SERVICE_INTERVAL_DAYS, DEFAULT_SERVICE_INTERVAL_DAYS)) or None,
            ),
        }

    def _state(self) -> PelletState:
        now = dt_util.now()
        today = now.date()
        data = self.boiler.data
        counter = data.total_kg if data else None
        # The controller counts today from midnight; our own count only from when we first saw it today.
        burned = data.kg_on(today) if data is not None else None
        if burned is None:
            burned = self.history.burned_today(counter)
        if burned is not None:
            burned *= self.calibration_factor

        expected = self.insights.expected_daily
        specs = self._task_specs()
        tasks = {
            key: maintenance.status(
                spec, self.tasks.get(key, maintenance.TaskState()), burned_total=self.burned_total, today=today,
                expected_kg=expected,
            )
            for key, spec in specs.items()
        }

        restock = self._restock(today)
        order_by = None
        if self.result is not None and self.result.runout["p10"] is not None:
            lead = int(self._opt(CONF_ORDER_LEAD_DAYS, DEFAULT_ORDER_LEAD_DAYS))
            order_by = max(today, self.result.runout["p10"] - timedelta(days=lead))

        price = self.inventory.avg_price_per_kg
        heat = economy.heat_price(
            price,
            float(self._opt(CONF_KWH_PER_KG, DEFAULT_KWH_PER_KG)),
            float(self._opt(CONF_BOILER_EFFICIENCY, DEFAULT_BOILER_EFFICIENCY)),
            self._state_number(self._opt(CONF_ELECTRICITY_PRICE)),
            float(self._opt(CONF_HEAT_PUMP_COP, DEFAULT_HEAT_PUMP_COP)),
        )
        cost_today = cost_month = cost_season = cost_30 = None
        if price is not None:
            month_start = today.replace(day=1).isoformat()
            season = economy.season_of(today, self.hass.config.latitude < 0)
            kg_month = sum(r.kg or 0.0 for k, r in self.history.days.items() if k >= month_start and k != self.history.current)
            kg_season = sum(
                r.kg or 0.0
                for k, r in self.history.days.items()
                if k != self.history.current and economy.season_of(date.fromisoformat(k), self.hass.config.latitude < 0) == season
            )
            today_kg = burned or 0.0
            cost_today = round(today_kg * price, 2)
            cost_month = round((kg_month * self.calibration_factor + today_kg) * price, 2)
            cost_season = round((kg_season * self.calibration_factor + today_kg) * price, 2)
            if self.result is not None:
                cost_30 = round(self.result.kg_next_30 * price, 2)

        return PelletState(
            inventory=self.inventory,
            model=self.model,
            forecast=self.result,
            climate=self.climate,
            burned_today=burned,
            computed_at=self.computed_at,
            insights=self.insights,
            tasks=tasks,
            restock=restock,
            order_by=order_by,
            heat=heat,
            cost_today=cost_today,
            cost_month=cost_month,
            cost_season=cost_season,
            cost_next_30=cost_30,
            burned_total=round(self.burned_total, 1),
            on_cloud=self.boiler.on_cloud,
        )

    # -- the heavy part ------------------------------------------------------

    def _weather(self, day: date) -> Weather | None:
        row = self.measured.get(day.isoformat())
        if row is None or row[0] is None:
            return None
        return Weather(row[0], row[1], row[2])

    def _wind_sun(self, day: date) -> tuple[float | None, float | None] | None:
        row = self.measured.get(day.isoformat())
        return None if row is None else (row[1], row[2])

    async def _async_update_data(self) -> PelletState:
        self._apply_options()
        now = dt_util.now()
        today = now.date()
        await self._refresh_climate(now)
        await self._refresh_open_meteo(now)
        temp_fc = await self._weather_entity_forecast(today)

        self._backfill(today)
        factor = self.calibration_factor
        split = bool(self._opt(CONF_SPLIT_HOT_WATER, True)) and self.history.dhw_mean(today) is not None
        days = [
            DayObs(d.day, d.kg * (factor if self._counted(d.day) else 1.0), d.temp, d.wind, d.sun)
            for d in self.history.observations(self._wind_sun, heating_only=split)
        ]
        months = [
            PeriodObs(m.start, m.days, m.kg * factor, m.weather, m.source) for m in self._months(today)
        ]
        fill_periods = self._fill_periods(days, months)
        annual = float(self._opt(CONF_ANNUAL_KG, DEFAULT_ANNUAL_KG))
        self.model = await self.hass.async_add_executor_job(
            partial(
                consumption.fit, days, self.climate, today=today, prior_annual_kg=annual, months=months, periods=fill_periods
            )
        )
        dhw_daily = self.history.dhw_mean(today) if split else None

        # Forecast weather: Open-Meteo, overridden by the user's weather entity where it has days.
        merged = {d: r[0] for d, r in self.om_forecast.items() if d >= today and r[0] is not None} | temp_fc
        teff = effective_temps([(d, t) for d, t in self.history.temps() if d < today])
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        burned_today = self._state().burned_today or 0.0
        inp = ForecastInput(
            today=today,
            day_fraction_left=1.0 - (now - midnight).total_seconds() / 86400.0,
            stock_kg=self.inventory.total_kg,
            silo_kg=self.inventory.silo_kg,
            temp_forecast=merged,
            wind_forecast={d: r[1] for d, r in self.om_forecast.items() if r[1] is not None},
            sun_forecast={d: r[2] for d, r in self.om_forecast.items() if r[2] is not None},
            extra_daily_kg=dhw_daily or 0.0,
            last_temp_eff=teff.get(today - timedelta(days=1)),
            burned_today_kg=burned_today,
            season_end=self._restock(today),
            seed=today.toordinal(),
        )
        self.result = await self.hass.async_add_executor_job(forecast.run, inp, self.model, self.climate)
        # The executor works on snapshots: boiler polls keep changing the live objects meanwhile.
        snapshot = _Snapshot(
            days=dict(self.history.days),
            current=self.history.current,
            lots={lot.id: (lot.supplier, lot.delivered[:10]) for lot in self.inventory.lots},
            fill_events=[
                (date.fromisoformat(e.at[:10]), e.kg, e.counter_kg)
                for e in self.inventory.events
                if e.kind == "fill" and e.counter_kg is not None
            ],
            fill_log=list(self.fill_log),
            silo_capacity=self.inventory.silo_capacity_kg,
            month_kg=self.boiler.data.kg_in_month if self.boiler.data else None,
        )
        self.insights = await self.hass.async_add_executor_job(
            self._compute_insights, today, days, months + fill_periods, dhw_daily, snapshot
        )
        self.computed_at = now
        self._save_soon()
        return self._state()

    def _counted(self, day: date) -> bool:
        """Days whose kg came from the controller's counter (and so take the calibration factor)."""
        rec = self.history.days.get(day.isoformat())
        return rec is not None and rec.source in ("counter", "controller", "live")

    def _fill_periods(self, days: list[DayObs], months: list[PeriodObs]) -> list[PeriodObs]:
        """Fill-log periods that don't overlap better data (days we observed, controller months)."""
        if not self.fill_log:
            return []
        seen = {d.day for d in days}
        for m in months:
            seen.update(m.dates())
        out = []
        for p in fills.periods(self.fill_log, self._weather):
            overlap = sum(1 for d in p.dates() if d in seen)
            if overlap <= p.days * 0.2:
                out.append(p)
        return out

    def _compute_insights(
        self, today: date, days: list[DayObs], periods: list[PeriodObs], dhw_daily: float | None, snap: _Snapshot
    ) -> Insights:
        model, climate = self.model, self.climate
        assert model is not None and self.result is not None
        extra = dhw_daily or 0.0

        # Expected burn per day for ~13 months: the forecast's first month, then climate normals.
        curve = [c["kg"] for c in self.result.curve]
        expected = curve + [
            model.expected(climate.mean(d), climate.wind_mean, climate.sun(d)) + extra
            for d in (today + timedelta(days=i) for i in range(len(curve), 400))
        ]
        year = [date(2001, 1, 1) + timedelta(days=i) for i in range(365)]
        normal_year = sum(model.expected(climate.mean(d), climate.wind_mean, climate.sun(d)) for d in year) + 365 * extra

        teff = effective_temps([(d.day, d.temp) for d in days if d.temp is not None])
        rows = [(d, d.kg, model.expected(teff[d.day], d.wind, d.sun)) for d in days if d.day in teff]

        def lot_info(lot_id: int) -> tuple[str | None, str | None]:
            return snap.lots.get(lot_id, (None, None))

        lot_rows = []
        for d, actual, exp in rows:
            rec = snap.days.get(d.day.isoformat())
            if rec is not None and rec.lot not in (None, UNKNOWN_LOT):
                lot_rows.append((rec.lot, actual, exp))

        season_rows = [(d.day, actual, exp) for d, actual, exp in rows]
        for p in periods:
            if p.weather is None:
                continue
            pteff = effective_temps([(d, w.temp) for d, w in zip(p.dates(), p.weather, strict=True)])
            exp = sum(model.expected(pteff[d], w.wind, w.sun) for d, w in zip(p.dates(), p.weather, strict=True))
            # Spread the period evenly over its days so it lands in the right season.
            for d in p.dates():
                season_rows.append((d, p.kg / p.days, exp / p.days))

        cal = fills.calibrate_from_counter(snap.fill_events, snap.silo_capacity)
        if cal is None and snap.fill_log and snap.month_kg is not None:
            cal = fills.calibrate_from_months(snap.fill_log, snap.month_kg)

        cutoff = (today - timedelta(days=30)).isoformat()
        recent = [r for k, r in snap.days.items() if k >= cutoff and k != snap.current and r.kg]
        dhw_share = None
        if recent and any(r.dhw_kg for r in recent):
            dhw_share = sum(r.dhw_kg or 0.0 for r in recent) / sum(r.kg or 0.0 for r in recent)

        return Insights(
            expected_daily=expected,
            lots=economy.lot_quality(lot_rows, lot_info),
            seasons=economy.season_trend(season_rows, normal_year, self.hass.config.latitude < 0),
            normal_year_kg=round(normal_year, 1),
            calibration=cal,
            dhw_daily_kg=None if dhw_daily is None else round(dhw_daily, 2),
            dhw_share=None if dhw_share is None else round(dhw_share, 3),
        )

    async def _refresh_climate(self, now: datetime) -> None:
        if not self._opt(CONF_USE_OPEN_METEO, True):
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
            self._remember(history, now.date())
            _LOGGER.debug("Climate fitted from %.1f years of history", self.climate.years)
        except (open_meteo.OpenMeteoError, ValueError) as err:
            _LOGGER.info("Using default climate, Open-Meteo history unavailable: %s", err)
            self.climate_at = now - timedelta(days=CLIMATE_MAX_AGE_DAYS - 1)  # try again in a day

    async def _ensure_weather(self, first: date, last: date) -> None:
        """Make sure measured weather covers ``first``..``last`` (for an imported fill log)."""
        missing = [
            first + timedelta(days=i)
            for i in range((last - first).days + 1)
            if (first + timedelta(days=i)).isoformat() not in self.measured
        ]
        if not missing or not self._opt(CONF_USE_OPEN_METEO, True):
            return
        try:
            rows = await open_meteo.fetch_history(
                async_get_clientsession(self.hass),
                self.hass.config.latitude,
                self.hass.config.longitude,
                dt_util.now().date(),
                start=min(missing),
            )
        except open_meteo.OpenMeteoError as err:
            _LOGGER.info("No weather for the imported fill log: %s", err)
            return
        self._remember(rows, dt_util.now().date())

    def _remember(self, rows: list[open_meteo.Row], today: date) -> None:
        """Keep measured weather for recent days and for every day an imported fill log covers."""
        keep_from = (today - timedelta(days=KEEP_RECENT_WEATHER)).isoformat()
        fill_days = set()
        if self.fill_log:
            first, last = self.fill_log[0].day, self.fill_log[-1].day
            fill_days = {(first + timedelta(days=i)).isoformat() for i in range((last - first).days + 1)}
        for d, t, w, s in rows:
            key = d.isoformat()
            if d < today and (key >= keep_from or key in fill_days):
                self.measured[key] = [round(t, 2), None if w is None else round(w, 2), None if s is None else round(s, 2)]
        self.measured = {k: v for k, v in self.measured.items() if k >= keep_from or k in fill_days}

    async def _refresh_open_meteo(self, now: datetime) -> None:
        if not self._opt(CONF_USE_OPEN_METEO, True):
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
        self.om_forecast = {d: [t, w, s] for d, t, w, s in daily if d >= today}
        self._remember(daily, today)
        self.om_at = now

    async def _weather_entity_forecast(self, today: date) -> dict[date, float]:
        entity_id = self._opt(CONF_WEATHER_ENTITY)
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
        """Teach the model from the controller's own day arrays and measured weather."""
        data = self.boiler.data
        if data is not None:
            for back in range(1, 32):
                day = today - timedelta(days=back)
                kg = data.kg_on(day)
                if kg is not None:
                    w = self._weather(day)
                    self.history.backfill(day, kg, w.temp if w else None, "controller")
                    rec = self.history.days.get(day.isoformat())
                    if rec is not None and rec.dhw_kg is None:
                        rec.dhw_kg = data.dhw_kg_on(day)
        for key, rec in self.history.days.items():
            if rec.temp is None and (row := self.measured.get(key)) and row[0] is not None:
                rec.temp_override = row[0]

    def _months(self, today: date) -> list[PeriodObs]:
        """Closed months from the controller that the daily history does not already cover."""
        data = self.boiler.data
        if data is None:
            return []
        covered: dict[tuple[int, int], int] = {}
        for obs in self.history.observations():
            key = (obs.day.year, obs.day.month)
            covered[key] = covered.get(key, 0) + 1
        out: list[PeriodObs] = []
        first = today.replace(day=1)
        for _ in range(11):
            end = first - timedelta(days=1)
            first = end.replace(day=1)
            kg = data.kg_in_month(first.year, first.month)
            if kg is None or covered.get((first.year, first.month), 0) >= 5:
                continue
            days_in = [first + timedelta(days=i) for i in range(end.day)]
            weather = [self._weather(d) for d in days_in]
            out.append(
                PeriodObs(first, end.day, kg, tuple(weather) if all(weather) else None, "controller")  # type: ignore[arg-type]
            )
        return out
