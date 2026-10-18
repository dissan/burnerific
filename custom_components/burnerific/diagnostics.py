"""Diagnostics download: raw controller keys, model and stock, PIN redacted."""

from __future__ import annotations

from dataclasses import asdict
from typing import TYPE_CHECKING, Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from .const import CONF_PIN

if TYPE_CHECKING:
    from . import BurnerificConfigEntry

REDACT = {CONF_PIN, "rsa_key", "misc/rsa_key", "settings/misc/rsa_key", "settings/misc/password"}


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: BurnerificConfigEntry) -> dict[str, Any]:
    rt = entry.runtime_data
    data = rt.boiler.data
    pellets = rt.pellets
    return {
        "entry": async_redact_data(dict(entry.data), REDACT),
        "options": dict(entry.options),
        "controller": asdict(rt.client.info) if rt.client.info else None,
        "raw": async_redact_data(dict(data.raw), REDACT) if data else None,
        "inventory": pellets.inventory.to_dict(),
        "model": pellets.model.describe() if pellets.model else None,
        "climate": pellets.climate.to_dict(),
        "history_days": len(pellets.history.days),
        "forecast": None if pellets.result is None else {
            "runout": {k: v.isoformat() if v else None for k, v in pellets.result.runout.items()},
            "days_left": pellets.result.days_left,
            "kg_next_30": pellets.result.kg_next_30,
        },
    }
