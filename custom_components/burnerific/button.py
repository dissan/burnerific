"""Buttons: fill the silo from storage, and start/stop/reset the boiler (with PIN)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN, TASKS
from .coordinator import BoilerCoordinator
from .entity import BurnerificEntity
from .pellet_coordinator import PelletCoordinator

if TYPE_CHECKING:
    from . import BurnerificConfigEntry

PARALLEL_UPDATES = 1

COMMANDS = (
    ("start", "misc", "start"),
    ("stop", "misc", "stop"),
    ("reset_alarm", "misc", "reset_alarm"),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: BurnerificConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    rt = entry.runtime_data
    entities: list[ButtonEntity] = [FillSiloButton(rt.pellets, rt.boiler)]
    entities += [MaintenanceDoneButton(rt.pellets, rt.boiler, task) for task in TASKS]
    if rt.boiler.can_write:
        entities += [CommandButton(rt.boiler, key, group, setting) for key, group, setting in COMMANDS]
    async_add_entities(entities)


class FillSiloButton(BurnerificEntity[PelletCoordinator], ButtonEntity):
    def __init__(self, pellets: PelletCoordinator, boiler: BoilerCoordinator) -> None:
        super().__init__(pellets, boiler, ButtonEntityDescription(key="fill_silo", translation_key="fill_silo"))

    async def async_press(self) -> None:
        pellets = self.coordinator
        kg = pellets.fill_amount_kg or pellets.inventory.suggested_fill_kg()
        if kg <= 0:
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="nothing_to_fill")
        await pellets.async_fill_silo(kg, None, True, "button")


class CommandButton(BurnerificEntity[BoilerCoordinator], ButtonEntity):
    def __init__(self, boiler: BoilerCoordinator, key: str, group: str, setting: str) -> None:
        super().__init__(boiler, boiler, ButtonEntityDescription(key=key, translation_key=key))
        self._group = group
        self._setting = setting

    async def async_press(self) -> None:
        await self.coordinator.async_write(self._group, self._setting, 1)


class MaintenanceDoneButton(BurnerificEntity[PelletCoordinator], ButtonEntity):
    """Press after emptying the ash drawer, cleaning, the sweep's visit or a service."""

    def __init__(self, pellets: PelletCoordinator, boiler: BoilerCoordinator, task: str) -> None:
        super().__init__(pellets, boiler, ButtonEntityDescription(key=f"{task}_done", translation_key=f"{task}_done"))
        self._task = task

    async def async_press(self) -> None:
        await self.coordinator.async_maintenance_done(self._task)
