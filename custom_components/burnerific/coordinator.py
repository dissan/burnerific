"""Polls the boiler."""

from __future__ import annotations

from datetime import timedelta
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL, DOMAIN
from .nbe import BoilerData, BoilerReader, NbeClient, NbeError

_LOGGER = logging.getLogger(__name__)


class BoilerCoordinator(DataUpdateCoordinator[BoilerData]):
    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, client: NbeClient) -> None:
        seconds = entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} boiler",
            update_interval=timedelta(seconds=seconds),
        )
        self.client = client
        self.reader = BoilerReader(client)

    @property
    def can_write(self) -> bool:
        return bool(self.client.pin)

    async def _async_update_data(self) -> BoilerData:
        try:
            return await self.reader.read(dt_util.now())
        except NbeError as err:
            raise UpdateFailed(f"Boiler did not answer: {err}") from err

    async def async_write(self, group: str, key: str, value: str | float | int) -> None:
        if not self.can_write:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="pin_required"
            )
        try:
            await self.client.write_setting(group, key, value)
        except NbeError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="write_failed",
                translation_placeholders={"setting": f"{group}.{key}", "error": str(err)},
            ) from err
        self.reader.invalidate()
        await self.async_request_refresh()
