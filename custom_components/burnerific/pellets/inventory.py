"""Pellet stock: what is in storage (bags, pallets, a store room) and in the boiler's silo.

The silo is the hopper next to the boiler. Most NBE controllers keep their own
count of it (``hopper.content``); when they do, that is the truth and this
ledger only mirrors it. When they don't, the silo is tracked here by
subtracting what the boiler burns.

Storage is everything not yet in the silo. Filling the silo moves pellets from
storage to the silo. Deliveries add to storage and update the average price.

Every delivery is a *lot* (supplier, price, amount). Pellets leave storage and
the silo first-in first-out, so at any time we know which lot is burning. That
is what lets Burnerific compare suppliers.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

MAX_EVENTS = 250
MAX_LOTS = 100
UNKNOWN_LOT = 0  # pellets whose origin we don't know (stock entered by hand)


class StockError(ValueError):
    """An operation that does not add up (negative amounts, more than there is)."""


@dataclass(slots=True)
class Event:
    at: str
    kind: str  # delivery | fill | adjust_storage | adjust_silo
    kg: float
    storage_after: float
    silo_after: float | None
    price_per_kg: float | None = None
    note: str | None = None
    counter_kg: float | None = None  # controller's burn counter at the time (fills)
    lot: int | None = None
    supplier: str | None = None


@dataclass(slots=True)
class Lot:
    id: int
    delivered: str
    kg: float
    supplier: str | None = None
    price_per_kg: float | None = None
    note: str | None = None


@dataclass(slots=True)
class Portion:
    lot: int
    kg: float


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
    lots: list[Lot] = field(default_factory=list)
    storage_lots: list[Portion] = field(default_factory=list)  # oldest first
    silo_lots: list[Portion] = field(default_factory=list)  # oldest first, burning from the front

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

    @property
    def burning_lot(self) -> Lot | None:
        """The delivery the boiler is burning right now (front of the silo)."""
        for p in self.silo_lots:
            if p.kg > 1e-6:
                return self.lot(p.lot)
        return None

    def lot(self, lot_id: int) -> Lot | None:
        return next((lot for lot in self.lots if lot.id == lot_id), None)

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

    def deliver(
        self,
        kg: float,
        at: datetime,
        *,
        price_per_kg: float | None = None,
        supplier: str | None = None,
        note: str | None = None,
    ) -> Event:
        """Pellets arrive. Price is folded into a weighted average of what is in stock."""
        _positive(kg)
        if price_per_kg is not None:
            if price_per_kg < 0:
                raise StockError("price cannot be negative")
            held = self.total_kg
            old = self.avg_price_per_kg if self.avg_price_per_kg is not None else price_per_kg
            self.avg_price_per_kg = (held * old + kg * price_per_kg) / (held + kg) if held + kg > 0 else price_per_kg
            self.spent_total += kg * price_per_kg
        lot_id = max((lot.id for lot in self.lots), default=0) + 1
        self.lots.append(Lot(lot_id, at.isoformat(), kg, supplier, price_per_kg, note))
        del self.lots[:-MAX_LOTS]
        self.storage_lots.append(Portion(lot_id, kg))
        self.storage_kg += kg
        self.delivered_kg_total += kg
        return self._log("delivery", kg, at, price_per_kg=price_per_kg, note=note, lot=lot_id, supplier=supplier)

    def fill_silo(
        self,
        kg: float,
        at: datetime,
        *,
        from_storage: bool = True,
        silo_after: float | None = None,
        counter_kg: float | None = None,
        note: str | None = None,
    ) -> Event:
        """Move ``kg`` into the silo. With ``from_storage`` it is taken out of storage.

        ``silo_after`` is the silo content when someone else (the controller) already
        counted the fill; otherwise ``kg`` is added to what the silo held.
        """
        _positive(kg)
        if from_storage and kg > self.storage_kg + 1e-6:
            raise StockError(f"only {self.storage_kg:.1f} kg in storage, cannot take {kg:.1f} kg")
        if from_storage:
            self.storage_kg = max(0.0, self.storage_kg - kg)
            moved = _take(self.storage_lots, kg)
        else:
            moved = [Portion(UNKNOWN_LOT, kg)]
        # Keep the silo's lot list in step with what is really in it before adding.
        self._trim_silo_lots()
        self.silo_lots.extend(moved)
        self.silo_kg = silo_after if silo_after is not None else (self.silo_kg or 0.0) + kg
        return self._log("fill", kg, at, note=note, counter_kg=counter_kg, lot=moved[0].lot if moved else None)

    def set_storage(self, kg: float, at: datetime, *, note: str | None = None) -> Event:
        if kg < 0:
            raise StockError("storage cannot be negative")
        delta = kg - self.storage_kg
        if delta < 0:
            _take(self.storage_lots, -delta, newest_first=True)
        elif delta > 0:
            self.storage_lots.append(Portion(UNKNOWN_LOT, delta))
        self.storage_kg = kg
        return self._log("adjust_storage", delta, at, note=note)

    def set_silo(self, kg: float, at: datetime, *, note: str | None = None, log: bool = True) -> Event | None:
        if kg < 0:
            raise StockError("silo content cannot be negative")
        delta = kg - (self.silo_kg or 0.0)
        self.silo_kg = kg
        if delta > 0:
            self.silo_lots.append(Portion(UNKNOWN_LOT, delta))
        self._trim_silo_lots()
        return self._log("adjust_silo", delta, at, note=note) if log else None

    def consume(self, kg: float) -> None:
        """Burned pellets, when the controller does not track the silo itself. Not logged per call."""
        if kg <= 0 or self.silo_kg is None:  # unknown silo stays unknown until someone sets it
            return
        self.silo_kg = max(0.0, self.silo_kg - kg)
        self._trim_silo_lots()

    def mirror_silo(self, kg: float) -> None:
        """The controller reported its silo count."""
        self.silo_kg = kg
        self._trim_silo_lots()

    def _trim_silo_lots(self) -> None:
        """Burn from the front of the silo until the lot list holds what the silo holds."""
        held = sum(p.kg for p in self.silo_lots)
        target = self.silo_kg or 0.0
        if held > target:
            _take(self.silo_lots, held - target)

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
        nested = {
            "events": [Event(**{k: v for k, v in e.items() if k in Event.__dataclass_fields__}) for e in data.get("events", [])],
            "lots": [Lot(**lot) for lot in data.get("lots", [])],
            "storage_lots": [Portion(**p) for p in data.get("storage_lots", [])],
            "silo_lots": [Portion(**p) for p in data.get("silo_lots", [])],
        }
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__ and k not in nested}
        inv = cls(**known, **nested)
        # Stock from before lots existed (or typed in by hand) is of unknown origin.
        if not inv.storage_lots and inv.storage_kg > 0:
            inv.storage_lots.append(Portion(UNKNOWN_LOT, inv.storage_kg))
        if not inv.silo_lots and inv.silo_kg:
            inv.silo_lots.append(Portion(UNKNOWN_LOT, inv.silo_kg))
        return inv


def _take(portions: list[Portion], kg: float, *, newest_first: bool = False) -> list[Portion]:
    """Remove ``kg`` from a FIFO list of portions (or LIFO), returning what was taken by lot."""
    taken: list[Portion] = []
    order = range(len(portions) - 1, -1, -1) if newest_first else range(len(portions))
    left = kg
    for i in order:
        if left <= 1e-9:
            break
        p = portions[i]
        n = min(p.kg, left)
        if n > 0:
            p.kg -= n
            left -= n
            if taken and taken[-1].lot == p.lot:
                taken[-1].kg += n
            else:
                taken.append(Portion(p.lot, n))
    portions[:] = [p for p in portions if p.kg > 1e-6]
    if left > 1e-6:  # more than we had on record: the rest is of unknown origin
        taken.append(Portion(UNKNOWN_LOT, left))
    return taken


def _positive(kg: float) -> None:
    if not kg > 0:
        raise StockError("amount must be more than 0 kg")
