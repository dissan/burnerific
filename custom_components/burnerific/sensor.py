"""Sensors: what the boiler reports, and what we know about the pellets."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfEnergy,
    UnitOfMass,
    UnitOfPower,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import CONF_KWH_PER_KG, DEFAULT_KWH_PER_KG, DOMAIN
from .coordinator import BoilerCoordinator
from .entity import BurnerificEntity
from .nbe import BoilerData
from .nbe.states import STATE_OPTIONS, SUBSTATE_OPTIONS, state_key, substate_key
from .pellet_coordinator import PelletCoordinator, PelletState

if TYPE_CHECKING:
    from . import BurnerificConfigEntry

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class BoilerSensorDescription(SensorEntityDescription):
    value_fn: Callable[[BoilerData], Any]
    always: bool = False  # create even when the boiler does not report it right now
    needs_lambda: bool = False  # junk without a lambda sensor: created disabled then


@dataclass(frozen=True, kw_only=True)
class PelletSensorDescription(SensorEntityDescription):
    value_fn: Callable[[PelletState], Any]
    attrs_fn: Callable[[PelletState], dict[str, Any]] | None = None
    currency: str | None = None  # unit is the HA currency plus this suffix ("", "/kg", "/kWh")


def _temp(key: str, fn: Callable[[BoilerData], float | None], **kw: Any) -> BoilerSensorDescription:
    return BoilerSensorDescription(
        key=key,
        translation_key=key,
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=fn,
        **kw,
    )


def _pct(key: str, fn: Callable[[BoilerData], float | None], **kw: Any) -> BoilerSensorDescription:
    return BoilerSensorDescription(
        key=key,
        translation_key=key,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        value_fn=fn,
        **kw,
    )


BOILER_SENSORS: tuple[BoilerSensorDescription, ...] = (
    BoilerSensorDescription(
        key="state",
        translation_key="state",
        device_class=SensorDeviceClass.ENUM,
        options=STATE_OPTIONS,
        value_fn=lambda d: state_key(d.state),
        always=True,
    ),
    BoilerSensorDescription(
        key="substate",
        translation_key="substate",
        device_class=SensorDeviceClass.ENUM,
        options=SUBSTATE_OPTIONS,
        value_fn=lambda d: substate_key(d.substate),
    ),
    BoilerSensorDescription(
        key="substate_time",
        translation_key="substate_time",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.substate_sec,
    ),
    _pct("power_pct", lambda d: d.power_pct, always=True),
    BoilerSensorDescription(
        key="power_kw",
        translation_key="power_kw",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda d: d.power_kw,
    ),
    _temp("boiler_temp", lambda d: d.boiler_temp, always=True),
    _temp("boiler_ref", lambda d: d.boiler_ref),
    _temp("return_temp", lambda d: d.return_temp),
    _temp("smoke_temp", lambda d: d.smoke_temp),
    _temp("shaft_temp", lambda d: d.shaft_temp, entity_category=EntityCategory.DIAGNOSTIC),
    _temp("dhw_temp", lambda d: d.dhw_temp),
    _temp("dhw_ref", lambda d: d.dhw_ref),
    _temp("outdoor_temp", lambda d: d.outdoor_temp),
    _pct("oxygen", lambda d: d.oxygen, needs_lambda=True),
    _pct("oxygen_ref", lambda d: d.oxygen_ref, entity_category=EntityCategory.DIAGNOSTIC, needs_lambda=True),
    _pct("photo_level", lambda d: d.photo_level, entity_category=EntityCategory.DIAGNOSTIC),
    _pct("fan_speed", lambda d: d.fan_speed, entity_category=EntityCategory.DIAGNOSTIC),
    _pct("exhaust_speed", lambda d: d.exhaust_speed, entity_category=EntityCategory.DIAGNOSTIC),
    BoilerSensorDescription(
        key="consumption_today",
        translation_key="consumption_today",
        device_class=SensorDeviceClass.WEIGHT,
        native_unit_of_measurement=UnitOfMass.KILOGRAMS,
        state_class=SensorStateClass.TOTAL_INCREASING,  # resets at midnight
        suggested_display_precision=1,
        value_fn=lambda d: d.kg_on(d.at.date()),
    ),
    BoilerSensorDescription(
        key="consumption_total",
        translation_key="consumption_total",
        device_class=SensorDeviceClass.WEIGHT,
        native_unit_of_measurement=UnitOfMass.KILOGRAMS,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=0,
        value_fn=lambda d: d.total_kg,
    ),
    BoilerSensorDescription(
        key="hot_water_today",
        translation_key="hot_water_today",
        device_class=SensorDeviceClass.WEIGHT,
        native_unit_of_measurement=UnitOfMass.KILOGRAMS,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=1,
        value_fn=lambda d: d.dhw_kg_today if any(d.dhw_days_kg) else None,
    ),
    BoilerSensorDescription(
        key="ignitions",
        translation_key="ignitions",
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.ignitions,
    ),
    BoilerSensorDescription(
        key="data_source",
        translation_key="data_source",
        device_class=SensorDeviceClass.ENUM,
        options=["local", "cloud"],
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.source,
        always=True,
    ),
)


def _q(state: PelletState, field: str) -> dict[str, Any]:
    f = state.forecast
    if f is None:
        return {}
    values = getattr(f, field)
    return {f"{field}_{k}": (v.isoformat() if isinstance(v, date) else v) for k, v in values.items()}


def _task_date(s: PelletState, key: str) -> date | None:
    t = s.tasks.get(key)
    return t.due_date if t else None


def _task_pct(s: PelletState, key: str) -> float | None:
    t = s.tasks.get(key)
    return None if t is None or t.fraction is None else round(min(100.0, 100 * t.fraction), 1)


def _task_attrs(s: PelletState, key: str) -> dict[str, Any]:
    t = s.tasks.get(key)
    return {} if t is None else {k: v for k, v in t.as_dict().items() if k != "key"}


def _lot_name(s: PelletState) -> str | None:
    lot = s.inventory.burning_lot
    if lot is None:
        return None
    return lot.supplier or f"#{lot.id} {lot.delivered[:10]}"


def _calibration_attrs(s: PelletState) -> dict[str, Any]:
    cal = s.insights.calibration
    if cal is None:
        return {}
    return {
        "uncertainty": round(cal.uncertainty, 3),
        "filled_kg": round(cal.filled_kg),
        "counted_kg": round(cal.counted_kg),
        "source": cal.source,
    }


def _rounded(v: float | None, nd: int = 1) -> float | None:
    return None if v is None else round(v, nd)


PELLET_SENSORS: tuple[PelletSensorDescription, ...] = (
    PelletSensorDescription(
        key="silo",
        translation_key="silo",
        device_class=SensorDeviceClass.WEIGHT,
        native_unit_of_measurement=UnitOfMass.KILOGRAMS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        value_fn=lambda s: _rounded(s.inventory.silo_kg),
        attrs_fn=lambda s: {
            "capacity_kg": s.inventory.silo_capacity_kg,
            "free_kg": _rounded(s.inventory.silo_free_kg),
            "suggested_fill_kg": s.inventory.suggested_fill_kg(),
        },
    ),
    PelletSensorDescription(
        key="silo_level",
        translation_key="silo_level",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        value_fn=lambda s: (
            None
            if not s.inventory.silo_capacity_kg or s.inventory.silo_kg is None
            else min(100.0, 100.0 * s.inventory.silo_kg / s.inventory.silo_capacity_kg)
        ),
    ),
    PelletSensorDescription(
        key="storage",
        translation_key="storage",
        device_class=SensorDeviceClass.WEIGHT,
        native_unit_of_measurement=UnitOfMass.KILOGRAMS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        value_fn=lambda s: round(s.inventory.storage_kg, 1),
        attrs_fn=lambda s: {
            "bags": round(s.inventory.storage_bags, 1),
            "bag_kg": s.inventory.bag_kg,
            "last_events": [
                {"at": e.at, "kind": e.kind, "kg": e.kg, "note": e.note} for e in s.inventory.events[-5:]
            ],
        },
    ),
    PelletSensorDescription(
        key="storage_bags",
        translation_key="storage_bags",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        value_fn=lambda s: round(s.inventory.storage_bags, 1),
    ),
    PelletSensorDescription(
        key="stock",
        translation_key="stock",
        device_class=SensorDeviceClass.WEIGHT,
        native_unit_of_measurement=UnitOfMass.KILOGRAMS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        value_fn=lambda s: round(s.inventory.total_kg, 1),
    ),
    PelletSensorDescription(
        key="stock_value",
        translation_key="stock_value",
        device_class=SensorDeviceClass.MONETARY,
        suggested_display_precision=0,
        value_fn=lambda s: _rounded(s.inventory.stock_value, 2),
        currency="",
    ),
    PelletSensorDescription(
        key="price_per_kg",
        translation_key="price_per_kg",
        suggested_display_precision=2,
        value_fn=lambda s: _rounded(s.inventory.avg_price_per_kg, 3),
        attrs_fn=lambda s: {
            "delivered_kg_total": round(s.inventory.delivered_kg_total, 1),
            "spent_total": round(s.inventory.spent_total, 2),
        },
        currency="/kg",
    ),
    PelletSensorDescription(
        key="burned_today",
        translation_key="burned_today",
        device_class=SensorDeviceClass.WEIGHT,
        native_unit_of_measurement=UnitOfMass.KILOGRAMS,
        state_class=SensorStateClass.TOTAL_INCREASING,  # resets at midnight
        suggested_display_precision=1,
        value_fn=lambda s: _rounded(s.burned_today, 2),
    ),
    PelletSensorDescription(
        key="runout_date",
        translation_key="runout_date",
        device_class=SensorDeviceClass.DATE,
        value_fn=lambda s: s.forecast.runout["p50"] if s.forecast else None,
        attrs_fn=lambda s: _q(s, "runout") | _q(s, "days_left") | ({"bands": s.forecast.bands} if s.forecast else {}),
    ),
    PelletSensorDescription(
        key="days_left",
        translation_key="days_left",
        native_unit_of_measurement=UnitOfTime.DAYS,
        device_class=SensorDeviceClass.DURATION,
        suggested_display_precision=0,
        value_fn=lambda s: s.forecast.days_left["p50"] if s.forecast else None,
        attrs_fn=lambda s: _q(s, "days_left"),
    ),
    PelletSensorDescription(
        key="silo_empty_date",
        translation_key="silo_empty_date",
        device_class=SensorDeviceClass.DATE,
        value_fn=lambda s: s.forecast.silo_empty["p50"] if s.forecast else None,
        attrs_fn=lambda s: _q(s, "silo_empty"),
    ),
    PelletSensorDescription(
        key="order_recommendation",
        translation_key="order_recommendation",
        device_class=SensorDeviceClass.WEIGHT,
        native_unit_of_measurement=UnitOfMass.KILOGRAMS,
        suggested_display_precision=0,
        value_fn=lambda s: s.forecast.order_kg if s.forecast else None,
        attrs_fn=lambda s: {
            "bags": (
                None
                if not s.forecast or s.forecast.order_kg is None or s.inventory.bag_kg <= 0
                else -(-s.forecast.order_kg // s.inventory.bag_kg)
            ),
            "season_end": s.forecast.season_end.isoformat() if s.forecast and s.forecast.season_end else None,
        },
    ),
    PelletSensorDescription(
        key="need_until_season_end",
        translation_key="need_until_season_end",
        device_class=SensorDeviceClass.WEIGHT,
        native_unit_of_measurement=UnitOfMass.KILOGRAMS,
        suggested_display_precision=0,
        value_fn=lambda s: s.forecast.need_until_season_end["p50"] if s.forecast else None,
        attrs_fn=lambda s: _q(s, "need_until_season_end")
        | {"season_end": s.forecast.season_end.isoformat() if s.forecast and s.forecast.season_end else None},
    ),
    PelletSensorDescription(
        key="runout_risk",
        translation_key="runout_risk",
        native_unit_of_measurement=PERCENTAGE,
        suggested_display_precision=0,
        value_fn=lambda s: (
            None
            if not s.forecast or s.forecast.prob_runout_before_season_end is None
            else round(100 * s.forecast.prob_runout_before_season_end, 1)
        ),
    ),
    PelletSensorDescription(
        key="forecast_today",
        translation_key="forecast_today",
        device_class=SensorDeviceClass.WEIGHT,
        native_unit_of_measurement=UnitOfMass.KILOGRAMS,
        suggested_display_precision=1,
        value_fn=lambda s: s.forecast.kg_today if s.forecast else None,
    ),
    PelletSensorDescription(
        key="forecast_7d",
        translation_key="forecast_7d",
        device_class=SensorDeviceClass.WEIGHT,
        native_unit_of_measurement=UnitOfMass.KILOGRAMS,
        suggested_display_precision=0,
        value_fn=lambda s: s.forecast.kg_next_7 if s.forecast else None,
        attrs_fn=lambda s: {"daily": s.forecast.curve[:14]} if s.forecast else {},
    ),
    PelletSensorDescription(
        key="forecast_30d",
        translation_key="forecast_30d",
        device_class=SensorDeviceClass.WEIGHT,
        native_unit_of_measurement=UnitOfMass.KILOGRAMS,
        suggested_display_precision=0,
        value_fn=lambda s: s.forecast.kg_next_30 if s.forecast else None,
        attrs_fn=lambda s: {"daily": s.forecast.curve} if s.forecast else {},
    ),
    PelletSensorDescription(
        key="heating_limit",
        translation_key="heating_limit",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        entity_category=EntityCategory.DIAGNOSTIC,
        suggested_display_precision=1,
        value_fn=lambda s: _rounded(s.model.heating_limit) if s.model else None,
        attrs_fn=lambda s: s.model.describe() if s.model else {},
    ),
    PelletSensorDescription(
        key="kg_per_degree_day",
        translation_key="kg_per_degree_day",
        native_unit_of_measurement="kg/°C·d",
        entity_category=EntityCategory.DIAGNOSTIC,
        suggested_display_precision=2,
        value_fn=lambda s: _rounded(s.model.kg_per_degree_day, 3) if s.model else None,
    ),
    PelletSensorDescription(
        key="base_load",
        translation_key="base_load",
        native_unit_of_measurement="kg/d",
        entity_category=EntityCategory.DIAGNOSTIC,
        suggested_display_precision=1,
        value_fn=lambda s: _rounded(s.model.base_load, 2) if s.model else None,
    ),
    PelletSensorDescription(
        key="model_days",
        translation_key="model_days",
        entity_category=EntityCategory.DIAGNOSTIC,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda s: s.model.n_days if s.model else None,
        attrs_fn=lambda s: {
            "months": s.model.n_months if s.model else None,
            "r2": None if not s.model or s.model.r2 is None else round(s.model.r2, 3),
            "climate_source": s.climate.source,
            "climate_years": s.climate.years,
            "computed_at": s.computed_at.isoformat() if s.computed_at else None,
            "periods": s.model.n_periods if s.model else None,
        },
    ),
    PelletSensorDescription(
        key="order_by",
        translation_key="order_by",
        device_class=SensorDeviceClass.DATE,
        value_fn=lambda s: s.order_by,
    ),
    PelletSensorDescription(
        key="restock_date",
        translation_key="restock_date",
        device_class=SensorDeviceClass.DATE,
        value_fn=lambda s: s.restock,
    ),
    PelletSensorDescription(
        key="ash_level",
        translation_key="ash_level",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        value_fn=lambda s: _task_pct(s, "ash"),
        attrs_fn=lambda s: _task_attrs(s, "ash"),
    ),
    PelletSensorDescription(
        key="clean_due",
        translation_key="clean_due",
        device_class=SensorDeviceClass.DATE,
        value_fn=lambda s: _task_date(s, "clean"),
        attrs_fn=lambda s: _task_attrs(s, "clean"),
    ),
    PelletSensorDescription(
        key="sweep_due",
        translation_key="sweep_due",
        device_class=SensorDeviceClass.DATE,
        value_fn=lambda s: _task_date(s, "sweep"),
        attrs_fn=lambda s: _task_attrs(s, "sweep"),
    ),
    PelletSensorDescription(
        key="service_due",
        translation_key="service_due",
        device_class=SensorDeviceClass.DATE,
        value_fn=lambda s: _task_date(s, "service"),
        attrs_fn=lambda s: _task_attrs(s, "service"),
    ),
    PelletSensorDescription(
        key="cost_today",
        translation_key="cost_today",
        device_class=SensorDeviceClass.MONETARY,
        suggested_display_precision=2,
        value_fn=lambda s: s.cost_today,
        currency="",
    ),
    PelletSensorDescription(
        key="cost_month",
        translation_key="cost_month",
        device_class=SensorDeviceClass.MONETARY,
        suggested_display_precision=0,
        value_fn=lambda s: s.cost_month,
        currency="",
    ),
    PelletSensorDescription(
        key="cost_season",
        translation_key="cost_season",
        device_class=SensorDeviceClass.MONETARY,
        suggested_display_precision=0,
        value_fn=lambda s: s.cost_season,
        currency="",
    ),
    PelletSensorDescription(
        key="cost_next_30d",
        translation_key="cost_next_30d",
        device_class=SensorDeviceClass.MONETARY,
        suggested_display_precision=0,
        value_fn=lambda s: s.cost_next_30,
        currency="",
    ),
    PelletSensorDescription(
        key="fuel_price_kwh",
        translation_key="fuel_price_kwh",
        suggested_display_precision=3,
        value_fn=lambda s: s.heat.pellet_per_kwh_fuel,
        currency="/kWh",
    ),
    PelletSensorDescription(
        key="heat_price_pellets",
        translation_key="heat_price_pellets",
        suggested_display_precision=3,
        value_fn=lambda s: s.heat.pellet_per_kwh_heat,
        attrs_fn=lambda s: {"pellets_cheaper_pct": s.heat.pellets_cheaper_pct},
        currency="/kWh",
    ),
    PelletSensorDescription(
        key="heat_price_heat_pump",
        translation_key="heat_price_heat_pump",
        suggested_display_precision=3,
        value_fn=lambda s: s.heat.heat_pump_per_kwh_heat,
        currency="/kWh",
    ),
    PelletSensorDescription(
        key="burning_lot",
        translation_key="burning_lot",
        value_fn=lambda s: _lot_name(s),
        attrs_fn=lambda s: {"lots": [lot.as_dict() for lot in s.insights.lots]},
    ),
    PelletSensorDescription(
        key="normalized_yearly",
        translation_key="normalized_yearly",
        device_class=SensorDeviceClass.WEIGHT,
        native_unit_of_measurement=UnitOfMass.KILOGRAMS,
        suggested_display_precision=0,
        value_fn=lambda s: s.insights.normal_year_kg,
        attrs_fn=lambda s: {"seasons": [x.as_dict() for x in s.insights.seasons]},
    ),
    PelletSensorDescription(
        key="hot_water_share",
        translation_key="hot_water_share",
        native_unit_of_measurement=PERCENTAGE,
        suggested_display_precision=0,
        value_fn=lambda s: None if s.insights.dhw_share is None else round(100 * s.insights.dhw_share, 1),
        attrs_fn=lambda s: {"daily_kg": s.insights.dhw_daily_kg},
    ),
    PelletSensorDescription(
        key="burned_total",
        translation_key="burned_total",
        device_class=SensorDeviceClass.WEIGHT,
        native_unit_of_measurement=UnitOfMass.KILOGRAMS,
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_category=EntityCategory.DIAGNOSTIC,
        suggested_display_precision=0,
        value_fn=lambda s: s.burned_total,
    ),
    PelletSensorDescription(
        key="counter_calibration",
        translation_key="counter_calibration",
        entity_category=EntityCategory.DIAGNOSTIC,
        suggested_display_precision=3,
        value_fn=lambda s: None if s.insights.calibration is None else round(s.insights.calibration.factor, 3),
        attrs_fn=lambda s: _calibration_attrs(s),
    ),
    PelletSensorDescription(
        key="wind_effect",
        translation_key="wind_effect",
        native_unit_of_measurement=PERCENTAGE,
        entity_category=EntityCategory.DIAGNOSTIC,
        suggested_display_precision=0,
        value_fn=lambda s: round(100 * s.model.wind_effect, 1) if s.model else None,
    ),
    PelletSensorDescription(
        key="sun_effect",
        translation_key="sun_effect",
        native_unit_of_measurement="kg/(MJ/m²)",
        entity_category=EntityCategory.DIAGNOSTIC,
        suggested_display_precision=2,
        value_fn=lambda s: round(s.model.sun_kg_per_mj, 3) if s.model else None,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: BurnerificConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    rt = entry.runtime_data
    data = rt.boiler.data
    entities: list[SensorEntity] = [
        BoilerSensor(rt.boiler, d, enabled=not (d.needs_lambda and data is not None and data.lambda_fitted is False))
        for d in BOILER_SENSORS
        if d.always or (data and d.value_fn(data) is not None)
    ]
    entities += [PelletSensor(rt.pellets, rt.boiler, d) for d in PELLET_SENSORS]
    if data is not None and data.total_kg is not None:
        entities.append(HeatSensor(rt.boiler, entry.options.get(CONF_KWH_PER_KG, DEFAULT_KWH_PER_KG)))
    async_add_entities(entities)
    _hide_oxygen_once(hass, entry, data)


def _hide_oxygen_once(hass: HomeAssistant, entry: BurnerificConfigEntry, data: BoilerData | None) -> None:
    """Versions before 0.2 created the oxygen sensors enabled even without a lambda sensor.

    Disable them once on such boilers; if the user turns them back on, that sticks.
    """
    pellets = entry.runtime_data.pellets
    if data is None or data.lambda_fitted is not False or pellets.flags.get("oxygen_hidden"):
        return
    registry = er.async_get(hass)
    for d in BOILER_SENSORS:
        if not d.needs_lambda:
            continue
        entity_id = registry.async_get_entity_id("sensor", DOMAIN, f"{entry.unique_id or entry.entry_id}_{d.key}")
        entry_reg = registry.async_get(entity_id) if entity_id else None
        if entry_reg is not None and entry_reg.disabled_by is None:
            registry.async_update_entity(entity_id, disabled_by=er.RegistryEntryDisabler.INTEGRATION)
    pellets.flags["oxygen_hidden"] = True


class BoilerSensor(BurnerificEntity[BoilerCoordinator], SensorEntity):
    entity_description: BoilerSensorDescription

    def __init__(self, boiler: BoilerCoordinator, description: BoilerSensorDescription, enabled: bool = True) -> None:
        super().__init__(boiler, boiler, description)
        self._attr_entity_registry_enabled_default = enabled

    @property
    def native_value(self) -> Any:
        return self.entity_description.value_fn(self.coordinator.data) if self.coordinator.data else None


class HeatSensor(BurnerificEntity[BoilerCoordinator], SensorEntity):
    """Heat from burned pellets, for the Energy dashboard (gross: before boiler losses)."""

    def __init__(self, boiler: BoilerCoordinator, kwh_per_kg: float) -> None:
        super().__init__(
            boiler,
            boiler,
            SensorEntityDescription(
                key="fuel_energy",
                translation_key="fuel_energy",
                device_class=SensorDeviceClass.ENERGY,
                native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
                state_class=SensorStateClass.TOTAL_INCREASING,
                suggested_display_precision=0,
            ),
        )
        self._kwh_per_kg = float(kwh_per_kg)

    @property
    def native_value(self) -> float | None:
        data = self.coordinator.data
        return None if data is None or data.total_kg is None else round(data.total_kg * self._kwh_per_kg, 1)


class PelletSensor(BurnerificEntity[PelletCoordinator], SensorEntity):
    entity_description: PelletSensorDescription
    # Charts and logs are for cards, not for the database.
    _unrecorded_attributes = frozenset({"daily", "last_events", "bands", "lots", "seasons"})

    def __init__(self, pellets: PelletCoordinator, boiler: BoilerCoordinator, description: PelletSensorDescription) -> None:
        super().__init__(pellets, boiler, description)
        if description.currency is not None:
            self._attr_native_unit_of_measurement = f"{pellets.hass.config.currency}{description.currency}"

    @property
    def native_value(self) -> Any:
        return self.entity_description.value_fn(self.coordinator.data) if self.coordinator.data else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        fn = self.entity_description.attrs_fn
        return fn(self.coordinator.data) if fn and self.coordinator.data else None
