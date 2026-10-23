"""Money and efficiency: what heat costs, which pellets burn best, and whether the house got better.

Lot quality and the season trend both use the same trick: compare what was
actually burned with what the current model *expects* for the weather of those
very days. The ratio removes the weather. A lot that needs 1.06 × the expected
pellets burns 6 % worse than average; a season at 0.92 used 8 % less than the
house "should" have for its weather, which is what new insulation looks like.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date

MIN_LOT_DAYS = 7
MIN_SEASON_DAYS = 60


@dataclass(slots=True, frozen=True)
class HeatPrice:
    pellet_per_kwh_fuel: float | None  # what a kWh of pellet energy costs
    pellet_per_kwh_heat: float | None  # ... after boiler losses
    heat_pump_per_kwh_heat: float | None  # electricity price / COP
    pellets_cheaper_pct: float | None  # > 0: pellets are cheaper than the heat pump by this much


def heat_price(
    price_per_kg: float | None,
    kwh_per_kg: float,
    boiler_efficiency: float,
    electricity_price: float | None,
    cop: float,
) -> HeatPrice:
    fuel = price_per_kg / kwh_per_kg if price_per_kg is not None and kwh_per_kg > 0 else None
    heat = fuel / boiler_efficiency if fuel is not None and boiler_efficiency > 0 else None
    hp = electricity_price / cop if electricity_price is not None and cop > 0 else None
    cheaper = None
    if heat is not None and hp is not None and hp > 0:
        cheaper = round(100.0 * (hp - heat) / hp, 1)
    return HeatPrice(
        None if fuel is None else round(fuel, 4),
        None if heat is None else round(heat, 4),
        None if hp is None else round(hp, 4),
        cheaper,
    )


@dataclass(slots=True, frozen=True)
class Ratio:
    actual_kg: float
    expected_kg: float
    days: int

    @property
    def index(self) -> float | None:
        return self.actual_kg / self.expected_kg if self.expected_kg > 0 else None


def ratio(rows: Iterable[tuple[float, float]]) -> Ratio:
    """``rows`` of (actual kg, expected kg) per day."""
    actual = expected = 0.0
    n = 0
    for a, e in rows:
        actual += a
        expected += e
        n += 1
    return Ratio(actual, expected, n)


@dataclass(slots=True, frozen=True)
class LotQuality:
    lot: int
    supplier: str | None
    delivered: str | None
    days: int
    index: float | None  # actual / expected; < 1 burns better than average

    def as_dict(self) -> dict:
        return {
            "lot": self.lot,
            "supplier": self.supplier,
            "delivered": self.delivered,
            "days_burned": self.days,
            "relative_consumption": None if self.index is None else round(self.index, 3),
        }


def lot_quality(
    days: Iterable[tuple[int, float, float]],
    lot_info: Callable[[int], tuple[str | None, str | None]],
) -> list[LotQuality]:
    """``days`` of (lot id, actual kg, expected kg). Lots with too few days are left out."""
    by_lot: dict[int, list[tuple[float, float]]] = {}
    for lot, actual, expected in days:
        by_lot.setdefault(lot, []).append((actual, expected))
    out = []
    for lot, rows in sorted(by_lot.items()):
        r = ratio(rows)
        if r.days < MIN_LOT_DAYS:
            continue
        supplier, delivered = lot_info(lot)
        out.append(LotQuality(lot, supplier, delivered, r.days, r.index))
    return out


def season_of(day: date, south: bool = False) -> str:
    """Heating season label. North: July to June ("2025/26"). South: calendar year."""
    if south:
        return str(day.year)
    start = day.year if day.month >= 7 else day.year - 1
    return f"{start}/{str(start + 1)[-2:]}"


@dataclass(slots=True, frozen=True)
class SeasonTrend:
    season: str
    days: int
    actual_kg: float
    expected_kg: float
    normalized_kg: float | None  # what the season would have burned in a normal-weather year

    @property
    def index(self) -> float | None:
        return self.actual_kg / self.expected_kg if self.expected_kg > 0 else None

    def as_dict(self) -> dict:
        return {
            "season": self.season,
            "days": self.days,
            "actual_kg": round(self.actual_kg),
            "expected_kg": round(self.expected_kg),
            "relative_consumption": None if self.index is None else round(self.index, 3),
            "normalized_yearly_kg": None if self.normalized_kg is None else round(self.normalized_kg),
        }


def season_trend(
    days: Iterable[tuple[date, float, float]], normal_year_kg: float, south: bool = False
) -> list[SeasonTrend]:
    """``days`` of (day, actual kg, expected kg). ``normal_year_kg`` is the model's burn for a normal year."""
    by: dict[str, list[tuple[float, float]]] = {}
    for d, a, e in days:
        by.setdefault(season_of(d, south), []).append((a, e))
    out = []
    for season, rows in sorted(by.items()):
        r = ratio(rows)
        if r.days < MIN_SEASON_DAYS:
            continue
        idx = r.index
        out.append(SeasonTrend(season, r.days, r.actual_kg, r.expected_kg, None if idx is None else idx * normal_year_kg))
    return out
