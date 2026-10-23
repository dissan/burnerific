"""The controller's event log (function 8).

Request payload ``yymmdd:hhmmss;`` asks for entries before that moment, newest
first. Each entry looks like ``261023,160313,1,0,2,5, `` (date, time, kind, ?,
from, to). Kind 1 is a state change (from/to are state codes, e.g. 2 -> 5 is
"ignition -> burning"); other kinds are passed through as they come.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, tzinfo

STATE_CHANGE = 1


@dataclass(slots=True, frozen=True)
class LogEntry:
    at: datetime
    kind: int
    code: int
    before: int | None
    after: int | None
    raw: str

    @property
    def is_state_change(self) -> bool:
        return self.kind == STATE_CHANGE


def request_payload(before: datetime) -> str:
    return before.strftime("%y%m%d:%H%M%S;")


def parse(payload: str, tz: tzinfo | None = None) -> list[LogEntry]:
    out: list[LogEntry] = []
    for item in payload.split(";"):
        parts = [p.strip() for p in item.split(",")]
        if len(parts) < 6 or not parts[0].isdigit() or not parts[1].isdigit():
            continue
        try:
            at = datetime.strptime(parts[0] + parts[1], "%y%m%d%H%M%S").replace(tzinfo=tz)
            kind = int(parts[2])
            code = int(parts[3]) if parts[3].lstrip("-").isdigit() else 0
        except ValueError:
            continue
        before = int(parts[4]) if parts[4].lstrip("-").isdigit() else None
        after = int(parts[5]) if parts[5].lstrip("-").isdigit() else None
        out.append(LogEntry(at, kind, code, before, after, item.strip()))
    return out
