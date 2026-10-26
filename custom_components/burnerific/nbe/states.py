"""Controller state and substate codes, as shown on the boiler's display.

Codes map to stable keys; the human texts live in the integration's
translations so Home Assistant shows them in the user's language.
"""

from __future__ import annotations

STATES: dict[int, str] = {
    0: "waiting",
    1: "ignition_1",
    2: "ignition_1",
    3: "ignition_2",
    4: "ignition_2",
    5: "power",
    6: "pause",
    7: "hot_water",
    8: "alarm_low_boiler_temp",
    9: "stopped_temp_reached",
    10: "summer_stop",
    11: "alarm_hot_burner",
    12: "alarm_burner_plug",
    13: "alarm_ignition",
    14: "off",
    15: "alarm_boiler_sensor",
    16: "alarm_photo_sensor",
    17: "alarm_shaft_sensor",
    19: "alarm_auger_output",
    20: "alarm_no_flame_high",
    22: "stopped_external_temp",
    23: "stopped_timer",
    24: "stopped_external_contact",
    25: "stopped_weather",
    26: "alarm_fan_rpm",
    27: "alarm_no_flame_low",
    28: "door_open",
    29: "alarm_overheat",
    30: "alarm_cascade",
    31: "alarm_compressor",
    36: "alarm_back_pressure",
    37: "alarm_air_flow",
    38: "alarm_exhaust_temp",
    39: "alarm_ash_drawer",
    40: "air_flow_calibration",
    41: "alarm_overpressure",
    42: "alarm_oxygen_sensor",
    43: "compressor_cleaning",
    44: "alarm_ignition_failed",
    45: "alarm_condensate",
}

SUBSTATES: dict[int, str] = {
    0: "none",
    1: "purging",
    2: "ignition_fuel",
    3: "igniter_pulse",
    4: "electric_ignition",
    5: "cooling_boiler",
    6: "extinguishing",
    7: "fan_cleaning",
    8: "ash_removal",
    15: "external_contact",
    16: "auger_after_run",
    17: "residual_heat_to_dhw",
    18: "external_filling",
}

STATE_OPTIONS = sorted(set(STATES.values()) | {"unknown"})
SUBSTATE_OPTIONS = sorted(set(SUBSTATES.values()) | {"unknown"})

IGNITION = frozenset({1, 2, 3, 4})


def state_key(code: int | None) -> str | None:
    if code is None:
        return None
    return STATES.get(code, "unknown")


def substate_key(code: int | None) -> str | None:
    if code is None:
        return None
    return SUBSTATES.get(code, "unknown")


def is_alarm(code: int | None) -> bool:
    return code is not None and STATES.get(code, "").startswith("alarm_")
