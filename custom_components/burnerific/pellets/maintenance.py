"""Maintenance that follows how much the boiler burns, not just the calendar.

Pellet boilers need the ash drawer emptied, the heat exchanger cleaned, the
chimney swept and the burner serviced. The first two scale with kilograms
burned (a cold January fills the ash drawer three times faster than October),
the last two are mostly yearly. Each task here can be due by kg, by days, or
by whichever comes first, and its due date is predicted from the burn forecast.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from typing import Any


@dataclass(slots=True, frozen=True)
class TaskSpec:
    key: str
    interval_kg: float | None = None
    interval_days: int | None = None


@dataclass(slots=True)
class TaskState:
    last_done: str | None = None  # ISO date
    burned_at: float | None = None  # our cumulative burn counter when it was done


@dataclass(slots=True, frozen=True)
class TaskStatus:
    key: str
    kg_since: float | None
    kg_left: float | None
    fraction: float | None  # 0 = just done, 1 = due
    due_date: date | None
    due: bool
    last_done: date | None

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["due_date"] = self.due_date.isoformat() if self.due_date else None
        d["last_done"] = self.last_done.isoformat() if self.last_done else None
        return d


def ash_interval_kg(ash_capacity_kg: float, ash_pct: float) -> float | None:
    """Kilograms of pellets that fill the ash drawer. Good pellets leave 0.5-0.7 % ash, poor ones over 1 %."""
    if ash_capacity_kg <= 0 or ash_pct <= 0:
        return None
    return ash_capacity_kg / (ash_pct / 100.0)


def _date_for_kg(kg_left: float, today: date, expected_kg: Sequence[float]) -> date | None:
    """Walk the expected daily burn until ``kg_left`` is used up."""
    if kg_left <= 0:
        return today
    acc = 0.0
    for i, kg in enumerate(expected_kg):
        acc += kg
        if acc >= kg_left:
            return today + timedelta(days=i)
    return None


def status(
    spec: TaskSpec,
    state: TaskState,
    *,
    burned_total: float | None,
    today: date,
    expected_kg: Sequence[float],
) -> TaskStatus:
    """Where a task stands. ``expected_kg`` is the expected burn per day from today on."""
    last = date.fromisoformat(state.last_done) if state.last_done else None
    kg_since = None
    if burned_total is not None and state.burned_at is not None:
        kg_since = max(0.0, burned_total - state.burned_at)

    fractions: list[float] = []
    dates: list[date] = []
    kg_left = None
    if spec.interval_kg and kg_since is not None:
        kg_left = spec.interval_kg - kg_since
        fractions.append(kg_since / spec.interval_kg)
        if (d := _date_for_kg(kg_left, today, expected_kg)) is not None:
            dates.append(d)
    if spec.interval_days and last is not None:
        due_by_days = last + timedelta(days=spec.interval_days)
        fractions.append((today - last).days / spec.interval_days)
        dates.append(due_by_days)

    fraction = max(fractions) if fractions else None
    due_date = min(dates) if dates else None
    return TaskStatus(
        key=spec.key,
        kg_since=None if kg_since is None else round(kg_since, 1),
        kg_left=None if kg_left is None else round(kg_left, 1),
        fraction=None if fraction is None else round(fraction, 3),
        due_date=max(today, due_date) if due_date else None,
        due=fraction is not None and fraction >= 1.0,
        last_done=last,
    )


def mark_done(state: TaskState, day: date, burned_total: float | None) -> None:
    state.last_done = day.isoformat()
    state.burned_at = burned_total


def to_dict(states: dict[str, TaskState]) -> dict[str, dict]:
    return {k: asdict(v) for k, v in states.items()}


def from_dict(data: dict | None) -> dict[str, TaskState]:
    return {k: TaskState(**v) for k, v in (data or {}).items()}
