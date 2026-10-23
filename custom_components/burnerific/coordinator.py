"""Polls the boiler, on the LAN or, as a fallback, through StokerCloud."""

from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL, DOMAIN, EVENT_CONTROLLER
from .nbe import BoilerData, BoilerReader, NbeClient, NbeError, NbeRefused
from .nbe.states import state_key
from .nbe.stokercloud import StokerCloud, StokerCloudError

_LOGGER = logging.getLogger(__name__)


class BoilerCoordinator(DataUpdateCoordinator[BoilerData]):
    config_entry: ConfigEntry

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, client: NbeClient, cloud: StokerCloud | None = None
    ) -> None:
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
        self.cloud = cloud
        self._on_cloud = False

    @property
    def can_write(self) -> bool:
        return bool(self.client.pin)

    @property
    def on_cloud(self) -> bool:
        return self._on_cloud

    async def _async_update_data(self) -> BoilerData:
        now = dt_util.now()
        try:
            data = await self.reader.read(now)
        except NbeError as err:
            if self.cloud is None:
                raise UpdateFailed(f"Boiler did not answer: {err}") from err
            try:
                data = await self.cloud.read(now)
            except StokerCloudError as cloud_err:
                raise UpdateFailed(f"Boiler did not answer ({err}), nor did StokerCloud ({cloud_err})") from cloud_err
            if not self._on_cloud:
                _LOGGER.warning("Boiler unreachable on the LAN (%s); reading StokerCloud instead", err)
            self._on_cloud = True
            return data
        if self._on_cloud:
            _LOGGER.info("Boiler answers on the LAN again")
        self._on_cloud = False
        self._fire_events(data)
        return data

    def _fire_events(self, data: BoilerData) -> None:
        """Controller event log entries become Home Assistant events (and logbook lines)."""
        if not data.events:
            return
        device = dr.async_get(self.hass).async_get_device(
            identifiers={(DOMAIN, self.config_entry.unique_id or self.config_entry.entry_id)}
        )
        for e in data.events:
            self.hass.bus.async_fire(
                EVENT_CONTROLLER,
                {
                    "entry_id": self.config_entry.entry_id,
                    "boiler": self.config_entry.title,
                    "device_id": device.id if device else None,
                    "time": e.at.isoformat(),
                    "kind": e.kind,
                    "code": e.code,
                    "before": e.before,
                    "after": e.after,
                    "before_state": state_key(e.before) if e.is_state_change else None,
                    "after_state": state_key(e.after) if e.is_state_change else None,
                    "raw": e.raw,
                },
            )

    async def async_write(self, group: str, key: str, value: str | float | int) -> None:
        if not self.can_write:
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="pin_required")
        try:
            await self.client.write_setting(group, key, value)
        except NbeRefused as err:
            # Is it the value or the PIN? A harmless write tells them apart.
            if not await self.client.validate_pin():
                self.config_entry.async_start_reauth(self.hass)
                raise HomeAssistantError(translation_domain=DOMAIN, translation_key="pin_wrong") from err
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="write_failed",
                translation_placeholders={"setting": f"{group}.{key}", "error": str(err)},
            ) from err
        except NbeError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="write_failed",
                translation_placeholders={"setting": f"{group}.{key}", "error": str(err)},
            ) from err
        self.reader.invalidate()
        await self.async_request_refresh()
