"""Burnerific: THE Home Assistant integration for NBE pellet boilers."""

from __future__ import annotations

import json
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path

from homeassistant.components.frontend import add_extra_js_url
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.helpers.typing import ConfigType
from homeassistant.setup import async_when_setup

from .const import (
    CONF_PIN,
    CONF_RESTOCK_DATE,
    CONF_SEASON_END,
    CONF_SERIAL,
    CONF_STOKERCLOUD_USER,
    DOMAIN,
    STORAGE_VERSION,
)
from .coordinator import BoilerCoordinator
from .nbe import NbeClient
from .nbe.stokercloud import StokerCloud
from .pellet_coordinator import PelletCoordinator
from .services import async_setup_services

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.CALENDAR,
    Platform.CLIMATE,
    Platform.NUMBER,
    Platform.SENSOR,
    Platform.WATER_HEATER,
]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

CARD_URL = f"/{DOMAIN}/burnerific-card.js"
HERE = Path(__file__).parent


@dataclass(slots=True)
class BurnerificData:
    client: NbeClient
    boiler: BoilerCoordinator
    pellets: PelletCoordinator


type BurnerificConfigEntry = ConfigEntry[BurnerificData]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    async_setup_services(hass)
    await _register_card(hass)
    return True


async def _register_card(hass: HomeAssistant) -> None:
    """Serve the Lovelace card and load it on every dashboard."""
    if hass.http is None:  # e.g. some test setups
        return
    await hass.http.async_register_static_paths([StaticPathConfig(CARD_URL, str(HERE / "www" / "burnerific-card.js"), True)])
    version = await hass.async_add_executor_job(_version)

    async def _add_to_dashboards(hass: HomeAssistant, _component: str) -> None:
        add_extra_js_url(hass, f"{CARD_URL}?v={version}")  # the version busts browser caches on upgrade

    # Whenever the frontend is (or becomes) ready; it may start after us, or not at all in tests.
    async_when_setup(hass, "frontend", _add_to_dashboards)


def _version() -> str:
    return json.loads((HERE / "manifest.json").read_text(encoding="utf-8")).get("version", "0")


async def async_setup_entry(hass: HomeAssistant, entry: BurnerificConfigEntry) -> bool:
    client = NbeClient(
        entry.data[CONF_HOST],
        serial=entry.data.get(CONF_SERIAL),
        pin=entry.data.get(CONF_PIN) or None,
    )
    cloud = None
    if user := entry.options.get(CONF_STOKERCLOUD_USER):
        cloud = StokerCloud(async_get_clientsession(hass), user)
    boiler = BoilerCoordinator(hass, entry, client, cloud)
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
    await hass.async_add_executor_job(_install_blueprints, hass.config.path("blueprints", "automation", DOMAIN))
    return True


def _install_blueprints(target: str) -> None:
    """Copy our automation blueprints into the config folder, never overwriting the user's edits."""
    source = HERE / "blueprints"
    dest = Path(target)
    dest.mkdir(parents=True, exist_ok=True)
    for file in source.glob("*.yaml"):
        if not (dest / file.name).exists():
            shutil.copyfile(file, dest / file.name)


async def async_unload_entry(hass: HomeAssistant, entry: BurnerificConfigEntry) -> bool:
    if ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        await entry.runtime_data.pellets.async_unload()
        await entry.runtime_data.client.close()
    return ok


async def async_remove_entry(hass: HomeAssistant, entry: BurnerificConfigEntry) -> None:
    """Delete the stock ledger and history of a removed boiler."""
    await Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}").async_remove()
    ir.async_delete_issue(hass, DOMAIN, f"alarm_{entry.entry_id}")


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """v1 -> v2: "heating season ends" became "next restock date"."""
    if entry.version > 2:
        return False
    if entry.version == 1:
        options = dict(entry.options)
        if CONF_SEASON_END in options:
            options[CONF_RESTOCK_DATE] = options.pop(CONF_SEASON_END)
        hass.config_entries.async_update_entry(entry, options=options, version=2)
        _LOGGER.debug("Migrated %s to version 2", entry.title)
    return True


async def _async_reload(hass: HomeAssistant, entry: BurnerificConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
