"""Set up Burnerific: find the boiler, then tell it about the pellets."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_HOST
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    CONF_ANNUAL_KG,
    CONF_BAG_KG,
    CONF_INITIAL_STORAGE,
    CONF_KWH_PER_KG,
    CONF_ORDER_LEAD_DAYS,
    CONF_PIN,
    CONF_SCAN_INTERVAL,
    CONF_SEASON_END,
    CONF_SERIAL,
    CONF_SILO_CAPACITY,
    CONF_SILO_TRACKING,
    CONF_TEMPERATURE_ENTITY,
    CONF_USE_OPEN_METEO,
    CONF_WEATHER_ENTITY,
    DEFAULT_ANNUAL_KG,
    DEFAULT_BAG_KG,
    DEFAULT_KWH_PER_KG,
    DEFAULT_ORDER_LEAD_DAYS,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    SILO_BOILER,
    SILO_INTEGRATION,
)
from .nbe import DiscoveryInfo, NbeClient, NbeError, scan

_LOGGER = logging.getLogger(__name__)

MANUAL = "manual"


def _kg(maximum: float = 100000, step: float = 1) -> selector.NumberSelector:
    return selector.NumberSelector(
        selector.NumberSelectorConfig(min=0, max=maximum, step=step, unit_of_measurement="kg", mode=selector.NumberSelectorMode.BOX)
    )


async def _probe(host: str, serial: str | None, pin: str | None) -> DiscoveryInfo:
    client = NbeClient(host, serial=serial, pin=pin, timeout=2.5, retries=1)
    try:
        info = await client.discover()
        await client.operating_data()
        return info
    finally:
        await client.close()


def _pellet_schema(defaults: dict[str, Any], hass_has_weather: bool) -> vol.Schema:
    fields: dict[Any, Any] = {
        vol.Optional(CONF_INITIAL_STORAGE, default=defaults.get(CONF_INITIAL_STORAGE, 0)): _kg(),
        vol.Optional(CONF_SILO_CAPACITY, description={"suggested_value": defaults.get(CONF_SILO_CAPACITY)}): _kg(5000),
        vol.Required(CONF_BAG_KG, default=defaults.get(CONF_BAG_KG, DEFAULT_BAG_KG)): _kg(2000, 0.5),
        vol.Required(CONF_ANNUAL_KG, default=defaults.get(CONF_ANNUAL_KG, DEFAULT_ANNUAL_KG)): _kg(50000, 50),
    }
    if hass_has_weather:
        fields[vol.Optional(CONF_WEATHER_ENTITY, description={"suggested_value": defaults.get(CONF_WEATHER_ENTITY)})] = (
            selector.EntitySelector(selector.EntitySelectorConfig(domain="weather"))
        )
    return vol.Schema(fields)


class BurnerificConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._found: dict[str, DiscoveryInfo] = {}
        self._data: dict[str, Any] = {}

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return BurnerificOptionsFlow()

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        try:
            found = await scan(wait=2.0)
        except OSError as err:
            _LOGGER.debug("Broadcast discovery failed: %s", err)
            found = []
        configured = {e.unique_id for e in self._async_current_entries()}
        self._found = {f.address: f for f in found if f.serial not in configured}
        if not self._found:
            return await self.async_step_manual()
        return await self.async_step_pick()

    async def async_step_pick(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            choice = user_input[CONF_HOST]
            if choice == MANUAL:
                return await self.async_step_manual()
            info = self._found[choice]
            return await self.async_step_manual(
                {CONF_HOST: info.address, CONF_SERIAL: info.serial or "", CONF_PIN: user_input.get(CONF_PIN, "")}
            )
        options = [
            selector.SelectOptionDict(
                value=addr, label=f"{info.model or 'NBE'} {info.serial or ''} ({addr})".replace("  ", " ")
            )
            for addr, info in self._found.items()
        ] + [selector.SelectOptionDict(value=MANUAL, label="Enter address manually")]
        return self.async_show_form(
            step_id="pick",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_HOST, default=options[0]["value"]): selector.SelectSelector(
                        selector.SelectSelectorConfig(options=options, translation_key="pick")
                    ),
                    vol.Optional(CONF_PIN, default=""): selector.TextSelector(
                        selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                    ),
                }
            ),
        )

    async def async_step_manual(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            serial = (user_input.get(CONF_SERIAL) or "").strip() or None
            pin = (user_input.get(CONF_PIN) or "").strip() or None
            try:
                info = await _probe(host, serial, pin)
            except NbeError:
                errors["base"] = "cannot_connect"
            except OSError:
                errors[CONF_HOST] = "invalid_host"
            else:
                serial = info.serial or serial
                await self.async_set_unique_id(serial or host)
                self._abort_if_unique_id_configured(updates={CONF_HOST: host})
                self._data = {CONF_HOST: host, CONF_SERIAL: serial, CONF_PIN: pin, "model": info.model}
                return await self.async_step_pellets()
        defaults = user_input or {}
        return self.async_show_form(
            step_id="manual",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_HOST, default=defaults.get(CONF_HOST, "")): str,
                    vol.Optional(CONF_SERIAL, default=defaults.get(CONF_SERIAL, "")): str,
                    vol.Optional(CONF_PIN, default=""): selector.TextSelector(
                        selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                    ),
                }
            ),
            errors=errors,
        )

    async def async_step_pellets(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            data = self._data | {CONF_INITIAL_STORAGE: user_input.pop(CONF_INITIAL_STORAGE, 0)}
            options = {k: v for k, v in user_input.items() if v not in (None, "")}
            title = f"NBE {data.get('model') or 'boiler'} {data.get(CONF_SERIAL) or data[CONF_HOST]}"
            return self.async_create_entry(title=title, data=data, options=options)
        has_weather = bool(self.hass.states.async_entity_ids("weather"))
        defaults = {}
        if has_weather:
            defaults[CONF_WEATHER_ENTITY] = sorted(self.hass.states.async_entity_ids("weather"))[0]
        return self.async_show_form(step_id="pellets", data_schema=_pellet_schema(defaults, has_weather))

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            # Blank keeps the current PIN.
            pin = (user_input.get(CONF_PIN) or "").strip() or entry.data.get(CONF_PIN)
            try:
                await _probe(host, entry.data.get(CONF_SERIAL), pin)
            except (NbeError, OSError):
                errors["base"] = "cannot_connect"
            else:
                return self.async_update_reload_and_abort(entry, data_updates={CONF_HOST: host, CONF_PIN: pin})
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_HOST, default=entry.data[CONF_HOST]): str,
                    vol.Optional(CONF_PIN, default=""): selector.TextSelector(
                        selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                    ),
                }
            ),
            errors=errors,
        )


class BurnerificOptionsFlow(OptionsFlow):
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data={k: v for k, v in user_input.items() if v not in (None, "")})
        o = self.config_entry.options

        def suggested(key: str, fallback: Any = None) -> dict[str, Any]:
            return {"suggested_value": o.get(key, fallback)}

        schema = vol.Schema(
            {
                vol.Required(CONF_SCAN_INTERVAL, default=o.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)): selector.NumberSelector(
                    selector.NumberSelectorConfig(min=10, max=600, step=5, unit_of_measurement="s", mode=selector.NumberSelectorMode.BOX)
                ),
                vol.Optional(CONF_WEATHER_ENTITY, description=suggested(CONF_WEATHER_ENTITY)): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="weather")
                ),
                vol.Optional(CONF_TEMPERATURE_ENTITY, description=suggested(CONF_TEMPERATURE_ENTITY)): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor", device_class="temperature")
                ),
                vol.Required(CONF_USE_OPEN_METEO, default=o.get(CONF_USE_OPEN_METEO, True)): selector.BooleanSelector(),
                vol.Required(CONF_BAG_KG, default=o.get(CONF_BAG_KG, DEFAULT_BAG_KG)): _kg(2000, 0.5),
                vol.Optional(CONF_SILO_CAPACITY, description=suggested(CONF_SILO_CAPACITY)): _kg(5000),
                vol.Required(CONF_SILO_TRACKING, default=o.get(CONF_SILO_TRACKING, SILO_BOILER)): selector.SelectSelector(
                    selector.SelectSelectorConfig(options=[SILO_BOILER, SILO_INTEGRATION], translation_key="silo_tracking")
                ),
                vol.Required(CONF_ANNUAL_KG, default=o.get(CONF_ANNUAL_KG, DEFAULT_ANNUAL_KG)): _kg(50000, 50),
                vol.Required(CONF_ORDER_LEAD_DAYS, default=o.get(CONF_ORDER_LEAD_DAYS, DEFAULT_ORDER_LEAD_DAYS)): selector.NumberSelector(
                    selector.NumberSelectorConfig(min=0, max=120, step=1, unit_of_measurement="d", mode=selector.NumberSelectorMode.BOX)
                ),
                vol.Optional(CONF_SEASON_END, description=suggested(CONF_SEASON_END)): selector.TextSelector(),
                vol.Required(CONF_KWH_PER_KG, default=o.get(CONF_KWH_PER_KG, DEFAULT_KWH_PER_KG)): selector.NumberSelector(
                    selector.NumberSelectorConfig(min=3, max=6, step=0.05, unit_of_measurement="kWh/kg", mode=selector.NumberSelectorMode.BOX)
                ),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
