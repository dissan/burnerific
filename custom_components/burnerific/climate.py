"""The boiler as a thermostat: boiler water setpoint, on/off, and what it is doing right now."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityDescription,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import BoilerCoordinator
from .entity import BurnerificEntity
from .nbe.model import num
from .nbe.states import IGNITION

if TYPE_CHECKING:
    from . import BurnerificConfigEntry

PARALLEL_UPDATES = 1

OFF_STATES = {14}
IDLE_STATES = {6, 9, 10, 22, 23, 24, 25}  # pause, temperature reached, summer stop, stopped by ...


async def async_setup_entry(
    hass: HomeAssistant, entry: BurnerificConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    async_add_entities([BoilerClimate(entry.runtime_data.boiler)])


class BoilerClimate(BurnerificEntity[BoilerCoordinator], ClimateEntity):
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_hvac_modes = [HVACMode.HEAT, HVACMode.OFF]
    _attr_min_temp = 30
    _attr_max_temp = 85
    _attr_target_temperature_step = 1
    _enable_turn_on_off_backwards_compatibility = False

    def __init__(self, boiler: BoilerCoordinator) -> None:
        super().__init__(boiler, boiler, ClimateEntityDescription(key="boiler", translation_key="boiler"))
        if boiler.can_write:
            self._attr_supported_features = (
                ClimateEntityFeature.TARGET_TEMPERATURE | ClimateEntityFeature.TURN_ON | ClimateEntityFeature.TURN_OFF
            )

    @property
    def current_temperature(self) -> float | None:
        return self.coordinator.data.boiler_temp if self.coordinator.data else None

    @property
    def target_temperature(self) -> float | None:
        data = self.coordinator.data
        if data is None:
            return None
        return num(data.raw.get("settings/boiler/temp")) or data.boiler_ref

    @property
    def hvac_mode(self) -> HVACMode | None:
        data = self.coordinator.data
        if data is None or data.state is None:
            return None
        return HVACMode.OFF if data.state in OFF_STATES else HVACMode.HEAT

    @property
    def hvac_action(self) -> HVACAction | None:
        data = self.coordinator.data
        if data is None or data.state is None:
            return None
        if data.state in OFF_STATES:
            return HVACAction.OFF
        if data.burning or data.state in IGNITION:
            return HVACAction.HEATING
        if data.state in IDLE_STATES or not data.alarm:
            return HVACAction.IDLE
        return HVACAction.OFF

    async def async_set_temperature(self, **kwargs: Any) -> None:
        if (temp := kwargs.get(ATTR_TEMPERATURE)) is not None:
            await self.coordinator.async_write("boiler", "temp", round(float(temp)))

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        await self.coordinator.async_write("misc", "stop" if hvac_mode == HVACMode.OFF else "start", 1)

    async def async_turn_on(self) -> None:
        await self.async_set_hvac_mode(HVACMode.HEAT)

    async def async_turn_off(self) -> None:
        await self.async_set_hvac_mode(HVACMode.OFF)
