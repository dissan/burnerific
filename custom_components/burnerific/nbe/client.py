"""Async UDP client for NBE controllers (V7, V10, V13, V16, RTB, Blackstar+)."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import random
import socket
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from . import eventlog
from .crypto import RsaKey, RsaKeyError
from .eventlog import LogEntry
from .frame import PORT, Fn, FrameError, Response, decode_response, encode_request, parse_pairs

_LOGGER = logging.getLogger(__name__)

SETTING_GROUPS = (
    "boiler", "hot_water", "regulation", "weather", "weather2", "oxygen", "cleaning",
    "hopper", "fan", "auger", "ignition", "pump", "sun", "vacuum", "misc", "alarm", "manual",
)  # fmt: skip
CONSUMPTION_KEYS = (
    "total_hours", "total_days", "total_months", "total_years",
    "dhw_hours", "dhw_days", "dhw_months", "dhw_years", "counter",
)  # fmt: skip


class NbeError(Exception):
    """Base error for talking to the boiler."""


class NbeTimeout(NbeError):
    """The boiler did not answer."""


class NbeRefused(NbeError):
    """The boiler answered with a non-zero status (unknown key, wrong PIN, out of range...)."""

    def __init__(self, what: str, response: Response) -> None:
        super().__init__(f"boiler refused {what}: {response.payload or f'status {response.status}'}")
        self.response = response


@dataclass(slots=True, frozen=True)
class DiscoveryInfo:
    serial: str | None
    ip: str | None
    model: str | None
    version: str | None
    address: str

    @classmethod
    def from_response(cls, response: Response, address: str) -> DiscoveryInfo:
        p = response.pairs()
        return cls(
            serial=p.get("Serial") or p.get("serial"),
            ip=p.get("IP") or p.get("ip"),
            model=p.get("Type") or p.get("type"),
            version=p.get("Ver") or p.get("ver"),
            address=address,
        )

    @property
    def generation(self) -> int | None:
        """``"v7std"`` -> 7, ``"V13"`` -> 13."""
        text = (self.model or "").lower()
        i = text.find("v")
        digits = ""
        for ch in text[i + 1 :] if i >= 0 else "":
            if not ch.isdigit():
                break
            digits += ch
        return int(digits) if digits else None


class _Protocol(asyncio.DatagramProtocol):
    def __init__(self) -> None:
        self.pending: dict[tuple[int, int], asyncio.Future[tuple[Response, str]]] = {}
        self.listeners: list = []
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport) -> None:  # type: ignore[override]
        self.transport = transport

    def datagram_received(self, data: bytes, addr) -> None:  # type: ignore[override]
        try:
            response = decode_response(data)
        except FrameError:
            return
        for listener in list(self.listeners):
            listener(response, addr[0])
        fut = self.pending.pop((response.fn, response.seq), None)
        if fut is not None and not fut.done():
            fut.set_result((response, addr[0]))

    def error_received(self, exc: Exception) -> None:
        # ICMP port unreachable and friends: fail whatever is waiting, the caller retries.
        for fut in self.pending.values():
            if not fut.done():
                fut.set_exception(NbeError(str(exc)))
        self.pending.clear()

    def connection_lost(self, exc: Exception | None) -> None:
        for fut in self.pending.values():
            if not fut.done():
                fut.set_exception(NbeError("socket closed"))
        self.pending.clear()


async def _open(broadcast: bool = False) -> tuple[asyncio.DatagramTransport, _Protocol]:
    loop = asyncio.get_running_loop()
    transport, protocol = await loop.create_datagram_endpoint(
        _Protocol, local_addr=("0.0.0.0", 0), family=socket.AF_INET, allow_broadcast=broadcast
    )
    return transport, protocol  # type: ignore[return-value]


async def scan(
    addresses: Iterable[str] = ("255.255.255.255",),
    *,
    serial: str | None = None,
    wait: float = 2.0,
    port: int = PORT,
) -> list[DiscoveryInfo]:
    """Broadcast a discovery and collect every controller that answers within ``wait`` seconds."""
    transport, protocol = await _open(broadcast=True)
    found: dict[str, DiscoveryInfo] = {}

    def on_response(response: Response, address: str) -> None:
        if response.fn == Fn.DISCOVERY:
            found[address] = DiscoveryInfo.from_response(response, address)

    protocol.listeners.append(on_response)
    try:
        frame = encode_request(Fn.DISCOVERY, random.randint(1, 99), "NBE Discovery", serial=serial)
        for address in addresses:
            try:
                transport.sendto(frame, (address, port))
            except OSError as err:
                _LOGGER.debug("discovery to %s failed: %s", address, err)
        await asyncio.sleep(wait)
    finally:
        transport.close()
    return list(found.values())


def split_host(host: str, default_port: int = PORT) -> tuple[str, int]:
    """``"192.168.1.20:9000"`` -> ``("192.168.1.20", 9000)``. Bare IPv6 addresses are left alone."""
    host = host.strip()
    name, sep, port = host.rpartition(":")
    if sep and port.isdigit() and ":" not in name:
        return name, int(port)
    return host.strip("[]"), default_port


def subnet_hosts(networks: Iterable[str], own: Iterable[str] = (), limit: int = 1024) -> list[str]:
    """Every host address of the given IPv4 networks (e.g. ``"192.168.1.0/24"``), skipping our own.

    Used to find a boiler by asking each address directly when broadcasts don't
    get through (Docker, VLANs, some firewalls). Networks larger than ``limit``
    hosts are skipped rather than flooded.
    """
    skip = set(own)
    out: list[str] = []
    for text in networks:
        try:
            net = ipaddress.ip_network(text, strict=False)
        except ValueError:
            continue
        if net.version != 4 or net.num_addresses - 2 > limit:
            continue
        out += [str(h) for h in net.hosts() if str(h) not in skip]
    return out


class NbeClient:
    """One controller. Requests are serialized; the controller only handles one at a time."""

    def __init__(
        self,
        host: str,
        *,
        serial: str | None = None,
        pin: str | None = None,
        port: int = PORT,
        timeout: float = 3.0,
        retries: int = 2,
    ) -> None:
        self.host, port = split_host(host, port)
        self.serial = serial
        self.pin = pin
        self.port = port
        self.timeout = timeout
        self.retries = retries
        self.info: DiscoveryInfo | None = None
        self._key: RsaKey | None = None
        self._seq = random.randint(1, 99)
        self._lock = asyncio.Lock()
        self._transport: asyncio.DatagramTransport | None = None
        self._protocol: _Protocol | None = None

    async def __aenter__(self) -> NbeClient:
        await self.discover()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def close(self) -> None:
        if self._transport is not None:
            self._transport.close()
        self._transport = self._protocol = None

    def _next_seq(self) -> int:
        self._seq = self._seq % 99 + 1
        return self._seq

    async def _exchange(self, fn: int, payload: str, *, key: RsaKey | None = None) -> Response:
        if self._transport is None or self._transport.is_closing():
            self._transport, self._protocol = await _open()
        assert self._protocol is not None
        loop = asyncio.get_running_loop()
        last: Exception = NbeTimeout(f"no answer from {self.host}")
        for attempt in range(self.retries + 1):
            seq = self._next_seq()
            fut: asyncio.Future[tuple[Response, str]] = loop.create_future()
            self._protocol.pending[(fn, seq)] = fut
            try:
                frame = encode_request(fn, seq, payload, serial=self.serial, pin=self.pin, key=key)
                self._transport.sendto(frame, (self.host, self.port))
                response, _ = await asyncio.wait_for(fut, self.timeout)
                return response
            except TimeoutError:
                last = NbeTimeout(f"no answer from {self.host} (fn {fn}, attempt {attempt + 1})")
            except OSError as err:
                last = NbeError(str(err))
            except NbeError as err:
                last = err
            finally:
                self._protocol.pending.pop((fn, seq), None)
        raise last

    async def request(self, fn: int, payload: str = "", *, encrypt: bool = False) -> Response:
        async with self._lock:
            if encrypt and self._key is None:
                self._key = await self._fetch_key()
            return await self._exchange(fn, payload, key=self._key if encrypt else None)

    async def _fetch_key(self) -> RsaKey:
        response = await self._exchange(Fn.READ_SETTING, "misc.rsa_key")
        value = response.pairs().get("rsa_key")
        if not response.ok or not value:
            raise NbeRefused("misc.rsa_key", response)
        try:
            return RsaKey.from_payload(value)
        except RsaKeyError as err:
            raise NbeError(f"boiler sent an unusable key: {err}") from err

    async def read_pairs(self, fn: int, payload: str) -> dict[str, str]:
        response = await self.request(fn, payload)
        if not response.ok:
            raise NbeRefused(payload or f"fn {fn}", response)
        return parse_pairs(response.payload)

    # -- reads ---------------------------------------------------------------

    async def discover(self) -> DiscoveryInfo:
        async with self._lock:
            response = await self._exchange(Fn.DISCOVERY, "NBE Discovery")
        self.info = DiscoveryInfo.from_response(response, self.host)
        if not self.serial and self.info.serial:
            self.serial = self.info.serial
        return self.info

    async def operating_data(self) -> dict[str, str]:
        return await self.read_pairs(Fn.OPERATING_DATA, "*")

    async def advanced_data(self) -> dict[str, str]:
        return await self.read_pairs(Fn.ADVANCED_DATA, "*")

    async def settings(self, group: str) -> dict[str, str]:
        return await self.read_pairs(Fn.READ_SETTING, f"{group}.*")

    async def consumption(self, key: str) -> dict[str, str]:
        return await self.read_pairs(Fn.CONSUMPTION, key)

    async def info_data(self) -> dict[str, str]:
        return await self.read_pairs(Fn.INFO, "")

    async def sw_versions(self) -> dict[str, str]:
        return await self.read_pairs(Fn.SW_VERSIONS, "")

    async def event_log(self, before: datetime) -> list[LogEntry]:
        """Event log entries before ``before`` (the controller sends a page, newest first)."""
        response = await self.request(Fn.EVENT_LOG, eventlog.request_payload(before))
        if not response.ok:
            raise NbeRefused("event log", response)
        return eventlog.parse(response.payload, before.tzinfo)

    async def validate_pin(self) -> bool:
        """Check the PIN with a harmless write (``misc.dummy``, a scratch setting every controller has).

        Returns False when the controller refuses it, which on a reachable boiler means a wrong PIN.
        """
        if not self.pin:
            return False
        try:
            await self.write_setting("misc", "dummy", 0)
        except NbeRefused:
            return False
        return True

    # -- writes --------------------------------------------------------------

    async def write_setting(self, group: str, key: str, value: str | float | int) -> None:
        """Change one setting, e.g. ``write_setting("hopper", "content", 120)``. Needs the PIN."""
        if not self.pin:
            raise NbeError("a PIN (the code on the controller's sticker) is needed to change settings")
        text = _format_value(value)
        what = f"{group}.{key}={text}"
        response = await self.request(Fn.WRITE_SETTING, what, encrypt=True)
        if not response.ok:
            # A stale key is the usual cause after a controller reboot; fetch a fresh one once.
            self._key = None
            response = await self.request(Fn.WRITE_SETTING, what, encrypt=True)
            if not response.ok:
                raise NbeRefused(what, response)


def _format_value(value: str | float | int) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return f"{value:.2f}".rstrip("0").rstrip(".") if value != int(value) else str(int(value))
    return str(value)
