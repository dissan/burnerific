"""Tests for fill logs, calibration, lots, maintenance, economy, wind/sun, event log and friends."""

from __future__ import annotations

import math
import random
from datetime import date, datetime, timedelta

import pytest
from nbe import NbeClient
from nbe.client import subnet_hosts
from nbe.eventlog import parse as parse_events
from nbe.eventlog import request_payload
from pellets import consumption, economy, fills, maintenance
from pellets.climate import ClimateModel
from pellets.consumption import DayObs, PeriodObs, Weather
from pellets.forecast import ForecastInput, run
from pellets.inventory import UNKNOWN_LOT, Inventory

from tests.test_protocol import FakeBoiler

T0 = datetime(2026, 10, 18, 12, 0)

# A real fill log from winter 2025/26 (sacks of 16 kg) and that boiler's monthly totals from its controller.
WINTER_LOG = """
04-10-2025\t6
15-10-2025\t6
20-10-2025\t4
24-10-2025\t4
28-10-2025\t5
31-10-2025\t5
04-11-2025\t6
09-11-2025\t5
13-11-2025\t5
16-11-2025\t6
19-11-2025\t5
22-11-2025\t8
26-11-2025\t8
30-11-2025\t8
4-12-2025\t8
8-12-2025\t8
14-12-2025\t8
18-12-2025\t8
22-12-2025\t8
26-12-2025\t8
31-12-2025\t8
03-01-2026\t8
06-01-2026\t8
09-01-2026\t8
12-01-2026\t8
16-01-2026\t8
20-01-2026\t8
23-01-2026\t8
27-01-2026\t8
31-01-2026\t8
02-02-2026\t8
05-02-2026\t8
09-02-2026\t8
12-02-2026\t8
15-02-2026\t8
18-02-2026\t8
22-02-2026\t8
26-02-2026\t8
02-03-2026\t8
07-03-2026\t8
12-03-2026\t8
17-03-2026\t8
22-03-2026\t8
27-03-2026\t4
"""
CONTROLLER_MONTHS = {(2025, 11): 792.951, (2025, 12): 831.277, (2026, 1): 1120.901, (2026, 2): 1047.674, (2026, 3): 834.398}


# -- event log ----------------------------------------------------------------------


def test_event_log_parses_real_v7_payload() -> None:
    payload = "261023,160313,1,0,2,5, ;261023,160007,1,0,23,2, ;261023,080002,1,0,5,23, ;garbage;"
    entries = parse_events(payload)
    assert [(e.at, e.before, e.after) for e in entries] == [
        (datetime(2026, 10, 23, 16, 3, 13), 2, 5),
        (datetime(2026, 10, 23, 16, 0, 7), 23, 2),
        (datetime(2026, 10, 23, 8, 0, 2), 5, 23),
    ]
    assert all(e.is_state_change for e in entries)
    assert request_payload(datetime(2026, 10, 23, 16, 46, 54)) == "261023:164654;"


@pytest.fixture(autouse=True)
def _allow_sockets(request):
    try:
        request.getfixturevalue("socket_enabled")
    except pytest.FixtureLookupError:
        pass


@pytest.fixture
async def fake():
    import asyncio

    loop = asyncio.get_running_loop()
    transport, proto = await loop.create_datagram_endpoint(lambda: FakeBoiler(profile="v13"), local_addr=("127.0.0.1", 0))
    yield proto, transport.get_extra_info("sockname")[1]
    transport.close()


async def test_client_event_log_and_pin_check(fake) -> None:
    proto, port = fake
    async with NbeClient(f"127.0.0.1:{port}", pin="0123456789", timeout=1) as client:
        entries = await client.event_log(T0)
        assert [(e.before, e.after) for e in entries] == [(2, 5), (23, 2)]
        assert await client.validate_pin() is True
    assert proto.writes == ["misc.dummy=0"]
    async with NbeClient(f"127.0.0.1:{port}", pin="9999", timeout=1) as client:
        assert await client.validate_pin() is False


def test_subnet_hosts() -> None:
    hosts = subnet_hosts(["192.168.1.109/24", "10.0.0.0/8", "fe80::1/64", "junk"], own=["192.168.1.109"])
    assert len(hosts) == 253 and hosts[0] == "192.168.1.1" and "192.168.1.109" not in hosts


# -- fill logs and calibration --------------------------------------------------------


def test_parse_fill_log_formats() -> None:
    fl = fills.parse_log(WINTER_LOG, 16)
    assert len(fl) == 44 and fl[0] == fills.Fill(date(2025, 10, 4), 96.0) and fl[-1].kg == 64
    other = fills.parse_log("# my log\n2025-10-04;6,5\n2025-10-04,1\n\n2025/10/09  3", 1)  # ";" with decimal comma, CSV
    assert other == [fills.Fill(date(2025, 10, 4), 7.5), fills.Fill(date(2025, 10, 9), 3.0)]
    with pytest.raises(fills.FillLogError):
        fills.parse_log("not a date 5", 16)


def test_fill_periods_and_month_calibration() -> None:
    fl = fills.parse_log(WINTER_LOG, 16)
    periods = fills.periods(fl, lambda d: Weather(2.0))
    assert all(p.days >= 14 or p is periods[-1] for p in periods)
    # Everything after the first fill is accounted for exactly once.
    assert sum(p.kg for p in periods) == pytest.approx(sum(f.kg for f in fl[1:]))
    cal = fills.calibrate_from_months(fl, lambda y, m: CONTROLLER_MONTHS.get((y, m)))
    # Nov-Feb: 3960 kg filled vs 3793 kg counted (March is not fully covered by the log).
    assert cal is not None and cal.source == "log"
    assert cal.factor == pytest.approx(3960 / 3792.8, rel=0.02)


def test_counter_calibration() -> None:
    rows = [(date(2026, 1, 1) + timedelta(days=4 * i), 128.0, 1000.0 + 4 * i * 30.0) for i in range(12)]
    cal = fills.calibrate_from_counter(rows, 220)
    assert cal is not None and cal.factor == pytest.approx(11 * 128 / (44 * 30.0))
    assert fills.calibrate_from_counter(rows[:2], 220) is None


def test_fill_log_teaches_the_model() -> None:
    """A model fitted only on fill intervals predicts winter burn close to what was filled."""
    clim = ClimateModel.default(57)
    fl = fills.parse_log(WINTER_LOG, 16)
    rng = random.Random(1)
    periods = fills.periods(fl, lambda d: Weather(clim.mean(d) + rng.gauss(0, 2), 7.0, clim.sun(d)))
    model = consumption.fit([], clim, today=date(2026, 4, 1), prior_annual_kg=4000, periods=periods)
    assert model.n_periods == len(periods)
    jan = sum(model.expected(clim.mean(date(2026, 1, 1) + timedelta(days=i))) for i in range(31))
    assert 900 < jan < 1400  # the log says ~1170 kg in January


# -- lots ------------------------------------------------------------------------------


def test_lots_flow_fifo_into_the_silo() -> None:
    inv = Inventory(bag_kg=16, silo_capacity_kg=220)
    inv.deliver(320, T0, price_per_kg=3.0, supplier="A")
    inv.deliver(320, T0, price_per_kg=2.5, supplier="B")
    inv.fill_silo(200, T0)
    assert inv.burning_lot.supplier == "A"
    inv.fill_silo(160, T0)  # 120 of A then 40 of B
    inv.consume(330)  # burns all of A
    assert inv.burning_lot.supplier == "B"
    assert sum(p.kg for p in inv.silo_lots) == pytest.approx(inv.silo_kg)
    again = Inventory.from_dict(inv.to_dict())
    assert again.burning_lot.supplier == "B"
    legacy = Inventory.from_dict({"storage_kg": 100.0, "silo_kg": 50.0})
    assert legacy.storage_lots[0].lot == UNKNOWN_LOT and legacy.burning_lot is None


# -- maintenance -------------------------------------------------------------------------


def test_maintenance_by_kg_and_days() -> None:
    today = date(2026, 10, 18)
    ash = maintenance.TaskSpec("ash", interval_kg=maintenance.ash_interval_kg(4.0, 0.7))
    assert ash.interval_kg == pytest.approx(571.4, abs=0.1)
    state = maintenance.TaskState(burned_at=1000.0)
    st = maintenance.status(ash, state, burned_total=1400.0, today=today, expected_kg=[20.0] * 400)
    assert st.kg_left == pytest.approx(171.4, abs=0.1) and not st.due
    assert st.due_date == today + timedelta(days=8)  # 171 kg at 20 kg/day: the 9th day
    sweep = maintenance.TaskSpec("sweep", interval_days=365)
    st = maintenance.status(maintenance.TaskSpec("sweep", interval_days=365), maintenance.TaskState("2025-10-01"),
                            burned_total=0, today=today, expected_kg=[])
    assert st.due and st.due_date == today
    maintenance.mark_done(state, today, 1400.0)
    assert maintenance.status(sweep, state, burned_total=1400.0, today=today, expected_kg=[]).fraction == 0


# -- economy -----------------------------------------------------------------------------


def test_heat_price_against_heat_pump() -> None:
    h = economy.heat_price(3.0, 4.8, 0.88, 2.5, 3.5)
    assert h.pellet_per_kwh_fuel == pytest.approx(0.625)
    assert h.pellet_per_kwh_heat == pytest.approx(0.7102, abs=1e-3)
    assert h.heat_pump_per_kwh_heat == pytest.approx(0.7143, abs=1e-3)
    assert h.pellets_cheaper_pct == pytest.approx(0.6, abs=0.1)
    assert economy.heat_price(None, 4.8, 0.88, None, 3.5).pellet_per_kwh_heat is None


def test_lot_quality_and_season_trend() -> None:
    rows = [(1, 21.0, 20.0)] * 10 + [(2, 19.0, 20.0)] * 10 + [(3, 5.0, 5.0)] * 3
    q = economy.lot_quality(rows, lambda lot: (f"S{lot}", "2026-01-01"))
    assert [(x.lot, round(x.index, 2)) for x in q] == [(1, 1.05), (2, 0.95)]  # lot 3 has too few days
    days = [(date(2025, 11, 1) + timedelta(days=i), 20.0, 25.0) for i in range(100)]
    days += [(date(2026, 11, 1) + timedelta(days=i), 25.0, 25.0) for i in range(100)]
    trend = economy.season_trend(days, normal_year_kg=6000)
    assert [t.season for t in trend] == ["2025/26", "2026/27"]
    assert trend[0].normalized_kg == pytest.approx(4800) and trend[1].normalized_kg == pytest.approx(6000)


# -- wind and sun --------------------------------------------------------------------------


def test_model_learns_wind_and_sun() -> None:
    rng = random.Random(4)
    clim = ClimateModel.default(57)
    start = date(2025, 9, 1)
    teff = None
    obs = []
    for i in range(300):
        d = start + timedelta(days=i)
        t = clim.mean(d) + rng.gauss(0, 3)
        wind = max(1.0, rng.gauss(7, 3))
        sun = max(0.0, clim.sun(d) * rng.uniform(0.2, 1.3))
        teff = t if teff is None else 0.7 * t + 0.3 * teff
        hdd = max(0.0, 17 - teff)
        kg = 2.4 + 1.8 * hdd * (1 + 0.25 * (wind - 7) / 5) - 0.12 * sun * min(1, hdd / 3) + rng.gauss(0, 1.5)
        obs.append(DayObs(d, max(0.0, kg), t, wind, sun))
    m = consumption.fit(obs, clim, today=obs[-1].day, prior_annual_kg=6000, wind_ref=7.0)
    assert m.heating_limit == pytest.approx(17, abs=1)
    assert m.wind_effect == pytest.approx(0.25, abs=0.06)
    assert m.sun_kg_per_mj == pytest.approx(0.12, abs=0.04)
    assert m.expected(0, wind=15) > m.expected(0, wind=3)


def test_climate_learns_sun_and_wind() -> None:
    rows = []
    rng = random.Random(2)
    for i in range(3 * 365):
        d = date(2022, 1, 1) + timedelta(days=i)
        doy = d.timetuple().tm_yday
        rows.append((d, 8 - 8 * math.cos(2 * math.pi * (doy - 20) / 365.25) + rng.gauss(0, 2),
                     6.5 + rng.gauss(0, 2), max(0.0, 10 - 9 * math.cos(2 * math.pi * (doy - 172 + 182) / 365.25))))
    clim = ClimateModel.fit(rows)
    assert clim.wind_mean == pytest.approx(6.5, abs=0.3)
    assert clim.sun(date(2026, 6, 21)) > 15 > 3 > clim.sun(date(2026, 12, 21))


def test_forecast_bands_and_extra_burn() -> None:
    clim = ClimateModel.default(57)
    model = consumption.fit([], clim, today=date(2026, 11, 1), prior_annual_kg=6000)
    base = ForecastInput(today=date(2026, 11, 1), day_fraction_left=1, stock_kg=2000, silo_kg=200, seed=5, paths=150)
    r = run(base, model, clim)
    assert r.bands[0]["p50"] == pytest.approx(2000, abs=1)
    for a, b in zip(r.bands, r.bands[1:], strict=False):
        assert b["p50"] <= a["p50"] and b["p10"] <= b["p50"] <= b["p90"]
    base.extra_daily_kg = 3.0
    assert run(base, model, clim).days_left["p50"] < r.days_left["p50"]


def test_period_obs_month_helper() -> None:
    m = consumption.MonthObs(2026, 1, 1120.0, 31, tuple([0.0] * 31))
    assert isinstance(m, PeriodObs) and m.start == date(2026, 1, 1) and m.end == date(2026, 2, 1)
    assert m.weather[0] == Weather(0.0)


def test_stokercloud_normalize() -> None:
    pytest.importorskip("aiohttp")
    from nbe.stokercloud import normalize

    d = {
        "notconnected": 0,
        "frontdata": [{"id": "boilertemp", "value": "61.2"}, {"id": "hoppercontent", "value": "120"},
                      {"id": "dhw", "value": "0"}, {"id": "dhwwanted", "value": "0"}],
        "boilerdata": [{"id": 3, "value": "130"}, {"id": 4, "value": "45"}, {"id": 5, "value": "7.2"}],
        "hopperdata": [{"id": 3, "value": "14.5"}, {"id": 4, "value": "48500"}],
        "weatherdata": [{"id": 1, "value": "6.0"}],
        "miscdata": {"state": {"value": "state_5"}, "substate": {"value": ""}},
        "leftoutput": {"output-2": {"val": "ON"}},
    }
    data = normalize(d, T0)
    assert data.source == "cloud" and data.state == 5 and data.burning
    assert data.boiler_temp == 61.2 and data.power_kw == 7.2 and data.hopper_kg == 120
    assert data.dhw_temp is None and data.boiler_pump is True
    assert data.kg_on(T0.date()) == 14.5 and data.total_kg == 48500
