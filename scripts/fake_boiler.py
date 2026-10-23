"""A simulated NBE controller for development. Speaks the real UDP protocol on port 8483.

    python scripts/fake_boiler.py [--port 8483] [--pin 0123456789] [--speed 1]

It answers discovery (so the config flow finds it), reads, and PIN protected
encrypted writes. The house heats with the weather, the boiler cycles between
burning and "temperature reached", and the burn counter, silo content and day
and month arrays move like on a real controller. ``--speed 60`` burns pellets
60x faster so the stock visibly drops while you watch.
"""

from __future__ import annotations

import argparse
import asyncio
import math
import random
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.rsa_util import b64, generate, spki  # noqa: E402

STX, ETX = 0x02, 0x04
KWH_PER_KG = 4.8


def response(fn: int, seq: int, payload: str, status: int = 0, controller: str = "123456") -> bytes:
    raw = payload.encode()
    return (
        b"NBE_Fake____"
        + controller.encode()
        + bytes([STX])
        + f"{fn:02d}{seq:02d}{status}{len(raw):03d}".encode()
        + raw
        + bytes([ETX])
    )


class Boiler:
    def __init__(self, pin: str, speed: float) -> None:
        self.pin = pin
        self.speed = speed
        self.n, self.e, self.d = generate(seed=random.randint(1, 10_000))
        self.serial = "12345678"
        self.boiler_temp = 55.0
        self.state = 5
        self.counter = 14_200.0
        self.last = datetime.now()
        today = self.last
        self.days = [round(max(2.0, 22 - 0.8 * self._outdoor(today) + random.gauss(0, 2)), 1) for _ in range(31)]
        self.days[today.day - 1] = 0.0
        self.months = [610, 540, 470, 300, 140, 60, 45, 45, 110, 280, 450, 590]
        self.months[today.month - 1] = round(sum(self.days[: today.day - 1]), 1)
        self.settings: dict[str, dict[str, str]] = {
            "boiler": {"temp": "65", "diff_over": "5", "diff_under": "5"},
            "hot_water": {"temp": "52"},
            "hopper": {"content": "180", "auger_capacity": "312"},
            "auger": {"kw_min": "4", "kw_max": "16"},
            "regulation": {"boiler_power_min": "30", "boiler_power_max": "100"},
            "misc": {"start": "0", "stop": "0", "reset_alarm": "0"},
        }

    @staticmethod
    def _outdoor(now: datetime) -> float:
        return 6.0 + 3.0 * math.sin(2 * math.pi * (now.hour + now.minute / 60 - 9) / 24)

    def step(self) -> None:
        now = datetime.now()
        prev, self.last = self.last, now
        dt = (now - prev).total_seconds()
        if now.date() != prev.date():
            self.days[now.day - 1] = 0.0  # the rolling array starts the new day from zero
            if now.month != prev.month:
                self.months[now.month - 1] = 0.0
        ref = float(self.settings["boiler"]["temp"])
        outdoor = self._outdoor(now)
        demand_kw = max(1.0, 0.75 * (18 - outdoor))  # what the house pulls out of the boiler
        if self.state == 5 and self.boiler_temp > ref + 4:
            self.state = 9
        elif self.state == 9 and self.boiler_temp < ref - 4:
            self.state = 5
        power_kw = self.power_kw(outdoor) if self.state == 5 else 0.0
        self.boiler_temp += (power_kw - demand_kw) * dt / 600.0
        self.boiler_temp = min(90.0, max(20.0, self.boiler_temp))
        kg = power_kw / KWH_PER_KG * dt / 3600.0 * self.speed
        hopper = float(self.settings["hopper"]["content"])
        if kg > 0 and hopper <= 0:
            self.state = 13  # out of pellets: ignition alarm, like the real thing
            return
        self.counter += kg
        self.days[now.day - 1] += kg
        self.months[now.month - 1] += kg
        self.settings["hopper"]["content"] = f"{max(0.0, hopper - kg):.1f}"

    def power_kw(self, outdoor: float) -> float:
        return min(16.0, max(4.0, 0.9 * (18 - outdoor) + 3)) if self.state == 5 else 0.0

    def operating(self) -> str:
        outdoor = self._outdoor(datetime.now())
        kw = self.power_kw(outdoor)
        pct = round(100 * kw / 16)
        vals = {
            "boiler_temp": f"{self.boiler_temp:.1f}",
            "boiler_ref": self.settings["boiler"]["temp"],
            "return_temp": f"{self.boiler_temp - 12:.1f}",
            "smoke_temp": f"{(90 + 6 * kw) if kw else 45:.0f}",
            "shaft_temp": "32",
            "dhw_temp": "49.5",
            "dhw_ref": self.settings["hot_water"]["temp"],
            "external_temp": f"{outdoor:.1f}",
            "oxygen": f"{7.5 + random.gauss(0, 0.3):.1f}" if kw else "20.9",
            "oxygen_ref": "8.0",
            "photo_level": "92" if kw else "0",
            "fan_speed": str(round(30 + 0.5 * pct)) if kw else "0",
            "exhaust_speed": "999.9",
            "power_pct": str(pct),
            "power_kw": f"{kw:.1f}",
            "state": str(self.state),
            "substate": "0",
            "substate_sec": "0",
            "boiler_pump_state": "1",
            "house_pump_state": "1",
            "dhw_valve_state": "0",
        }
        return ";".join(f"{k}={v}" for k, v in vals.items())

    def handle(self, data: bytes) -> bytes | None:
        self.step()
        encrypted = data[18:19] == b"*"
        body = pow(int.from_bytes(data[19:], "big"), self.d, self.n).to_bytes(64, "big") if encrypted else data[19:]
        try:
            fn, seq = int(body[1:3]), int(body[3:5])
            pin = body[5:15].decode().strip()
            size = int(body[29:32])
            payload = body[32 : 32 + size].decode()
        except ValueError:
            return None
        status, out = 0, ""
        if fn == 0:
            out = f"Serial={self.serial};IP=127.0.0.1;Type=v13;Ver=1105"
        elif fn == 4:
            out = self.operating()
        elif fn == 5:
            out = "program_version=1105;outdoor_temp=999.9"
        elif fn == 6:
            out = {
                "counter": f"counter={self.counter:.1f}",
                "total_days": "total_days=" + ",".join(f"{v:.1f}" for v in self.days),
                "total_months": "total_months=" + ",".join(f"{v:.0f}" for v in self.months),
            }.get(payload, "")
            status = 0 if out else 1
        elif fn == 1 and payload == "misc.rsa_key":
            out = "rsa_key=" + b64(spki(self.n, self.e))
        elif fn == 1:
            group = payload.split(".")[0]
            if group in self.settings:
                out = ";".join(f"{k}={v}" for k, v in self.settings[group].items())
            else:
                status = 1
        elif fn == 2:
            if not encrypted or pin != self.pin:
                status, out = 1, "wrong pin"
            else:
                path, _, value = payload.partition("=")
                group, _, key = path.partition(".")
                if group not in self.settings:
                    status, out = 1, "unknown setting"
                else:
                    self.settings[group][key] = value
                    if path == "misc.stop":
                        self.state = 14
                    elif path in ("misc.start", "misc.reset_alarm"):
                        self.state = 5
                    print(f"write {payload}", flush=True)
        else:
            status = 1
        return response(fn, seq, out, status)


class Protocol(asyncio.DatagramProtocol):
    def __init__(self, boiler: Boiler) -> None:
        self.boiler = boiler

    def connection_made(self, transport) -> None:
        self.transport = transport

    def datagram_received(self, data: bytes, addr) -> None:
        if (out := self.boiler.handle(data)) is not None:
            self.transport.sendto(out, addr)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=8483)
    parser.add_argument("--pin", default="0123456789")
    parser.add_argument("--speed", type=float, default=1.0, help="burn pellets this many times faster")
    args = parser.parse_args()
    boiler = Boiler(args.pin, args.speed)
    loop = asyncio.get_running_loop()
    await loop.create_datagram_endpoint(lambda: Protocol(boiler), local_addr=("0.0.0.0", args.port), allow_broadcast=True)
    print(f"Fake NBE boiler on udp/{args.port}, serial {boiler.serial}, PIN {args.pin}, speed x{args.speed}", flush=True)
    await asyncio.Event().wait()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
