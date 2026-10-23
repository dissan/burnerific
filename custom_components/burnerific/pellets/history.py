"""Day-by-day record of pellets burned and outdoor temperature, the training data of the model."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta

from .consumption import DayObs

KEEP_DAYS = 3 * 366
MAX_GAP = timedelta(minutes=45)  # longer gaps don't count towards temperature coverage
MIN_TEMP_HOURS = 8.0


@dataclass(slots=True)
class DayRecord:
    kg: float | None = None
    temp_sum: float = 0.0  # °C·hours
    temp_hours: float = 0.0
    temp_override: float | None = None  # e.g. backfilled from the weather archive
    source: str = "live"
    dhw_kg: float | None = None  # part of kg that went to hot water, when the controller tells
    lot: int | None = None  # delivery lot burning that day

    @property
    def temp(self) -> float | None:
        if self.temp_hours >= MIN_TEMP_HOURS:
            return self.temp_sum / self.temp_hours
        return self.temp_override


@dataclass(slots=True)
class History:
    days: dict[str, DayRecord]
    current: str | None = None
    counter_start: float | None = None  # boiler's total counter when we first saw `current`
    full_day: bool = False  # we saw `current` from (near) midnight
    last_seen: str | None = None
    last_temp: float | None = None

    @classmethod
    def empty(cls) -> History:
        return cls(days={})

    def observe(
        self,
        now: datetime,
        counter_kg: float | None,
        temp: float | None,
        boiler_day_kg=None,
    ) -> list[date]:
        """Feed one poll. Returns the days that were closed by this call.

        ``boiler_day_kg(day)`` returns the controller's own figure for a day, used
        when we did not see the whole day ourselves.
        """
        today = now.date().isoformat()
        closed: list[date] = []
        last = datetime.fromisoformat(self.last_seen) if self.last_seen else None

        if self.current != today:
            if self.current is not None:
                rec = self.days.setdefault(self.current, DayRecord())
                if self.full_day and self.counter_start is not None and counter_kg is not None:
                    rec.kg = max(0.0, counter_kg - self.counter_start)
                    rec.source = "counter"
                elif boiler_day_kg is not None:
                    kg = boiler_day_kg(date.fromisoformat(self.current))
                    if kg is not None:
                        rec.kg = kg
                        rec.source = "controller"
                closed.append(date.fromisoformat(self.current))
            midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
            self.full_day = (
                self.current is not None and last is not None and now - midnight <= MAX_GAP and now - last <= MAX_GAP
            )
            self.current = today
            self.counter_start = counter_kg
            last = None if not self.full_day else last

        # Time-weighted temperature: the previous reading holds until this one.
        if last is not None and self.last_temp is not None:
            dt = now - max(last, now.replace(hour=0, minute=0, second=0, microsecond=0))
            if timedelta(0) < dt <= MAX_GAP:
                rec = self.days.setdefault(today, DayRecord())
                hours = dt.total_seconds() / 3600.0
                rec.temp_sum += self.last_temp * hours
                rec.temp_hours += hours
        if self.counter_start is None and counter_kg is not None:
            self.counter_start = counter_kg
            self.full_day = False
        self.last_seen = now.isoformat()
        self.last_temp = temp
        self._prune(now.date())
        return closed

    def burned_today(self, counter_kg: float | None) -> float | None:
        if counter_kg is None or self.counter_start is None:
            return None
        return max(0.0, counter_kg - self.counter_start)

    def backfill(self, day: date, kg: float | None, temp: float | None, source: str) -> None:
        """Add data we did not observe live (controller arrays, weather archive). Never overwrites live data."""
        key = day.isoformat()
        if key == self.current:
            return
        rec = self.days.setdefault(key, DayRecord(source=source))
        if kg is not None and rec.kg is None:
            rec.kg = kg
            rec.source = source
        if temp is not None and rec.temp_hours < MIN_TEMP_HOURS:
            rec.temp_override = temp

    def observations(self, weather=None, *, heating_only: bool = False) -> list[DayObs]:
        """Closed days with a burn figure. ``weather(day)`` may supply ``(wind, sun)``.

        With ``heating_only`` the hot water share is taken out, for models that
        handle hot water separately.
        """
        out = []
        for key, rec in sorted(self.days.items()):
            if key == self.current or rec.kg is None:
                continue
            day = date.fromisoformat(key)
            wind = sun = None
            if weather is not None and (w := weather(day)) is not None:
                wind, sun = w
            kg = rec.kg - (rec.dhw_kg or 0.0) if heating_only else rec.kg
            out.append(DayObs(day, max(0.0, kg), rec.temp, wind, sun))
        return out

    def dhw_mean(self, today: date, days: int = 30) -> float | None:
        """Average hot water kg/day over recent days the controller reported it for."""
        cutoff = (today - timedelta(days=days)).isoformat()
        vals = [r.dhw_kg for k, r in self.days.items() if k >= cutoff and k != self.current and r.dhw_kg is not None]
        return sum(vals) / len(vals) if vals else None

    def temps(self) -> list[tuple[date, float]]:
        return [(date.fromisoformat(k), t) for k, r in sorted(self.days.items()) if (t := r.temp) is not None]

    def _prune(self, today: date) -> None:
        cutoff = (today - timedelta(days=KEEP_DAYS)).isoformat()
        for key in [k for k in self.days if k < cutoff]:
            del self.days[key]

    def to_dict(self) -> dict:
        return {
            "days": {k: asdict(v) for k, v in self.days.items()},
            "current": self.current,
            "counter_start": self.counter_start,
            "full_day": self.full_day,
            "last_seen": self.last_seen,
            "last_temp": self.last_temp,
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> History:
        if not data:
            return cls.empty()
        fields = DayRecord.__dataclass_fields__
        days = {k: DayRecord(**{f: x for f, x in v.items() if f in fields}) for k, v in data.get("days", {}).items()}
        return cls(
            days=days,
            current=data.get("current"),
            counter_start=data.get("counter_start"),
            full_day=bool(data.get("full_day")),
            last_seen=data.get("last_seen"),
            last_temp=data.get("last_temp"),
        )
