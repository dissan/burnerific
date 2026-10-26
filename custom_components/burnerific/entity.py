"""Shared entity base: one device per boiler, boiler and pellet entities on it."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityDescription
from homeassistant.helpers.update_coordinator import CoordinatorEntity, DataUpdateCoordinator

from .const import CONF_SERIAL, DOMAIN, MANUFACTURER
from .coordinator import BoilerCoordinator


def device_info(boiler: BoilerCoordinator) -> DeviceInfo:
    entry = boiler.config_entry
    info = boiler.client.info
    return DeviceInfo(
        identifiers={(DOMAIN, entry.unique_id or entry.entry_id)},
        manufacturer=MANUFACTURER,
        model=info.model if info and info.model else None,
        sw_version=info.version if info else None,
        serial_number=entry.data.get(CONF_SERIAL),
        name=entry.title,
    )


class BurnerificEntity[C: DataUpdateCoordinator](CoordinatorEntity[C]):
    _attr_has_entity_name = True

    def __init__(self, coordinator: C, boiler: BoilerCoordinator, description: EntityDescription) -> None:
        super().__init__(coordinator)
        entry = coordinator.config_entry
        self.entity_description = description
        self._attr_unique_id = f"{entry.unique_id or entry.entry_id}_{description.key}"
        self._attr_device_info = device_info(boiler)
