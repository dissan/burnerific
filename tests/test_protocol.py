from __future__ import annotations

import asyncio

import pytest

from nbe.client import DiscoveryInfo, NbeClient, NbeError, NbeRefused
from nbe.crypto import RsaKey, RsaKeyError
from nbe.frame import ETX, STX, FrameError, Fn, controller_id, decode_response, encode_request, parse_pairs
from nbe.model import BoilerData, BoilerReader, num, series
from tests.rsa_util import b64, generate, pkcs1, spki


def make_response(fn: int, seq: int, payload: str, status: int = 0, controller: str = "123456") -> bytes:
    raw = payload.encode()
    return (
        b"Burnerific__"
        + controller.encode()
        + bytes([STX])
        + f"{fn:02d}{seq:02d}{status}{len(raw):03d}".encode()
        + raw
        + bytes([ETX])
    )


# -- frames -------------------------------------------------------------------


def test_plain_request_layout() -> None:
    frame = encode_request(Fn.OPERATING_DATA, 7, "*", serial="12345678", now=1_700_000_000)
    assert frame[:12] == b"Burnerific__"
    assert frame[12:18] == b"123456"
    assert frame[18:19] == b" "
    body = frame[19:]
    assert body[0] == STX and body[-1] == ETX
    assert body[1:3] == b"04" and body[3:5] == b"07"
    assert body[5:15] == b"0000000000"  # no PIN on plain requests
    assert body[15:25] == b"1700000000"
    assert body[25:29] == b"pad "
    assert body[29:32] == b"001" and body[32:33] == b"*"


def test_controller_id() -> None:
    assert controller_id(None) == b"000000"
    assert controller_id("12345") == b"012345"
    assert controller_id("SN 98765432") == b"987654"


def test_decode_and_pairs() -> None:
    r = decode_response(make_response(4, 12, "boiler_temp=61.2;state=5;junk;x="))
    assert (r.fn, r.seq, r.status, r.ok) == (4, 12, 0, True)
    assert r.pairs() == {"boiler_temp": "61.2", "state": "5", "x": ""}
    with pytest.raises(FrameError):
        decode_response(b"nope")
    with pytest.raises(FrameError):
        decode_response(make_response(4, 1, "abc")[:-3])


def test_parse_pairs_keeps_equals_in_values() -> None:
    assert parse_pairs("rsa_key=AB==;a=1") == {"rsa_key": "AB==", "a": "1"}


def test_numbers() -> None:
    assert num("999.9") is None
    assert num("-999.9") is None
    assert num("12,5") == 12.5
    assert num("") is None and num("x") is None
    assert series("1.5,,2") == [1.5, 0.0, 2.0]


# -- crypto -------------------------------------------------------------------


@pytest.mark.parametrize("encode", [pkcs1, spki])
def test_rsa_key_formats_and_roundtrip(encode) -> None:
    n, e, d = generate()
    key = RsaKey.from_payload(b64(encode(n, e)))
    assert (key.n, key.e, key.size) == (n, e, 64)
    frame = encode_request(Fn.WRITE_SETTING, 3, "boiler.temp=70", serial="1", pin="1234567890", key=key)
    assert frame[18:19] == b"*"
    cipher = frame[19:]
    assert len(cipher) == 64
    plain = pow(int.from_bytes(cipher, "big"), d, n).to_bytes(64, "big")
    assert plain[0] == STX
    assert plain[5:15] == b"1234567890"
    size = int(plain[29:32])
    assert plain[32 : 32 + size] == b"boiler.temp=70"
    assert plain[32 + size] == ETX
    assert all(b < 128 for b in plain[33 + size :])  # 7-bit padding


def test_rsa_rejects_junk() -> None:
    with pytest.raises(RsaKeyError):
        RsaKey.from_payload(b64(b"\x30\x03\x02\x01\x05"))
    with pytest.raises(RsaKeyError):
        RsaKey.from_payload("not base64 at all!!")


def test_encrypted_payload_too_long() -> None:
    n, e, _ = generate()
    with pytest.raises(FrameError):
        encode_request(2, 1, "x" * 40, pin="1", key=RsaKey(n, e))


# -- client against a fake boiler ----------------------------------------------


class FakeBoiler(asyncio.DatagramProtocol):
    """Speaks just enough of the protocol: discovery, reads, key and encrypted writes."""

    def __init__(self, pin: str = "0123456789", drop_first: int = 0) -> None:
        self.n, self.e, self.d = generate()
        self.pin = pin
        self.settings = {"boiler": {"temp": "65"}, "hopper": {"content": "104", "auger_capacity": "312"}}
        self.drop = drop_first
        self.writes: list[str] = []
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport) -> None:
        self.transport = transport

    def datagram_received(self, data: bytes, addr) -> None:
        if self.drop > 0:
            self.drop -= 1
            return
        assert self.transport is not None
        if data[18:19] == b"*":
            body = pow(int.from_bytes(data[19:], "big"), self.d, self.n).to_bytes(64, "big")
        else:
            body = data[19:]
        fn, seq = int(body[1:3]), int(body[3:5])
        pin = body[5:15].decode().strip()
        size = int(body[29:32])
        payload = body[32 : 32 + size].decode()
        status, out = 0, ""
        if fn == Fn.DISCOVERY:
            out = "Serial=12345678;IP=127.0.0.1;Type=v13;Ver=1105"
        elif fn == Fn.OPERATING_DATA:
            out = "boiler_temp=61.5;boiler_ref=65;state=5;substate=0;power_pct=42;power_kw=6.8;dhw_temp=999.9;external_temp=4.5"
        elif fn == Fn.ADVANCED_DATA:
            out = "program_version=1105"
        elif fn == Fn.CONSUMPTION:
            out = {"counter": "counter=12345.6", "total_days": "total_days=" + ",".join(["10.0"] * 31)}.get(payload, "")
            status = 0 if out else 1
        elif fn == Fn.READ_SETTING and payload == "misc.rsa_key":
            out = "rsa_key=" + b64(spki(self.n, self.e))
        elif fn == Fn.READ_SETTING:
            group = payload.split(".")[0]
            if group in self.settings:
                out = ";".join(f"{k}={v}" for k, v in self.settings[group].items())
            else:
                status = 1
        elif fn == Fn.WRITE_SETTING:
            if data[18:19] != b"*" or pin != self.pin:
                status, out = 1, "bad pin"
            else:
                path, _, value = payload.partition("=")
                group, _, key = path.partition(".")
                self.settings.setdefault(group, {})[key] = value
                self.writes.append(payload)
        self.transport.sendto(make_response(fn, seq, out, status), addr)


@pytest.fixture
async def boiler():
    loop = asyncio.get_running_loop()
    transport, proto = await loop.create_datagram_endpoint(FakeBoiler, local_addr=("127.0.0.1", 0))
    yield proto, transport.get_extra_info("sockname")[1]
    transport.close()


async def test_client_reads(boiler) -> None:
    proto, port = boiler
    async with NbeClient("127.0.0.1", port=port, timeout=1) as client:
        assert client.info == DiscoveryInfo("12345678", "127.0.0.1", "v13", "1105", "127.0.0.1")
        assert client.info.generation == 13
        assert client.serial == "12345678"
        op = await client.operating_data()
        assert op["power_kw"] == "6.8"
        assert (await client.settings("hopper"))["content"] == "104"
        with pytest.raises(NbeRefused):
            await client.settings("vacuum")


async def test_client_retries_lost_packets(boiler) -> None:
    proto, port = boiler
    proto.drop = 1
    async with NbeClient("127.0.0.1", port=port, timeout=0.3, retries=2) as client:
        assert client.info is not None


async def test_client_timeout() -> None:
    client = NbeClient("127.0.0.1", port=9, timeout=0.2, retries=0)  # discard port: nobody answers
    with pytest.raises(NbeError):  # a timeout, or "port unreachable" where the OS reports it
        await client.discover()
    await client.close()


async def test_encrypted_write(boiler) -> None:
    proto, port = boiler
    async with NbeClient("127.0.0.1", port=port, pin="0123456789", timeout=1) as client:
        await client.write_setting("hopper", "content", 250.0)
    assert proto.writes == ["hopper.content=250"]
    assert proto.settings["hopper"]["content"] == "250"


async def test_wrong_pin_is_refused(boiler) -> None:
    proto, port = boiler
    async with NbeClient("127.0.0.1", port=port, pin="9999", timeout=1) as client:
        with pytest.raises(NbeRefused):
            await client.write_setting("boiler", "temp", 70)
    assert proto.writes == []


async def test_reader_normalizes(boiler) -> None:
    from datetime import datetime

    proto, port = boiler
    async with NbeClient("127.0.0.1", port=port, timeout=1) as client:
        reader = BoilerReader(client)
        data = await reader.read(datetime(2026, 10, 18, 12, 0))
    assert isinstance(data, BoilerData)
    assert data.boiler_temp == 61.5
    assert data.state == 5 and data.burning and not data.alarm
    assert data.dhw_temp is None  # 999.9 = no sensor
    assert data.outdoor_temp == 4.5
    assert data.hopper_kg == 104
    assert data.total_kg == 12345.6
    assert data.kg_on(datetime(2026, 10, 18).date()) == 10.0
    assert data.raw["settings/boiler/temp"] == "65"
