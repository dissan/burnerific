"""Binary sensors: alarm, flame, pumps, and the two things you actually care about: order and refill."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import CONF_ORDER_LEAD_DAYS, DEFAULT_ORDER_LEAD_DAYS
from .coordinator import BoilerCoordinator
from .entity import BurnerificEntity
from .nbe import BoilerData
from .pellet_coordinator import PelletCoordinator, PelletState

if TYPE_CHECKING:
    from . import BurnerificConfigEntry

PARALLEL_UPDATES = 0
SILO_WARNING_DAYS = 2


@dataclass(frozen=True, kw_only=True)
class BoilerBinaryDescription(BinarySensorEntityDescription):
    value_fn: Callable[[BoilerData], bool | None]
    always: bool = False


BOILER_BINARY: tuple[BoilerBinaryDescription, ...] = (
    BoilerBinaryDescription(
        key="alarm",
        translation_key="alarm",
        device_class=BinarySensorDeviceClass.PROBLEM,
        value_fn=lambda d: d.alarm,
        always=True,
    ),
    BoilerBinaryDescription(
        key="burning",
        translation_key="burning",
        device_class=BinarySensorDeviceClass.HEAT,
        value_fn=lambda d: d.burning,
        always=True,
    ),
    BoilerBinaryDescription(key="igniting", translation_key="igniting", value_fn=lambda d: d.igniting, always=True),
    BoilerBinaryDescription(
        key="boiler_pump",
        translation_key="boiler_pump",
        device_class=BinarySensorDeviceClass.RUNNING,
        value_fn=lambda d: d.boiler_pump,
    ),
    BoilerBinaryDescription(
        key="house_pump",
        translation_key="house_pump",
        device_class=BinarySensorDeviceClass.RUNNING,
        value_fn=lambda d: d.house_pump,
    ),
    BoilerBinaryDescription(
        key="dhw_valve",
        translation_key="dhw_valve",
        device_class=BinarySensorDeviceClass.OPENING,
        value_fn=lambda d: d.dhw_valve,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: BurnerificConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    rt = entry.runtime_data
    data = rt.boiler.data
    entities: list[BinarySensorEntity] = [
        BoilerBinary(rt.boiler, d) for d in BOILER_BINARY if d.always or (data and d.value_fn(data) is not None)
    ]
    lead = int(entry.options.get(CONF_ORDER_LEAD_DAYS, DEFAULT_ORDER_LEAD_DAYS))
    entities += [OrderPellets(rt.pellets, rt.boiler, lead), RefillSilo(rt.pellets, rt.boiler)]
    async_add_entities(entities)


class BoilerBinary(BurnerificEntity[BoilerCoordinator], BinarySensorEntity):
    entity_description: BoilerBinaryDescription

    def __init__(self, boiler: BoilerCoordinator, description: BoilerBinaryDescription) -> None:
        super().__init__(boiler, boiler, description)

    @property
    def is_on(self) -> bool | None:
        return self.entity_description.value_fn(self.coordinator.data) if self.coordinator.data else None


class OrderPellets(BurnerificEntity[PelletCoordinator], BinarySensorEntity):
    """On when there is a 10 % or greater chance of running dry before a new delivery can arrive."""

    def __init__(self, pellets: PelletCoordinator, boiler: BoilerCoordinator, lead_days: int) -> None:
        super().__init__(
            pellets,
            boiler,
            BinarySensorEntityDescription(
                key="order_pellets", translation_key="order_pellets", device_class=BinarySensorDeviceClass.PROBLEM
            ),
        )
        self._lead = lead_days

    @property
    def is_on(self) -> bool | None:
        state: PelletState | None = self.coordinator.data
        if state is None or state.forecast is None:
            return None
        early = state.forecast.runout["p10"]
        return early is not None and early <= dt_util.now().date() + timedelta(days=self._lead)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        state: PelletState | None = self.coordinator.data
        early = state.forecast.runout["p10"] if state and state.forecast else None
        return {"lead_days": self._lead, "earliest_runout": early.isoformat() if early else None}


class RefillSilo(BurnerificEntity[PelletCoordinator], BinarySensorEntity):
    """On when the silo may be empty within two days and there is something in storage to fill it with."""

    def __init__(self, pellets: PelletCoordinator, boiler: BoilerCoordinator) -> None:
        super().__init__(
            pellets,
            boiler,
            BinarySensorEntityDescription(
                key="refill_silo", translation_key="refill_silo", device_class=BinarySensorDeviceClass.PROBLEM
            ),
        )

    @property
    def is_on(self) -> bool | None:
        state: PelletState | None = self.coordinator.data
        if state is None or state.forecast is None or state.inventory.silo_kg is None:
            return None
        early = state.forecast.silo_empty["p10"]
        soon = early is not None and early <= dt_util.now().date() + timedelta(days=SILO_WARNING_DAYS)
        return soon and state.inventory.storage_kg > 0
