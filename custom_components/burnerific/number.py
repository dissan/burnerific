"""Numbers: correct the stock from the dashboard, pick a fill amount, and boiler setpoints (with PIN)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.number import (
    NumberDeviceClass,
    NumberEntity,
    NumberEntityDescription,
    NumberMode,
    RestoreNumber,
)
from homeassistant.const import EntityCategory, UnitOfMass, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import BoilerCoordinator
from .entity import BurnerificEntity
from .nbe.model import num
from .pellet_coordinator import PelletCoordinator

if TYPE_CHECKING:
    from . import BurnerificConfigEntry

PARALLEL_UPDATES = 1

SETPOINTS = (
    # key, group, setting, min, max
    ("boiler_setpoint", "boiler", "temp", 30.0, 85.0),
    ("dhw_setpoint", "hot_water", "temp", 20.0, 75.0),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: BurnerificConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    rt = entry.runtime_data
    entities: list[NumberEntity] = [
        FillAmount(rt.pellets, rt.boiler),
        StorageNumber(rt.pellets, rt.boiler),
    ]
    if not rt.pellets.tracks_silo_on_boiler or rt.boiler.can_write:
        entities.append(SiloNumber(rt.pellets, rt.boiler))
    if rt.boiler.can_write and rt.boiler.data is not None:
        for key, group, setting, lo, hi in SETPOINTS:
            if f"settings/{group}/{setting}" in rt.boiler.data.raw:
                entities.append(Setpoint(rt.boiler, key, group, setting, lo, hi))
    async_add_entities(entities)


def _kg_description(key: str, maximum: float, step: float = 1.0, category: EntityCategory | None = None) -> NumberEntityDescription:
    return NumberEntityDescription(
        key=key,
        translation_key=key,
        device_class=NumberDeviceClass.WEIGHT,
        native_unit_of_measurement=UnitOfMass.KILOGRAMS,
        native_min_value=0,
        native_max_value=maximum,
        native_step=step,
        mode=NumberMode.BOX,
        entity_category=category,
    )


class FillAmount(BurnerificEntity[PelletCoordinator], RestoreNumber):
    """How much the 'Fill silo' button puts in. 0 means 'as many whole bags as fit'."""

    def __init__(self, pellets: PelletCoordinator, boiler: BoilerCoordinator) -> None:
        super().__init__(pellets, boiler, _kg_description("fill_amount", 5000, 0.5, EntityCategory.CONFIG))

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last := await self.async_get_last_number_data()) and last.native_value is not None:
            self.coordinator.fill_amount_kg = float(last.native_value)

    @property
    def native_value(self) -> float:
        return self.coordinator.fill_amount_kg

    async def async_set_native_value(self, value: float) -> None:
        self.coordinator.fill_amount_kg = value
        self.async_write_ha_state()


class StorageNumber(BurnerificEntity[PelletCoordinator], NumberEntity):
    def __init__(self, pellets: PelletCoordinator, boiler: BoilerCoordinator) -> None:
        super().__init__(pellets, boiler, _kg_description("storage_set", 100000))

    @property
    def native_value(self) -> float:
        return round(self.coordinator.inventory.storage_kg, 1)

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.async_set_storage(value, None, note="dashboard")


class SiloNumber(BurnerificEntity[PelletCoordinator], NumberEntity):
    def __init__(self, pellets: PelletCoordinator, boiler: BoilerCoordinator) -> None:
        super().__init__(pellets, boiler, _kg_description("silo_set", 5000))

    @property
    def native_value(self) -> float | None:
        kg = self.coordinator.inventory.silo_kg
        return None if kg is None else round(kg, 1)

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.async_set_silo(value, note="dashboard")


class Setpoint(BurnerificEntity[BoilerCoordinator], NumberEntity):
    def __init__(self, boiler: BoilerCoordinator, key: str, group: str, setting: str, lo: float, hi: float) -> None:
        super().__init__(
            boiler,
            boiler,
            NumberEntityDescription(
                key=key,
                translation_key=key,
                device_class=NumberDeviceClass.TEMPERATURE,
                native_unit_of_measurement=UnitOfTemperature.CELSIUS,
                native_min_value=lo,
                native_max_value=hi,
                native_step=1,
                mode=NumberMode.BOX,
            ),
        )
        self._group = group
        self._setting = setting

    @property
    def native_value(self) -> float | None:
        data = self.coordinator.data
        return None if data is None else num(data.raw.get(f"settings/{self._group}/{self._setting}"))

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.async_write(self._group, self._setting, round(value))
