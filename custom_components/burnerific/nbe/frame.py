"""Wire format of the NBE local protocol (UDP port 8483).

Request  (plain):     app_id(12) controller(6) " "  BODY
Request  (encrypted): app_id(12) controller(6) "*"  RSA(BODY padded to 64 bytes)
BODY:                 STX fn(2) seq(2) pin(10) time(10) "pad "(4) len(3) payload ETX

Response:             app_id(12) controller(6) STX fn(2) seq(2) status(1) len(3) payload ETX

All numeric fields are ASCII decimal. Responses are never encrypted.
"""

from __future__ import annotations

from dataclasses import dataclass
import secrets
import time

from .crypto import LeadingZero, RsaKey

PORT = 8483
APP_ID = "Burnerific__"  # any 12 chars work; the boiler echoes it back
STX = 0x02
ETX = 0x04
MAX_PAYLOAD = 495

PLAIN = b" "
ENCRYPTED = b"*"


class Fn:
    """Function codes understood by the controller."""

    DISCOVERY = 0
    READ_SETTING = 1
    WRITE_SETTING = 2
    OPERATING_DATA = 4
    ADVANCED_DATA = 5
    CONSUMPTION = 6
    CHART_DATA = 7
    EVENT_LOG = 8
    INFO = 9
    SW_VERSIONS = 10


class FrameError(ValueError):
    """Bytes that are not a well formed NBE frame."""


@dataclass(slots=True, frozen=True)
class Response:
    app_id: str
    controller: str
    fn: int
    seq: int
    status: int
    payload: str

    @property
    def ok(self) -> bool:
        return self.status == 0

    def pairs(self) -> dict[str, str]:
        return parse_pairs(self.payload)


def controller_id(serial: str | None) -> bytes:
    """First six digits of the serial, zero padded. Boilers with another serial ignore us."""
    digits = "".join(ch for ch in (serial or "") if ch.isdigit())[:6]
    return f"{int(digits or 0):06d}".encode()


def build_body(fn: int, seq: int, payload: str, pin: str | None, now: float | None = None) -> bytes:
    raw = payload.encode("latin-1")
    if len(raw) > MAX_PAYLOAD:
        raise FrameError(f"payload is {len(raw)} bytes, max is {MAX_PAYLOAD}")
    if not 0 <= seq <= 99:
        raise FrameError(f"sequence number {seq} out of range")
    pin_field = f"{pin[:10]:>10}" if pin is not None else "0" * 10
    stamp = int(time.time() if now is None else now)
    return (
        bytes([STX])
        + f"{fn:02d}{seq:02d}{pin_field}{stamp:>10}pad {len(raw):03d}".encode("ascii")
        + raw
        + bytes([ETX])
    )


def encode_request(
    fn: int,
    seq: int,
    payload: str = "",
    *,
    serial: str | None = None,
    pin: str | None = None,
    key: RsaKey | None = None,
    now: float | None = None,
) -> bytes:
    """Build a request datagram. With ``key`` the body is RSA encrypted and carries the PIN."""
    head = APP_ID.encode("ascii")[:12].ljust(12) + controller_id(serial)
    if key is None:
        return head + PLAIN + build_body(fn, seq, payload, None, now)
    body = build_body(fn, seq, payload, pin or "", now)
    if len(body) > key.size:
        raise FrameError(f"encrypted payload too long ({len(body)} > {key.size} bytes)")
    for _ in range(32):
        # Pad with random 7-bit bytes, as the controller expects.
        padded = body + bytes(secrets.randbelow(128) for _ in range(key.size - len(body)))
        try:
            return head + ENCRYPTED + key.encrypt(padded)
        except LeadingZero:
            continue
    raise FrameError("could not encrypt request")


def decode_response(data: bytes) -> Response:
    if len(data) < 28 or data[18] != STX:
        raise FrameError("not an NBE response")
    try:
        fn = int(data[19:21])
        seq = int(data[21:23])
        status = int(data[23:24])
        size = int(data[24:27])
    except ValueError as err:
        raise FrameError("bad header field") from err
    payload = data[27 : 27 + size]
    if len(payload) != size:
        raise FrameError("truncated payload")
    return Response(
        app_id=data[0:12].decode("latin-1"),
        controller=data[12:18].decode("latin-1"),
        fn=fn,
        seq=seq,
        status=status,
        payload=payload.decode("utf-8", errors="replace"),
    )


def parse_pairs(payload: str) -> dict[str, str]:
    """``"a=1;b=2"`` -> ``{"a": "1", "b": "2"}``. Items without ``=`` are dropped."""
    out: dict[str, str] = {}
    for item in payload.split(";"):
        key, sep, value = item.partition("=")
        if sep and key.strip():
            out[key.strip()] = value.strip()
    return out
