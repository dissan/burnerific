"""Controller event log entries in Home Assistant's logbook."""

from __future__ import annotations

from collections.abc import Callable

from homeassistant.components.logbook import LOGBOOK_ENTRY_MESSAGE, LOGBOOK_ENTRY_NAME
from homeassistant.core import Event, HomeAssistant, callback

from .const import DOMAIN, EVENT_ALARM, EVENT_CONTROLLER

# The logbook shows text as-is, so these mirror the English state names.
LABELS = {
    "waiting": "waiting",
    "ignition_1": "ignition 1",
    "ignition_2": "ignition 2",
    "power": "burning",
    "pause": "pause",
    "hot_water": "hot water",
    "stopped_temp_reached": "stopped (temperature reached)",
    "summer_stop": "summer stop",
    "off": "off",
    "stopped_timer": "stopped by timer",
    "stopped_weather": "stopped by weather compensation",
    "stopped_external_temp": "stopped by external temperature",
    "stopped_external_contact": "stopped by external contact",
    "door_open": "door open",
}


def _label(key: str | None, code: int | None) -> str:
    if key is None:
        return "?" if code is None else f"state {code}"
    return LABELS.get(key, key.replace("_", " "))


@callback
def async_describe_events(
    hass: HomeAssistant, async_describe_event: Callable[[str, str, Callable[[Event], dict[str, str]]], None]
) -> None:
    @callback
    def describe_controller(event: Event) -> dict[str, str]:
        d = event.data
        if d.get("before_state") is not None or d.get("after_state") is not None:
            message = f"changed from {_label(d.get('before_state'), d.get('before'))} to {_label(d.get('after_state'), d.get('after'))}"
        else:
            message = f"logged event {d.get('raw')}"
        return {LOGBOOK_ENTRY_NAME: d.get("boiler") or "NBE boiler", LOGBOOK_ENTRY_MESSAGE: message}

    @callback
    def describe_alarm(event: Event) -> dict[str, str]:
        d = event.data
        return {
            LOGBOOK_ENTRY_NAME: d.get("boiler") or "NBE boiler",
            LOGBOOK_ENTRY_MESSAGE: f"raised alarm {d.get('state')} ({_label(d.get('state_key'), d.get('state'))})",
        }

    async_describe_event(DOMAIN, EVENT_CONTROLLER, describe_controller)
    async_describe_event(DOMAIN, EVENT_ALARM, describe_alarm)
