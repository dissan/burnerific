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
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import CONF_KWH_PER_KG, DEFAULT_KWH_PER_KG
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


@dataclass(frozen=True, kw_only=True)
class PelletSensorDescription(SensorEntityDescription):
    value_fn: Callable[[PelletState], Any]
    attrs_fn: Callable[[PelletState], dict[str, Any]] | None = None
    currency: bool = False


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
    _pct("oxygen", lambda d: d.oxygen),
    _pct("oxygen_ref", lambda d: d.oxygen_ref, entity_category=EntityCategory.DIAGNOSTIC),
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
)


def _q(state: PelletState, field: str) -> dict[str, Any]:
    f = state.forecast
    if f is None:
        return {}
    values = getattr(f, field)
    return {f"{field}_{k}": (v.isoformat() if isinstance(v, date) else v) for k, v in values.items()}


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
        currency=True,
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
        currency=True,
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
        attrs_fn=lambda s: _q(s, "runout") | _q(s, "days_left"),
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
        },
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: BurnerificConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    rt = entry.runtime_data
    data = rt.boiler.data
    entities: list[SensorEntity] = [
        BoilerSensor(rt.boiler, d) for d in BOILER_SENSORS if d.always or (data and d.value_fn(data) is not None)
    ]
    entities += [PelletSensor(rt.pellets, rt.boiler, d) for d in PELLET_SENSORS]
    if data is not None and data.total_kg is not None:
        entities.append(HeatSensor(rt.boiler, entry.options.get(CONF_KWH_PER_KG, DEFAULT_KWH_PER_KG)))
    async_add_entities(entities)


class BoilerSensor(BurnerificEntity[BoilerCoordinator], SensorEntity):
    entity_description: BoilerSensorDescription

    def __init__(self, boiler: BoilerCoordinator, description: BoilerSensorDescription) -> None:
        super().__init__(boiler, boiler, description)

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
    _unrecorded_attributes = frozenset({"daily", "last_events"})

    def __init__(self, pellets: PelletCoordinator, boiler: BoilerCoordinator, description: PelletSensorDescription) -> None:
        super().__init__(pellets, boiler, description)
        if description.currency:
            currency = pellets.hass.config.currency
            self._attr_native_unit_of_measurement = currency if description.key == "stock_value" else f"{currency}/kg"

    @property
    def native_value(self) -> Any:
        return self.entity_description.value_fn(self.coordinator.data) if self.coordinator.data else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        fn = self.entity_description.attrs_fn
        return fn(self.coordinator.data) if fn and self.coordinator.data else None
