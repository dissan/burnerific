"""A calendar of what's coming: silo refill, order deadline, run-out, restock and maintenance."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING

from homeassistant.components.calendar import CalendarEntity, CalendarEntityDescription, CalendarEvent
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from .coordinator import BoilerCoordinator
from .entity import BurnerificEntity
from .pellet_coordinator import PelletCoordinator, PelletState

if TYPE_CHECKING:
    from . import BurnerificConfigEntry

PARALLEL_UPDATES = 0

# Event titles are not translatable in Home Assistant calendars; keep them short and plain.
TITLES = {
    "silo": "Silo empty (expected)",
    "order": "Order pellets by",
    "runout": "Pellets run out (expected)",
    "restock": "Restock date",
    "ash": "Empty ash drawer",
    "clean": "Clean heat exchanger",
    "sweep": "Chimney sweep due",
    "service": "Burner service due",
}


async def async_setup_entry(
    hass: HomeAssistant, entry: BurnerificConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    rt = entry.runtime_data
    async_add_entities([PelletCalendar(rt.pellets, rt.boiler)])


def events_for(state: PelletState) -> list[CalendarEvent]:
    out: list[tuple[str, date, str]] = []
    f = state.forecast
    if f is not None:
        if f.silo_empty["p50"]:
            out.append(("silo", f.silo_empty["p50"], f"Likely between {f.silo_empty['p10']} and {f.silo_empty['p90']}"))
        if f.runout["p50"]:
            out.append(("runout", f.runout["p50"], f"Likely between {f.runout['p10']} and {f.runout['p90']}"))
    if state.order_by:
        out.append(("order", state.order_by, "10 % risk of running dry after this, given the delivery time"))
    out.append(("restock", state.restock, ""))
    for key, task in state.tasks.items():
        if task.due_date:
            out.append((key, task.due_date, "" if task.kg_left is None else f"{task.kg_left:.0f} kg to go"))
    return sorted(
        (CalendarEvent(start=d, end=d + timedelta(days=1), summary=TITLES[k], description=desc or None, uid=f"{k}")
         for k, d, desc in out),
        key=lambda e: e.start,
    )


class PelletCalendar(BurnerificEntity[PelletCoordinator], CalendarEntity):
    def __init__(self, pellets: PelletCoordinator, boiler: BoilerCoordinator) -> None:
        super().__init__(pellets, boiler, CalendarEntityDescription(key="calendar", translation_key="calendar"))

    @property
    def event(self) -> CalendarEvent | None:
        if self.coordinator.data is None:
            return None
        today = dt_util.now().date()
        upcoming = [e for e in events_for(self.coordinator.data) if e.start >= today]
        return upcoming[0] if upcoming else None

    async def async_get_events(self, hass: HomeAssistant, start_date: datetime, end_date: datetime) -> list[CalendarEvent]:
        if self.coordinator.data is None:
            return []
        start, end = start_date.date(), end_date.date()
        return [e for e in events_for(self.coordinator.data) if start <= e.start <= end]
