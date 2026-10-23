"""StokerCloud (NBE's cloud) as a read-only fallback when the boiler can't be reached on the LAN.

Uses the public ``dataout2`` API: log in with the StokerCloud user name (often
the boiler's serial, visible in ``stokercloud.dk/v3/#/<user>/main-page``), then
read ``controllerdata2``. No password is needed for reading. Nothing is ever
written through the cloud.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any
from urllib.parse import quote

import aiohttp

from .model import BoilerData, num

BASE = "https://www.stokercloud.dk"
TIMEOUT = aiohttp.ClientTimeout(total=20)


class StokerCloudError(Exception):
    pass


def _pick(items: list[dict] | None, item_id: str | int) -> Any:
    for it in items or []:
        if str(it.get("id")) == str(item_id):
            return it.get("value")
    return None


def _code(value: Any) -> int | None:
    m = re.search(r"(\d+)", str(value or ""))
    return int(m.group(1)) if m else None


def _onoff(value: Any) -> bool | None:
    return True if value == "ON" else False if value == "OFF" else None


def normalize(d: dict, at: datetime) -> BoilerData:
    """Map a ``controllerdata2`` response onto the same reading the LAN gives."""
    misc = d.get("miscdata") or {}
    front, boiler, hopper, weather = (d.get(k) for k in ("frontdata", "boilerdata", "hopperdata", "weatherdata"))
    state = _code((misc.get("state") or {}).get("value"))
    substate = _code((misc.get("substate") or {}).get("value"))
    dhw, dhw_ref = num(_pick(front, "dhw")), num(_pick(front, "dhwwanted"))
    if dhw == 0 and dhw_ref in (0, None):  # StokerCloud reports 0/0 when no tank is fitted
        dhw = dhw_ref = None
    left = d.get("leftoutput") or {}
    raw: dict[str, str] = {}
    for group in ("frontdata", "boilerdata", "hopperdata", "dhwdata", "weatherdata"):
        for it in d.get(group) or []:
            raw[f"cloud/{group}/{it.get('id')}"] = str(it.get("value", ""))
    today_kg = num(_pick(hopper, 3))
    return BoilerData(
        raw=raw,
        at=at,
        state=state,
        substate=substate,
        substate_sec=num((misc.get("substatesecs") or {}).get("value")),
        power_pct=num(_pick(boiler, 4)) if num(_pick(boiler, 4)) is not None else num(misc.get("outputpct")),
        power_kw=num(_pick(boiler, 5)) if num(_pick(boiler, 5)) is not None else num(misc.get("output")),
        boiler_temp=num(_pick(front, "boilertemp")),
        boiler_ref=num(_pick(front, "-wantedboilertemp")),
        smoke_temp=num(_pick(boiler, 3)),
        dhw_temp=dhw,
        dhw_ref=dhw_ref,
        outdoor_temp=num(_pick(weather, 1)),
        oxygen=num(_pick(boiler, 12)),
        oxygen_ref=num(_pick(front, "refoxygen")),
        photo_level=num(_pick(boiler, 6)),
        boiler_pump=_onoff((left.get("output-2") or {}).get("val")),
        dhw_valve=_onoff((left.get("output-1") or {}).get("val")),
        hopper_kg=num(_pick(front, "hoppercontent")),
        auger_capacity_g=num(_pick(hopper, 2)),
        total_kg=num(_pick(hopper, 4)),
        # The cloud gives today's total only; put it where the day array would have it.
        days_kg=[0.0] * (at.day - 1) + [today_kg] if today_kg is not None else [],
        source="cloud",
    )


class StokerCloud:
    def __init__(self, session: aiohttp.ClientSession, user: str, base: str = BASE) -> None:
        if not user.strip():
            raise StokerCloudError("no StokerCloud user name")
        self._session = session
        self._user = user.strip()
        self._base = base.rstrip("/")
        self._token: str | None = None

    async def _json(self, path: str) -> dict:
        try:
            async with self._session.get(self._base + path, timeout=TIMEOUT) as resp:
                if resp.status != 200:
                    raise StokerCloudError(f"StokerCloud answered HTTP {resp.status}")
                return await resp.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError) as err:
            raise StokerCloudError(str(err)) from err

    async def _login(self) -> str:
        r = await self._json(f"/v2/dataout2/login.php?user={quote(self._user, safe='')}")
        token = r.get("token")
        if not token:
            raise StokerCloudError(f"StokerCloud login failed: {r.get('message') or 'no token'}")
        self._token = token
        return token

    async def read(self, at: datetime) -> BoilerData:
        for attempt in range(2):
            token = self._token or await self._login()
            try:
                d = await self._json(f"/v2/dataout2/controllerdata2.php?token={token}")
            except StokerCloudError:
                self._token = None
                if attempt:
                    raise
                continue
            if not isinstance(d.get("notconnected"), int):
                self._token = None
                if attempt:
                    raise StokerCloudError("unexpected StokerCloud response")
                continue
            if d["notconnected"] != 0:
                raise StokerCloudError("the boiler is not connected to StokerCloud either")
            return normalize(d, at)
        raise StokerCloudError("StokerCloud did not answer")
