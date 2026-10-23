"""Silo fill logs: parse them, turn them into training data, and calibrate the controller's counter.

A fill log is what many pellet owners keep anyway: a date and how many sacks
went into the silo. Each fill replaces what burned since the previous one, so
consecutive fills bracket how much the house used in between. The silo is not
filled to exactly the same level every time, so single intervals are noisy;
grouping them into periods of a couple of weeks keeps that noise small.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date, timedelta

from .consumption import PeriodObs, Weather

MIN_PERIOD_DAYS = 14
MAX_GAP_DAYS = 45  # longer gaps mean the log is incomplete (or the boiler was off)

_DATE_DMY = re.compile(r"^(\d{1,2})[-./](\d{1,2})[-./](\d{2,4})$")
_DATE_ISO = re.compile(r"^(\d{4})[-./](\d{1,2})[-./](\d{1,2})$")


class FillLogError(ValueError):
    """A line of the fill log could not be understood."""


@dataclass(slots=True, frozen=True)
class Fill:
    day: date
    kg: float


def _parse_date(text: str) -> date:
    text = text.strip()
    if m := _DATE_ISO.match(text):
        y, mo, d = (int(x) for x in m.groups())
    elif m := _DATE_DMY.match(text):
        d, mo, y = (int(x) for x in m.groups())
        if y < 100:
            y += 2000
    else:
        raise FillLogError(f"not a date: {text!r}")
    try:
        return date(y, mo, d)
    except ValueError as err:
        raise FillLogError(f"not a date: {text!r}") from err


def parse_log(text: str, unit_kg: float) -> list[Fill]:
    """Parse lines like ``04-10-2025  6`` (day-month-year, amount) or ``2025-10-04;96``.

    ``unit_kg`` converts the amount: the bag size for sacks, 1 for kilograms.
    Blank lines and lines starting with ``#`` are ignored. Amounts may use a
    decimal comma. Fills on the same day are added together.
    """
    by_day: dict[date, float] = {}
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p for p in re.split(r"[\s;]+", line) if p]
        if len(parts) == 1 and "," in line:  # CSV "2025-10-04,96"; a lone comma is not a decimal here
            parts = [p.strip() for p in line.split(",", 1)]
        if len(parts) < 2:
            raise FillLogError(f"line {n}: expected a date and an amount")
        day = _parse_date(parts[0])
        try:
            amount = float(parts[1].replace(",", "."))
        except ValueError as err:
            raise FillLogError(f"line {n}: {parts[1]!r} is not an amount") from err
        if amount < 0:
            raise FillLogError(f"line {n}: amount cannot be negative")
        by_day[day] = by_day.get(day, 0.0) + amount * unit_kg
    return [Fill(d, kg) for d, kg in sorted(by_day.items())]


def periods(
    fills: Iterable[Fill],
    weather: Callable[[date], Weather | None],
    *,
    min_days: int = MIN_PERIOD_DAYS,
    max_gap: int = MAX_GAP_DAYS,
) -> list[PeriodObs]:
    """Turn fills into burn periods of at least ``min_days`` days.

    The pellets added at a fill are what burned since the previous fill. A
    period runs from one fill to a later one and holds everything added at
    the fills after its start, up to and including its end.
    """
    fills = sorted(fills, key=lambda f: f.day)
    out: list[PeriodObs] = []
    i = 0
    while i < len(fills) - 1:
        start = fills[i].day
        kg = 0.0
        j = i
        while j + 1 < len(fills):
            gap = (fills[j + 1].day - fills[j].day).days
            if gap > max_gap or gap <= 0:
                break
            j += 1
            kg += fills[j].kg
            if (fills[j].day - start).days >= min_days:
                break
        if j == i:
            i += 1
            continue
        end = fills[j].day
        days = (end - start).days
        dates = [start + timedelta(days=k) for k in range(days)]
        ws = [weather(d) for d in dates]
        out.append(PeriodObs(start, days, kg, tuple(ws) if all(w is not None for w in ws) else None, "fills"))  # type: ignore[arg-type]
        i = j
    return out


def daily_spread(fills: Iterable[Fill]) -> dict[date, float]:
    """kg per day, spreading each fill evenly over the days since the previous fill."""
    fills = sorted(fills, key=lambda f: f.day)
    out: dict[date, float] = {}
    for prev, cur in zip(fills, fills[1:], strict=False):
        days = (cur.day - prev.day).days
        if not 0 < days <= MAX_GAP_DAYS:
            continue
        for k in range(days):
            d = prev.day + timedelta(days=k)
            out[d] = out.get(d, 0.0) + cur.kg / days
    return out


@dataclass(slots=True, frozen=True)
class Calibration:
    """How the controller's burn counter compares with pellets actually put in.

    ``factor`` > 1 means the controller under-counts: real kg = counter kg × factor.
    """

    factor: float
    uncertainty: float  # ± on the factor, from how much the silo level can differ between fills
    filled_kg: float
    counted_kg: float
    source: str  # "fills" (Burnerific's own silo fills) or "log" (imported log vs monthly totals)


def calibrate_from_counter(
    fills: list[tuple[date, float, float]], silo_capacity: float | None, min_counted: float = 500.0
) -> Calibration | None:
    """From fills with the controller's counter read at each fill: ``(day, kg, counter_kg)``.

    Between the first and the last fill the boiler burned (counter_last - counter_first)
    by its own count, and we put in everything filled after the first fill. The two
    differ by at most the difference in silo level at the two fills.
    """
    fills = sorted(fills)
    if len(fills) < 3:
        return None
    counted = fills[-1][2] - fills[0][2]
    filled = sum(kg for _, kg, _ in fills[1:])
    if counted < min_counted or filled <= 0:
        return None
    slack = silo_capacity if silo_capacity else max(kg for _, kg, _ in fills)
    return Calibration(filled / counted, slack / counted, filled, counted, "fills")


def calibrate_from_months(
    fills: list[Fill], month_kg: Callable[[int, int], float | None], min_months: int = 3
) -> Calibration | None:
    """Imported fill log against the controller's monthly totals, over months the log fully covers."""
    spread = daily_spread(fills)
    if not spread:
        return None
    first, last = min(spread), max(spread)
    filled = counted = 0.0
    months = 0
    y, m = first.year, first.month
    while (y, m) <= (last.year, last.month):
        start = date(y, m, 1)
        nxt = date(y + (m == 12), m % 12 + 1, 1)
        days = [start + timedelta(days=k) for k in range((nxt - start).days)]
        ctrl = month_kg(y, m)
        if ctrl and all(d in spread for d in days):
            filled += sum(spread[d] for d in days)
            counted += ctrl
            months += 1
        y, m = nxt.year, nxt.month
    if months < min_months or counted <= 0:
        return None
    # Silo level at the month edges is unknown: roughly one fill's worth per month boundary pair.
    typical_fill = sum(f.kg for f in fills) / len(fills)
    return Calibration(filled / counted, typical_fill / counted, filled, counted, "log")
