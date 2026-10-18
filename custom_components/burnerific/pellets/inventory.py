"""Pellet stock: what is in storage (bags, pallets, a store room) and in the boiler's silo.

The silo is the hopper next to the boiler. Most NBE controllers keep their own
count of it (``hopper.content``); when they do, that is the truth and this
ledger only mirrors it. When they don't, the silo is tracked here by
subtracting what the boiler burns.

Storage is everything not yet in the silo. Filling the silo moves pellets from
storage to the silo. Deliveries add to storage and update the average price.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

MAX_EVENTS = 250


class StockError(ValueError):
    """An operation that does not add up (negative amounts, more than there is)."""


@dataclass(slots=True)
class Event:
    at: str
    kind: str  # delivery | fill | adjust_storage | adjust_silo | consumed
    kg: float
    storage_after: float
    silo_after: float | None
    price_per_kg: float | None = None
    note: str | None = None


@dataclass(slots=True)
class Inventory:
    storage_kg: float = 0.0
    silo_kg: float | None = None
    silo_capacity_kg: float | None = None
    bag_kg: float = 16.0
    avg_price_per_kg: float | None = None
    delivered_kg_total: float = 0.0
    spent_total: float = 0.0
    events: list[Event] = field(default_factory=list)

    # -- derived -------------------------------------------------------------

    @property
    def total_kg(self) -> float:
        return self.storage_kg + (self.silo_kg or 0.0)

    @property
    def storage_bags(self) -> float:
        return self.storage_kg / self.bag_kg if self.bag_kg > 0 else 0.0

    @property
    def silo_free_kg(self) -> float | None:
        if self.silo_capacity_kg is None or self.silo_kg is None:
            return None
        return max(0.0, self.silo_capacity_kg - self.silo_kg)

    @property
    def stock_value(self) -> float | None:
        return None if self.avg_price_per_kg is None else self.total_kg * self.avg_price_per_kg

    def suggested_fill_kg(self) -> float:
        """Whole bags that fit in the silo and are in storage."""
        free = self.silo_free_kg
        if free is None:
            free = self.storage_kg
        kg = min(free, self.storage_kg)
        if self.bag_kg > 0:
            kg = (kg // self.bag_kg) * self.bag_kg
        return round(kg, 1)

    # -- operations ----------------------------------------------------------

    def deliver(self, kg: float, at: datetime, *, price_per_kg: float | None = None, note: str | None = None) -> Event:
        """Pellets arrive. Price is folded into a weighted average of what is in stock."""
        _positive(kg)
        if price_per_kg is not None:
            if price_per_kg < 0:
                raise StockError("price cannot be negative")
            held = self.total_kg
            old = self.avg_price_per_kg if self.avg_price_per_kg is not None else price_per_kg
            self.avg_price_per_kg = (held * old + kg * price_per_kg) / (held + kg) if held + kg > 0 else price_per_kg
            self.spent_total += kg * price_per_kg
        self.storage_kg += kg
        self.delivered_kg_total += kg
        return self._log("delivery", kg, at, price_per_kg=price_per_kg, note=note)

    def fill_silo(self, kg: float, at: datetime, *, from_storage: bool = True, note: str | None = None) -> Event:
        """Move ``kg`` into the silo. With ``from_storage`` it is taken out of storage."""
        _positive(kg)
        if from_storage and kg > self.storage_kg + 1e-6:
            raise StockError(f"only {self.storage_kg:.1f} kg in storage, cannot take {kg:.1f} kg")
        if from_storage:
            self.storage_kg = max(0.0, self.storage_kg - kg)
        self.silo_kg = (self.silo_kg or 0.0) + kg
        return self._log("fill", kg, at, note=note)

    def set_storage(self, kg: float, at: datetime, *, note: str | None = None) -> Event:
        if kg < 0:
            raise StockError("storage cannot be negative")
        delta = kg - self.storage_kg
        self.storage_kg = kg
        return self._log("adjust_storage", delta, at, note=note)

    def set_silo(self, kg: float, at: datetime, *, note: str | None = None, log: bool = True) -> Event | None:
        if kg < 0:
            raise StockError("silo content cannot be negative")
        delta = kg - (self.silo_kg or 0.0)
        self.silo_kg = kg
        return self._log("adjust_silo", delta, at, note=note) if log else None

    def consume(self, kg: float) -> None:
        """Burned pellets, when the controller does not track the silo itself. Not logged per call."""
        if kg <= 0 or self.silo_kg is None:  # unknown silo stays unknown until someone sets it
            return
        self.silo_kg = max(0.0, self.silo_kg - kg)

    def _log(self, kind: str, kg: float, at: datetime, **extra: Any) -> Event:
        ev = Event(
            at=at.isoformat(),
            kind=kind,
            kg=round(kg, 3),
            storage_after=round(self.storage_kg, 3),
            silo_after=None if self.silo_kg is None else round(self.silo_kg, 3),
            **extra,
        )
        self.events.append(ev)
        del self.events[:-MAX_EVENTS]
        return ev

    # -- persistence ---------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> Inventory:
        if not data:
            return cls()
        events = [Event(**e) for e in data.get("events", [])]
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__ and k != "events"}
        return cls(**known, events=events)


def _positive(kg: float) -> None:
    if not kg > 0:
        raise StockError("amount must be more than 0 kg")
