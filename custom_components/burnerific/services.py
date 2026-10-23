"""Services: deliveries, silo fills, stock corrections, boiler settings and the full forecast."""

from __future__ import annotations

from dataclasses import asdict
from datetime import date
from typing import TYPE_CHECKING, Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv

from .const import (
    ATTR_BAGS,
    ATTR_CONFIG_ENTRY,
    ATTR_DATE,
    ATTR_FROM_STORAGE,
    ATTR_KEY,
    ATTR_KG,
    ATTR_LOG,
    ATTR_NOTE,
    ATTR_PRICE_PER_KG,
    ATTR_SUPPLIER,
    ATTR_TASK,
    ATTR_TOTAL_PRICE,
    ATTR_UNIT,
    ATTR_VALUE,
    DOMAIN,
    SERVICE_ADD_DELIVERY,
    SERVICE_FILL_SILO,
    SERVICE_GET_FORECAST,
    SERVICE_IMPORT_FILLS,
    SERVICE_MAINTENANCE_DONE,
    SERVICE_SET_SETTING,
    SERVICE_SET_SILO,
    SERVICE_SET_STORAGE,
    TASKS,
)

if TYPE_CHECKING:
    from . import BurnerificConfigEntry

_amount = vol.All(vol.Coerce(float), vol.Range(min=0))
_ENTRY = {vol.Optional(ATTR_CONFIG_ENTRY): cv.string}

ADD_DELIVERY_SCHEMA = vol.Schema(
    {
        **_ENTRY,
        vol.Optional(ATTR_KG): _amount,
        vol.Optional(ATTR_BAGS): _amount,
        vol.Optional(ATTR_PRICE_PER_KG): _amount,
        vol.Optional(ATTR_TOTAL_PRICE): _amount,
        vol.Optional(ATTR_SUPPLIER): cv.string,
        vol.Optional(ATTR_NOTE): cv.string,
    }
)
IMPORT_FILLS_SCHEMA = vol.Schema(
    {**_ENTRY, vol.Required(ATTR_LOG): cv.string, vol.Optional(ATTR_UNIT, default="bags"): vol.In(["bags", "kg"])}
)
MAINTENANCE_DONE_SCHEMA = vol.Schema(
    {**_ENTRY, vol.Required(ATTR_TASK): vol.In(TASKS), vol.Optional(ATTR_DATE): cv.date}
)
FILL_SILO_SCHEMA = vol.Schema(
    {
        **_ENTRY,
        vol.Optional(ATTR_KG): _amount,
        vol.Optional(ATTR_BAGS): _amount,
        vol.Optional(ATTR_FROM_STORAGE, default=True): cv.boolean,
        vol.Optional(ATTR_NOTE): cv.string,
    }
)
SET_STORAGE_SCHEMA = vol.Schema(
    {**_ENTRY, vol.Optional(ATTR_KG): _amount, vol.Optional(ATTR_BAGS): _amount, vol.Optional(ATTR_NOTE): cv.string}
)
SET_SILO_SCHEMA = vol.Schema({**_ENTRY, vol.Required(ATTR_KG): _amount, vol.Optional(ATTR_NOTE): cv.string})
SET_SETTING_SCHEMA = vol.Schema(
    {
        **_ENTRY,
        vol.Required(ATTR_KEY): vol.Match(r"^(settings/)?[a-z0-9_]+[./][a-z0-9_]+$"),
        vol.Required(ATTR_VALUE): vol.Any(vol.Coerce(float), cv.string),
    }
)
GET_FORECAST_SCHEMA = vol.Schema(_ENTRY)


def _entry(hass: HomeAssistant, call: ServiceCall) -> BurnerificConfigEntry:
    entries = [
        e for e in hass.config_entries.async_entries(DOMAIN) if e.state is ConfigEntryState.LOADED
    ]
    wanted = call.data.get(ATTR_CONFIG_ENTRY)
    if wanted:
        entries = [e for e in entries if e.entry_id == wanted]
    if len(entries) != 1:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="entry_not_found" if wanted or not entries else "entry_ambiguous",
        )
    return entries[0]


def _jsonable(value: Any) -> Any:
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    return value


def _insights(state: Any) -> dict[str, Any]:
    if state is None:
        return {}
    ins = state.insights
    cal = ins.calibration
    return {
        "restock_date": state.restock.isoformat(),
        "order_by": state.order_by.isoformat() if state.order_by else None,
        "maintenance": {k: t.as_dict() for k, t in state.tasks.items()},
        "lots": [lot.as_dict() for lot in ins.lots],
        "seasons": [s.as_dict() for s in ins.seasons],
        "normal_year_kg": ins.normal_year_kg,
        "calibration": None
        if cal is None
        else {"factor": round(cal.factor, 3), "uncertainty": round(cal.uncertainty, 3), "source": cal.source},
        "hot_water": {"daily_kg": ins.dhw_daily_kg, "share": ins.dhw_share},
        "heat_price": asdict(state.heat),
        "cost": {
            "today": state.cost_today,
            "month": state.cost_month,
            "season": state.cost_season,
            "next_30_days": state.cost_next_30,
        },
    }


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    async def add_delivery(call: ServiceCall) -> None:
        d = call.data
        await _entry(hass, call).runtime_data.pellets.async_add_delivery(
            d.get(ATTR_KG),
            d.get(ATTR_BAGS),
            d.get(ATTR_PRICE_PER_KG),
            d.get(ATTR_TOTAL_PRICE),
            d.get(ATTR_NOTE),
            d.get(ATTR_SUPPLIER),
        )

    async def import_fills(call: ServiceCall) -> ServiceResponse:
        pellets = _entry(hass, call).runtime_data.pellets
        unit = pellets.inventory.bag_kg if call.data[ATTR_UNIT] == "bags" else 1.0
        return await pellets.async_import_fills(call.data[ATTR_LOG], unit)

    async def maintenance_done(call: ServiceCall) -> None:
        await _entry(hass, call).runtime_data.pellets.async_maintenance_done(call.data[ATTR_TASK], call.data.get(ATTR_DATE))

    async def fill_silo(call: ServiceCall) -> None:
        d = call.data
        pellets = _entry(hass, call).runtime_data.pellets
        kg, bags = d.get(ATTR_KG), d.get(ATTR_BAGS)
        if kg is None and bags is None:
            kg = pellets.inventory.suggested_fill_kg()
            if kg <= 0:
                raise ServiceValidationError(translation_domain=DOMAIN, translation_key="nothing_to_fill")
        await pellets.async_fill_silo(kg, bags, d[ATTR_FROM_STORAGE], d.get(ATTR_NOTE))

    async def set_storage(call: ServiceCall) -> None:
        d = call.data
        await _entry(hass, call).runtime_data.pellets.async_set_storage(d.get(ATTR_KG), d.get(ATTR_BAGS), d.get(ATTR_NOTE))

    async def set_silo(call: ServiceCall) -> None:
        d = call.data
        await _entry(hass, call).runtime_data.pellets.async_set_silo(d[ATTR_KG], d.get(ATTR_NOTE))

    async def set_setting(call: ServiceCall) -> None:
        group, _, key = call.data[ATTR_KEY].removeprefix("settings/").replace("/", ".").partition(".")
        await _entry(hass, call).runtime_data.boiler.async_write(group, key, call.data[ATTR_VALUE])

    async def get_forecast(call: ServiceCall) -> ServiceResponse:
        pellets = _entry(hass, call).runtime_data.pellets
        result = pellets.result
        inv = pellets.inventory
        return {
            "computed_at": pellets.computed_at.isoformat() if pellets.computed_at else None,
            "stock": {
                "storage_kg": round(inv.storage_kg, 1),
                "silo_kg": None if inv.silo_kg is None else round(inv.silo_kg, 1),
                "total_kg": round(inv.total_kg, 1),
            },
            "model": pellets.model.describe() if pellets.model else None,
            "climate": {"source": pellets.climate.source, "years": pellets.climate.years, "phi": round(pellets.climate.phi, 3)},
            "forecast": _jsonable(asdict(result)) if result else None,
            **_insights(pellets.data),
        }

    for name, handler, schema in (
        (SERVICE_ADD_DELIVERY, add_delivery, ADD_DELIVERY_SCHEMA),
        (SERVICE_FILL_SILO, fill_silo, FILL_SILO_SCHEMA),
        (SERVICE_SET_STORAGE, set_storage, SET_STORAGE_SCHEMA),
        (SERVICE_SET_SILO, set_silo, SET_SILO_SCHEMA),
        (SERVICE_SET_SETTING, set_setting, SET_SETTING_SCHEMA),
    ):
        hass.services.async_register(DOMAIN, name, handler, schema=schema)
    hass.services.async_register(DOMAIN, SERVICE_MAINTENANCE_DONE, maintenance_done, schema=MAINTENANCE_DONE_SCHEMA)
    hass.services.async_register(
        DOMAIN, SERVICE_IMPORT_FILLS, import_fills, schema=IMPORT_FILLS_SCHEMA, supports_response=SupportsResponse.OPTIONAL
    )
    hass.services.async_register(
        DOMAIN, SERVICE_GET_FORECAST, get_forecast, schema=GET_FORECAST_SCHEMA, supports_response=SupportsResponse.ONLY
    )
