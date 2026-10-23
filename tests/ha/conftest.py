"""Fixtures for running Burnerific inside a real Home Assistant test instance."""

from __future__ import annotations

import asyncio
import math
from datetime import date, timedelta

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.burnerific.const import DOMAIN
from custom_components.burnerific.open_meteo import ARCHIVE_URL, FORECAST_URL
from tests.test_protocol import FakeBoiler

PIN = "0123456789"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    return


@pytest.fixture
def profile(request) -> str:
    """Override with ``@pytest.mark.parametrize("profile", ["v13"], indirect=True)``."""
    return getattr(request, "param", "basic")


@pytest.fixture
async def fake_boiler(socket_enabled, profile):
    """A protocol-level fake controller on localhost. Yields (boiler, "127.0.0.1:port")."""
    loop = asyncio.get_running_loop()
    transport, proto = await loop.create_datagram_endpoint(
        lambda: FakeBoiler(pin=PIN, profile=profile), local_addr=("127.0.0.1", 0)
    )
    yield proto, f"127.0.0.1:{transport.get_extra_info('sockname')[1]}"
    transport.close()


def _daily(start: date, days: int) -> dict:
    times, temps, winds, suns = [], [], [], []
    for i in range(days):
        d = start + timedelta(days=i)
        doy = d.timetuple().tm_yday
        times.append(d.isoformat())
        temps.append(round(8.5 - 8 * math.cos(2 * math.pi * (doy - 20) / 365.25), 1))
        winds.append(round(7 + 3 * math.sin(i), 1))
        suns.append(round(max(0.0, 10 - 9 * math.cos(2 * math.pi * (doy + 10) / 365.25)), 1))
    return {
        "daily": {
            "time": times,
            "temperature_2m_mean": temps,
            "wind_speed_10m_max": winds,
            "shortwave_radiation_sum": suns,
        }
    }


@pytest.fixture
def open_meteo(aioclient_mock):
    today = date.today()
    aioclient_mock.get(ARCHIVE_URL, json=_daily(today - timedelta(days=3 * 365), 3 * 365 - 6))
    aioclient_mock.get(FORECAST_URL, json=_daily(today - timedelta(days=62), 62 + 16))
    return aioclient_mock


@pytest.fixture
def entry(fake_boiler) -> MockConfigEntry:
    _, host = fake_boiler
    return MockConfigEntry(
        domain=DOMAIN,
        title="NBE v13 12345678",
        unique_id="12345678",
        version=2,
        data={"host": host, "serial": "12345678", "pin": PIN, "initial_storage_kg": 480},
        options={"bag_kg": 16, "silo_capacity_kg": 300, "annual_kg": 4000},
    )
