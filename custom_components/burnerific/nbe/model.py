"""Normalized boiler reading built from the controller's flat key space."""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from .client import CONSUMPTION_KEYS, NbeClient, NbeError, NbeRefused
from .eventlog import LogEntry
from .states import IGNITION, is_alarm

_LOGGER = logging.getLogger(__name__)

NO_SENSOR = 999.9

# Settings groups read on every slow cycle. Others are read once for diagnostics.
SLOW_GROUPS = ("boiler", "hot_water", "hopper", "auger", "regulation", "weather", "oxygen", "ignition", "misc")


def num(value: object) -> float | None:
    """NBE number -> float. ``None`` for empty, junk and the 999.9 "no sensor" marker."""
    if value is None or value == "":
        return None
    try:
        n = float(str(value).replace(",", "."))
    except ValueError:
        return None
    if math.isnan(n) or abs(n - NO_SENSOR) < 0.01 or abs(n + NO_SENSOR) < 0.01:
        return None
    return n


def flag(value: object) -> bool | None:
    n = num(value)
    return None if n is None else n != 0


def series(value: str | None) -> list[float]:
    """``"8.7,7.5,,3"`` -> ``[8.7, 7.5, 0.0, 3.0]``. NBE leaves empty slots for no burn."""
    if not value:
        return []
    return [num(v) or 0.0 for v in value.split(",")]


@dataclass(slots=True)
class BoilerData:
    raw: dict[str, str]
    at: datetime

    state: int | None = None
    substate: int | None = None
    substate_sec: float | None = None
    power_pct: float | None = None
    power_kw: float | None = None
    boiler_temp: float | None = None
    boiler_ref: float | None = None
    return_temp: float | None = None
    smoke_temp: float | None = None
    shaft_temp: float | None = None
    dhw_temp: float | None = None
    dhw_ref: float | None = None
    outdoor_temp: float | None = None
    oxygen: float | None = None
    oxygen_ref: float | None = None
    photo_level: float | None = None
    fan_speed: float | None = None
    exhaust_speed: float | None = None
    boiler_pump: bool | None = None
    dhw_valve: bool | None = None
    house_pump: bool | None = None

    hopper_kg: float | None = None
    auger_capacity_g: float | None = None
    total_kg: float | None = None
    dhw_kg_today: float | None = None
    days_kg: list[float] = field(default_factory=list)
    months_kg: list[float] = field(default_factory=list)
    hours_kg: list[float] = field(default_factory=list)
    dhw_days_kg: list[float] = field(default_factory=list)

    lambda_fitted: bool | None = None  # O₂ regulation on: the controller trusts its lambda sensor
    ignitions: int | None = None  # lifetime ignition counter
    source: str = "local"  # "local" (UDP) or "cloud" (StokerCloud fallback)
    events: list = field(default_factory=list)  # new controller event log entries since last read

    @property
    def alarm(self) -> bool:
        return is_alarm(self.state)

    @property
    def igniting(self) -> bool:
        return self.state in IGNITION

    @property
    def burning(self) -> bool:
        return not self.alarm and (self.power_pct or 0) > 0

    def kg_on(self, day: date) -> float | None:
        """Consumption for a day from the controller's rolling day array.

        Index ``n`` holds day ``n + 1`` of the current month up to today, and
        day ``n + 1`` of the previous month after that (not yet overwritten).
        """
        today = self.at.date()
        if day > today:
            return None
        i = day.day - 1
        if not 0 <= i < len(self.days_kg):
            return None
        if (day.year, day.month) == (today.year, today.month):
            return self.days_kg[i]
        prev = today.replace(day=1) - timedelta(days=1)
        if (day.year, day.month) == (prev.year, prev.month) and day.day > today.day:
            return self.days_kg[i]
        return None

    def dhw_kg_on(self, day: date) -> float | None:
        """Hot water share of a day, from the rolling hot water day array (same layout as ``kg_on``)."""
        if not any(self.dhw_days_kg):
            return None
        today = self.at.date()
        i = day.day - 1
        if day > today or not 0 <= i < len(self.dhw_days_kg):
            return None
        prev = today.replace(day=1) - timedelta(days=1)
        if (day.year, day.month) == (today.year, today.month) or (
            (day.year, day.month) == (prev.year, prev.month) and day.day > today.day
        ):
            return self.dhw_days_kg[i]
        return None

    def kg_in_month(self, year: int, month: int) -> float | None:
        """A closed month from the rolling 12-month array (index 0 = January)."""
        today = self.at.date()
        if len(self.months_kg) < 12 or (year, month) >= (today.year, today.month):
            return None
        if year == today.year or (year == today.year - 1 and month > today.month):
            return self.months_kg[month - 1]
        return None

    @classmethod
    def from_raw(cls, raw: dict[str, str], at: datetime) -> BoilerData:
        o = lambda k: raw.get(f"operating_data/{k}")  # noqa: E731
        a = lambda k: raw.get(f"advanced_data/{k}")  # noqa: E731
        s = lambda k: raw.get(f"settings/{k}")  # noqa: E731
        c = lambda k: raw.get(f"consumption_data/{k}")  # noqa: E731
        state = num(o("state"))
        substate = num(o("substate"))
        dhw_temp = num(o("dhw_temp"))
        dhw_days = series(c("dhw_days"))
        return cls(
            raw=raw,
            at=at,
            state=int(state) if state is not None else None,
            substate=int(substate) if substate is not None else None,
            substate_sec=num(o("substate_sec")),
            power_pct=num(o("power_pct")),
            power_kw=num(o("power_kw")),
            boiler_temp=num(o("boiler_temp")),
            boiler_ref=num(o("boiler_ref")) or num(s("boiler/temp")),
            return_temp=num(o("return_temp")),
            smoke_temp=num(o("smoke_temp")),
            shaft_temp=num(o("shaft_temp")),
            dhw_temp=dhw_temp,
            dhw_ref=num(o("dhw_ref")) if dhw_temp is not None else None,
            outdoor_temp=num(o("external_temp")) if num(o("external_temp")) is not None else num(a("outdoor_temp")),
            oxygen=num(o("oxygen")),
            oxygen_ref=num(o("oxygen_ref")),
            photo_level=num(o("photo_level")),
            fan_speed=num(o("fan_speed")),
            exhaust_speed=num(o("exhaust_speed")),
            boiler_pump=flag(o("boiler_pump_state")),
            dhw_valve=flag(o("dhw_valve_state")),
            house_pump=flag(o("house_pump_state")),
            # operating_data/content is a scaled counter; the hopper setting is the kg the display shows.
            hopper_kg=num(s("hopper/content")),
            auger_capacity_g=num(s("hopper/auger_capacity")),
            total_kg=num(c("counter")),
            dhw_kg_today=dhw_days[at.day - 1] if len(dhw_days) >= at.day else None,
            days_kg=series(c("total_days")),
            months_kg=series(c("total_months")),
            hours_kg=series(c("total_hours")),
            dhw_days_kg=dhw_days,
            lambda_fitted=None if s("oxygen/regulation") is None else flag(s("oxygen/regulation")),
            ignitions=int(n) if (n := num(s("ignition/ignition_number"))) is not None else None,
        )


def _prefixed(prefix: str, pairs: dict[str, str]) -> dict[str, str]:
    return {f"{prefix}{k}": v for k, v in pairs.items()}


class BoilerReader:
    """Reads fast data every poll and slow data (settings, consumption) every ``slow_every`` polls."""

    def __init__(self, client: NbeClient, slow_every: int = 5) -> None:
        self.client = client
        self.slow_every = max(1, slow_every)
        self._slow: dict[str, str] = {}
        self._polls = 0
        self._missing: set[str] = set()
        self.event_bookmark: datetime | None = None

    def invalidate(self) -> None:
        """Force a slow read next time, e.g. after writing a setting."""
        self._polls = 0

    async def read(self, now: datetime) -> BoilerData:
        c = self.client
        if c.info is None:
            await c.discover()
        fast = _prefixed("operating_data/", await c.operating_data())
        try:
            fast |= _prefixed("advanced_data/", await c.advanced_data())
        except NbeError as err:
            _LOGGER.debug("advanced data unavailable: %s", err)
        events: list[LogEntry] = []
        if self._polls % self.slow_every == 0:
            await self._read_slow()
            events = await self._read_events(now)
        self._polls += 1
        data = BoilerData.from_raw(self._slow | fast, now)
        data.events = events
        return data

    async def _read_events(self, now: datetime) -> list[LogEntry]:
        """New event log entries since the last read. The first read only sets the bookmark."""
        if "event_log" in self._missing:
            return []
        try:
            entries = await self.client.event_log(now)
        except NbeError as err:
            if isinstance(err, NbeRefused):
                self._missing.add("event_log")
            _LOGGER.debug("event log unavailable: %s", err)
            return []
        if not entries:
            return []
        newest = max(e.at for e in entries)
        if self.event_bookmark is None:
            self.event_bookmark = newest
            return []
        fresh = sorted((e for e in entries if e.at > self.event_bookmark), key=lambda e: e.at)
        self.event_bookmark = max(newest, self.event_bookmark)
        return fresh

    async def _read_slow(self) -> None:
        slow: dict[str, str] = {}
        for group in SLOW_GROUPS:
            if f"settings/{group}" in self._missing:
                continue
            try:
                slow |= _prefixed(f"settings/{group}/", await self.client.settings(group))
            except NbeError as err:
                # Not every controller has every group. Stop asking for groups it refuses.
                if isinstance(err, NbeRefused):
                    self._missing.add(f"settings/{group}")
                _LOGGER.debug("settings %s unavailable: %s", group, err)
        for key in CONSUMPTION_KEYS:
            if f"consumption/{key}" in self._missing:
                continue
            try:
                slow |= _prefixed("consumption_data/", await self.client.consumption(key))
            except NbeError as err:
                if isinstance(err, NbeRefused):
                    self._missing.add(f"consumption/{key}")
                _LOGGER.debug("consumption %s unavailable: %s", key, err)
        self._slow |= slow
