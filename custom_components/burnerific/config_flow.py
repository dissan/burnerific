"""Set up Burnerific: find the boiler, then tell it about the pellets."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.components import network
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_HOST
from homeassistant.core import callback
from homeassistant.data_entry_flow import section
from homeassistant.helpers import selector

from .const import (
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
    CONF_PIN,
    CONF_RESTOCK_DATE,
    CONF_SCAN_INTERVAL,
    CONF_SERIAL,
    CONF_SERVICE_INTERVAL_DAYS,
    CONF_SERVICE_INTERVAL_KG,
    CONF_SILO_CAPACITY,
    CONF_SILO_SENSOR,
    CONF_SILO_SENSOR_EMPTY,
    CONF_SILO_SENSOR_FULL,
    CONF_SILO_TRACKING,
    CONF_SPLIT_HOT_WATER,
    CONF_STOKERCLOUD_USER,
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
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_SERVICE_INTERVAL_DAYS,
    DEFAULT_SERVICE_INTERVAL_KG,
    DEFAULT_SWEEP_INTERVAL_DAYS,
    DOMAIN,
    SILO_BOILER,
    SILO_INTEGRATION,
    SILO_SENSOR,
)
from .nbe import DiscoveryInfo, NbeClient, NbeError, scan
from .nbe.client import subnet_hosts

_LOGGER = logging.getLogger(__name__)

MANUAL = "manual"


def _num(minimum: float, maximum: float, step: float, unit: str | None = None) -> selector.NumberSelector:
    config = selector.NumberSelectorConfig(min=minimum, max=maximum, step=step, mode=selector.NumberSelectorMode.BOX)
    if unit:
        config["unit_of_measurement"] = unit
    return selector.NumberSelector(config)


def _kg(maximum: float = 100000, step: float = 1) -> selector.NumberSelector:
    return _num(0, maximum, step, "kg")


def _password() -> selector.TextSelector:
    return selector.TextSelector(selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD))


async def _probe(host: str, serial: str | None, pin: str | None) -> tuple[DiscoveryInfo, bool | None]:
    """Connect, read once, and check the PIN if one is given. Returns (info, pin_ok)."""
    client = NbeClient(host, serial=serial, pin=pin, timeout=2.5, retries=1)
    try:
        info = await client.discover()
        await client.operating_data()
        pin_ok = await client.validate_pin() if pin else None
        return info, pin_ok
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
    VERSION = 2

    def __init__(self) -> None:
        self._found: dict[str, DiscoveryInfo] = {}
        self._data: dict[str, Any] = {}

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return BurnerificOptionsFlow()

    async def _discover(self) -> list[DiscoveryInfo]:
        """Broadcast first; if nothing answers, ask every address on our own subnets directly."""
        try:
            found = await scan(wait=2.0)
        except OSError as err:
            _LOGGER.debug("Broadcast discovery failed: %s", err)
            found = []
        if found:
            return found
        networks, own = [], []
        try:
            for adapter in await network.async_get_adapters(self.hass):
                if not adapter["enabled"]:
                    continue
                for ip in adapter["ipv4"]:
                    networks.append(f"{ip['address']}/{ip['network_prefix']}")
                    own.append(ip["address"])
        except Exception as err:  # noqa: BLE001 - discovery is best effort
            _LOGGER.debug("No network adapters for a subnet sweep: %s", err)
        hosts = subnet_hosts(networks, own)
        if not hosts:
            return []
        try:
            return await scan(hosts, wait=3.0)
        except OSError as err:
            _LOGGER.debug("Subnet sweep failed: %s", err)
            return []

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        found = await self._discover()
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
                    vol.Optional(CONF_PIN, default=""): _password(),
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
                info, pin_ok = await _probe(host, serial, pin)
            except NbeError:
                errors["base"] = "cannot_connect"
            except OSError:
                errors[CONF_HOST] = "invalid_host"
            else:
                if pin_ok is False:
                    errors[CONF_PIN] = "invalid_pin"
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
                    vol.Optional(CONF_PIN, default=""): _password(),
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
                _, pin_ok = await _probe(host, entry.data.get(CONF_SERIAL), pin)
            except (NbeError, OSError):
                errors["base"] = "cannot_connect"
            else:
                if pin_ok is False:
                    errors[CONF_PIN] = "invalid_pin"
                else:
                    return self.async_update_reload_and_abort(entry, data_updates={CONF_HOST: host, CONF_PIN: pin})
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_HOST, default=entry.data[CONF_HOST]): str,
                    vol.Optional(CONF_PIN, default=""): _password(),
                }
            ),
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        """The controller refused our PIN (changed on the panel, or typed wrong)."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            pin = (user_input.get(CONF_PIN) or "").strip() or None
            try:
                _, pin_ok = await _probe(entry.data[CONF_HOST], entry.data.get(CONF_SERIAL), pin)
            except (NbeError, OSError):
                errors["base"] = "cannot_connect"
            else:
                if pin and pin_ok:
                    return self.async_update_reload_and_abort(entry, data_updates={CONF_PIN: pin})
                errors[CONF_PIN] = "invalid_pin"
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_PIN): _password()}),
            description_placeholders={"boiler": entry.title},
            errors=errors,
        )


SECTIONS = ("boiler", "weather", "stock", "maintenance", "money")


class BurnerificOptionsFlow(OptionsFlow):
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            flat: dict[str, Any] = {}
            for key, value in user_input.items():
                if key in SECTIONS and isinstance(value, dict):
                    flat |= value
                else:
                    flat[key] = value
            return self.async_create_entry(data={k: v for k, v in flat.items() if v not in (None, "")})
        o = self.config_entry.options

        def suggested(key: str, fallback: Any = None) -> dict[str, Any]:
            return {"suggested_value": o.get(key, fallback)}

        def req(key: str, default: Any) -> vol.Required:
            return vol.Required(key, default=o.get(key, default))

        boiler = vol.Schema(
            {
                req(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL): _num(10, 600, 5, "s"),
                vol.Optional(CONF_STOKERCLOUD_USER, description=suggested(CONF_STOKERCLOUD_USER)): selector.TextSelector(),
            }
        )
        weather = vol.Schema(
            {
                vol.Optional(CONF_WEATHER_ENTITY, description=suggested(CONF_WEATHER_ENTITY)): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="weather")
                ),
                vol.Optional(CONF_TEMPERATURE_ENTITY, description=suggested(CONF_TEMPERATURE_ENTITY)): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor", device_class="temperature")
                ),
                req(CONF_USE_OPEN_METEO, True): selector.BooleanSelector(),
            }
        )
        stock = vol.Schema(
            {
                req(CONF_BAG_KG, DEFAULT_BAG_KG): _kg(2000, 0.5),
                vol.Optional(CONF_SILO_CAPACITY, description=suggested(CONF_SILO_CAPACITY)): _kg(50000),
                req(CONF_SILO_TRACKING, SILO_BOILER): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[SILO_BOILER, SILO_INTEGRATION, SILO_SENSOR], translation_key="silo_tracking"
                    )
                ),
                vol.Optional(CONF_SILO_SENSOR, description=suggested(CONF_SILO_SENSOR)): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor")
                ),
                vol.Optional(CONF_SILO_SENSOR_EMPTY, description=suggested(CONF_SILO_SENSOR_EMPTY)): _num(0, 100000, 0.1),
                vol.Optional(CONF_SILO_SENSOR_FULL, description=suggested(CONF_SILO_SENSOR_FULL)): _num(0, 100000, 0.1),
                req(CONF_ANNUAL_KG, DEFAULT_ANNUAL_KG): _kg(50000, 50),
                req(CONF_ORDER_LEAD_DAYS, DEFAULT_ORDER_LEAD_DAYS): _num(0, 120, 1, "d"),
                vol.Optional(CONF_RESTOCK_DATE, description=suggested(CONF_RESTOCK_DATE)): selector.TextSelector(),
                req(CONF_APPLY_CALIBRATION, False): selector.BooleanSelector(),
                req(CONF_SPLIT_HOT_WATER, True): selector.BooleanSelector(),
            }
        )
        maint = vol.Schema(
            {
                req(CONF_ASH_CAPACITY, DEFAULT_ASH_CAPACITY): _kg(100, 0.5),
                req(CONF_ASH_PCT, DEFAULT_ASH_PCT): _num(0.1, 5, 0.05, "%"),
                req(CONF_CLEAN_INTERVAL_KG, DEFAULT_CLEAN_INTERVAL_KG): _kg(20000, 50),
                req(CONF_SWEEP_INTERVAL_DAYS, DEFAULT_SWEEP_INTERVAL_DAYS): _num(0, 1095, 1, "d"),
                req(CONF_SERVICE_INTERVAL_KG, DEFAULT_SERVICE_INTERVAL_KG): _kg(50000, 100),
                req(CONF_SERVICE_INTERVAL_DAYS, DEFAULT_SERVICE_INTERVAL_DAYS): _num(0, 1095, 1, "d"),
            }
        )
        money = vol.Schema(
            {
                req(CONF_KWH_PER_KG, DEFAULT_KWH_PER_KG): _num(3, 6, 0.05, "kWh/kg"),
                req(CONF_BOILER_EFFICIENCY, DEFAULT_BOILER_EFFICIENCY): _num(0.5, 1.0, 0.01),
                vol.Optional(CONF_ELECTRICITY_PRICE, description=suggested(CONF_ELECTRICITY_PRICE)): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain=["sensor", "input_number"])
                ),
                req(CONF_HEAT_PUMP_COP, DEFAULT_HEAT_PUMP_COP): _num(1, 7, 0.1),
            }
        )
        schema = vol.Schema(
            {
                vol.Required("boiler"): section(boiler, {"collapsed": False}),
                vol.Required("weather"): section(weather, {"collapsed": True}),
                vol.Required("stock"): section(stock, {"collapsed": False}),
                vol.Required("maintenance"): section(maint, {"collapsed": True}),
                vol.Required("money"): section(money, {"collapsed": True}),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
