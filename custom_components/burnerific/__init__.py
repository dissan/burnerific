"""Burnerific: THE Home Assistant integration for NBE pellet boilers."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

from .const import CONF_PIN, CONF_SERIAL, DOMAIN
from .coordinator import BoilerCoordinator
from .nbe import NbeClient
from .pellet_coordinator import PelletCoordinator
from .services import async_setup_services

PLATFORMS = [Platform.BINARY_SENSOR, Platform.BUTTON, Platform.NUMBER, Platform.SENSOR]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


@dataclass(slots=True)
class BurnerificData:
    client: NbeClient
    boiler: BoilerCoordinator
    pellets: PelletCoordinator


type BurnerificConfigEntry = ConfigEntry[BurnerificData]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    async_setup_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: BurnerificConfigEntry) -> bool:
    client = NbeClient(
        entry.data[CONF_HOST],
        serial=entry.data.get(CONF_SERIAL),
        pin=entry.data.get(CONF_PIN) or None,
    )
    boiler = BoilerCoordinator(hass, entry, client)
    try:
        await boiler.async_config_entry_first_refresh()
    except Exception:
        await client.close()
        raise
    pellets = PelletCoordinator(hass, entry, boiler)
    await pellets.async_load()
    await pellets.async_config_entry_first_refresh()

    entry.runtime_data = BurnerificData(client, boiler, pellets)
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: BurnerificConfigEntry) -> bool:
    if ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        await entry.runtime_data.pellets.async_unload()
        await entry.runtime_data.client.close()
    return ok


async def _async_reload(hass: HomeAssistant, entry: BurnerificConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
