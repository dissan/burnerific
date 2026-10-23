"""Hot water tank heated by the boiler (only when the controller has a tank sensor)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.water_heater import (
    WaterHeaterEntity,
    WaterHeaterEntityDescription,
    WaterHeaterEntityFeature,
)
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import BoilerCoordinator
from .entity import BurnerificEntity
from .nbe.model import num

if TYPE_CHECKING:
    from . import BurnerificConfigEntry

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant, entry: BurnerificConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    boiler = entry.runtime_data.boiler
    if boiler.data is not None and boiler.data.dhw_temp is not None:
        async_add_entities([HotWater(boiler)])


class HotWater(BurnerificEntity[BoilerCoordinator], WaterHeaterEntity):
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_min_temp = 20
    _attr_max_temp = 75

    def __init__(self, boiler: BoilerCoordinator) -> None:
        super().__init__(boiler, boiler, WaterHeaterEntityDescription(key="hot_water", translation_key="hot_water"))
        if boiler.can_write:
            self._attr_supported_features = WaterHeaterEntityFeature.TARGET_TEMPERATURE

    @property
    def current_temperature(self) -> float | None:
        return self.coordinator.data.dhw_temp if self.coordinator.data else None

    @property
    def target_temperature(self) -> float | None:
        data = self.coordinator.data
        if data is None:
            return None
        return num(data.raw.get("settings/hot_water/temp")) or data.dhw_ref

    @property
    def current_operation(self) -> str | None:
        data = self.coordinator.data
        return None if data is None else ("heating" if data.state == 7 or data.dhw_valve else "idle")

    async def async_set_temperature(self, **kwargs: Any) -> None:
        if (temp := kwargs.get(ATTR_TEMPERATURE)) is not None:
            await self.coordinator.async_write("hot_water", "temp", round(float(temp)))
