from __future__ import annotations

from datetime import date, datetime, timedelta
import math
import random

import pytest

from pellets import consumption
from pellets.climate import ClimateModel, expected_degree_days
from pellets.consumption import DayObs, MonthObs
from pellets.forecast import ForecastInput, default_season_end, run
from pellets.history import History
from pellets.inventory import Inventory, StockError

T0 = datetime(2026, 10, 18, 12, 0)


# -- inventory ------------------------------------------------------------------


def test_delivery_fill_and_average_price() -> None:
    inv = Inventory(bag_kg=16, silo_capacity_kg=300)
    inv.deliver(960, T0, price_per_kg=2.5)
    inv.deliver(640, T0, price_per_kg=3.0)
    assert inv.storage_kg == 1600
    assert inv.avg_price_per_kg == pytest.approx((960 * 2.5 + 640 * 3.0) / 1600)
    assert inv.spent_total == pytest.approx(960 * 2.5 + 640 * 3.0)

    inv.fill_silo(160, T0)
    assert (inv.storage_kg, inv.silo_kg, inv.total_kg) == (1440, 160, 1600)
    assert inv.silo_free_kg == 140
    assert inv.suggested_fill_kg() == 128  # 8 whole bags fit
    with pytest.raises(StockError):
        inv.fill_silo(5000, T0)
    inv.fill_silo(50, T0, from_storage=False)
    assert inv.storage_kg == 1440 and inv.silo_kg == 210

    inv.consume(10)
    assert inv.silo_kg == 200
    assert [e.kind for e in inv.events] == ["delivery", "delivery", "fill", "fill"]


def test_inventory_roundtrip_and_validation() -> None:
    inv = Inventory()
    inv.deliver(100, T0)
    inv.set_silo(40, T0)
    inv.set_storage(80, T0, note="counted")
    again = Inventory.from_dict(inv.to_dict())
    assert again == inv
    for bad in (lambda: inv.deliver(0, T0), lambda: inv.set_storage(-1, T0), lambda: inv.deliver(5, T0, price_per_kg=-1)):
        with pytest.raises(StockError):
            bad()


# -- climate --------------------------------------------------------------------


def synthetic_temps(years: float, seed: int = 1, mean: float = 9.0, amp: float = 8.0, phi: float = 0.8):
    rng = random.Random(seed)
    start = date(2020, 1, 1)
    anom = 0.0
    out = []
    for i in range(int(365 * years)):
        d = start + timedelta(days=i)
        doy = d.timetuple().tm_yday
        clim = mean - amp * math.cos(2 * math.pi * (doy - 20) / 365.25)  # coldest ~20 Jan
        sd = 3.0 + 0.8 * math.cos(2 * math.pi * (doy - 20) / 365.25)  # wider in winter
        anom = phi * anom + math.sqrt(1 - phi * phi) * sd * rng.gauss(0, 1)
        out.append((d, clim + anom))
    return out


def test_climate_fit_recovers_season() -> None:
    model = ClimateModel.fit(synthetic_temps(6))
    assert model.source == "history"
    assert model.mean(date(2026, 1, 20)) == pytest.approx(1.0, abs=0.8)
    assert model.mean(date(2026, 7, 21)) == pytest.approx(17.0, abs=0.8)
    assert model.phi == pytest.approx(0.8, abs=0.06)
    assert model.sd(date(2026, 1, 20)) > model.sd(date(2026, 7, 20))
    assert ClimateModel.from_dict(model.to_dict()) == model


def test_default_climate_is_sane_and_mirrors_south() -> None:
    north = ClimateModel.default(55.7)
    south = ClimateModel.default(-33.9)
    assert north.mean(date(2026, 1, 25)) < 2 < 15 < north.mean(date(2026, 7, 21))
    assert south.mean(date(2026, 7, 21)) < south.mean(date(2026, 1, 21))
    assert north.sd(date(2026, 1, 20)) > north.sd(date(2026, 7, 20))


def test_expected_degree_days() -> None:
    assert expected_degree_days(17, 5, 0) == 12
    assert expected_degree_days(17, 25, 0) == 0
    # A mild mean still has cold tails.
    assert 0 < expected_degree_days(17, 17, 3) == pytest.approx(3 / math.sqrt(2 * math.pi))


# -- consumption model ------------------------------------------------------------


def house(days: int, *, limit=15.5, base=1.8, k=1.1, sigma=1.2, seed=3, start=date(2025, 9, 1)):
    """Simulate a house: daily kg from smoothed temperatures, like the model assumes."""
    rng = random.Random(seed)
    temps = {d: t for d, t in synthetic_temps(3, seed=seed + 10)}
    out = []
    teff = None
    for i in range(days):
        d = start + timedelta(days=i)
        t = temps[date(2020, 1, 1) + timedelta(days=(d - date(2020, 1, 1)).days % len(temps))]
        teff = t if teff is None else 0.7 * t + 0.3 * teff
        kg = max(0.0, base + k * max(0.0, limit - teff) + rng.gauss(0, sigma))
        out.append(DayObs(d, kg, t))
    return out


def test_fit_predicts_a_heating_season() -> None:
    # September to April: few warm days, so base load and heating limit trade off,
    # but predictions across the temperatures seen must be right.
    days = house(240)
    model = consumption.fit(days, ClimateModel.default(55), today=days[-1].day + timedelta(days=1))
    assert model.n_days == 240
    for temp in (-5.0, 0.0, 5.0, 10.0):
        assert model.expected(temp) == pytest.approx(1.8 + 1.1 * (15.5 - temp), rel=0.06)
    assert model.heating_limit == pytest.approx(15.5, abs=1.5)
    assert model.kg_per_degree_day == pytest.approx(1.1, rel=0.1)
    assert model.sigma == pytest.approx(1.2, rel=0.3)
    assert model.r2 is not None and model.r2 > 0.9
    assert abs(sum(c.weight for c in model.candidates) - 1) < 1e-9


def test_full_year_pins_down_base_load() -> None:
    days = house(365, seed=11)
    model = consumption.fit(days, ClimateModel.default(55), today=days[-1].day + timedelta(days=1))
    assert model.base_load == pytest.approx(1.8, abs=0.5)
    assert model.heating_limit == pytest.approx(15.5, abs=1.0)


def test_prior_only_matches_yearly_guess() -> None:
    climate = ClimateModel.default(55)
    today = date(2026, 10, 18)
    model = consumption.fit([], climate, today=today, prior_annual_kg=5000)
    yearly = sum(
        model.expected(climate.mean(today + timedelta(days=i))) for i in range(365)
    )
    # Expected burn on mean temperatures ignores cold spells, so it sits a little under the guess.
    assert 0.7 * 5000 < yearly < 1.1 * 5000


def test_outliers_do_not_wreck_the_fit() -> None:
    days = house(150, seed=5)
    broken = [DayObs(d.day, 0.0 if i % 25 == 0 else d.kg, d.temp) for i, d in enumerate(days)]  # boiler off some days
    clean = consumption.fit(days, ClimateModel.default(55), today=days[-1].day)
    dirty = consumption.fit(broken, ClimateModel.default(55), today=days[-1].day)
    assert dirty.kg_per_degree_day == pytest.approx(clean.kg_per_degree_day, rel=0.12)


def test_months_inform_the_model() -> None:
    climate = ClimateModel.default(55)
    months = [MonthObs(2026, m, kg, 30) for m, kg in ((1, 700.0), (2, 620.0), (3, 520.0), (4, 330.0))]
    model = consumption.fit([], climate, today=date(2026, 10, 18), prior_annual_kg=2000, months=months)
    # Four months already burned ~2200 kg, so the model must believe in a hungrier house than 2000 kg/year.
    yearly = sum(model.expected(climate.mean(date(2026, 1, 1) + timedelta(days=i))) for i in range(365))
    assert yearly > 2600


# -- forecast ---------------------------------------------------------------------


def fitted_model():
    days = house(200)
    return consumption.fit(days, ClimateModel.default(55), today=days[-1].day)


def test_forecast_quantiles_are_ordered_and_plausible() -> None:
    climate = ClimateModel.default(55)
    model = fitted_model()
    today = date(2026, 11, 1)
    inp = ForecastInput(
        today=today,
        day_fraction_left=1.0,
        stock_kg=600.0,
        silo_kg=150.0,
        temp_forecast={today + timedelta(days=i): 2.0 for i in range(7)},
        season_end=default_season_end(today, 55),
        seed=42,
        paths=300,
    )
    r = run(inp, model, climate)
    p10, p50, p90 = (r.days_left[k] for k in ("p10", "p50", "p90"))
    assert p10 <= p50 <= p90
    # About 1.8 + 1.1·13.5 ≈ 16.7 kg/day at 2 °C, a bit less as November goes on.
    assert 25 < p50 < 55
    assert r.silo_empty["p50"] < r.runout["p50"]
    assert r.kg_next_7 == pytest.approx(7 * (1.8 + 1.1 * (15.5 - 2.0)), rel=0.2)
    assert r.prob_runout_before_season_end == 1.0
    assert r.order_kg and r.order_kg > 1500
    assert len(r.curve) == 30


def test_forecast_is_deterministic_for_a_seed() -> None:
    climate = ClimateModel.default(55)
    model = fitted_model()
    inp = ForecastInput(today=date(2026, 11, 1), day_fraction_left=0.5, stock_kg=900, silo_kg=None, seed=1, paths=100)
    a, b = run(inp, model, climate), run(inp, model, climate)
    assert a.runout == b.runout
    assert a.silo_empty == {"p10": None, "p50": None, "p90": None}


def test_summer_stock_lasts_and_season_need_is_zero_ish() -> None:
    climate = ClimateModel.default(55)
    model = fitted_model()
    today = date(2026, 6, 15)
    inp = ForecastInput(
        today=today, day_fraction_left=1, stock_kg=4000, silo_kg=200, season_end=date(2026, 9, 1), seed=3, paths=150
    )
    r = run(inp, model, climate)
    assert r.need_until_season_end["p90"] < 600  # summer: hot water only
    assert r.order_kg == 0
    assert r.runout["p50"] is not None and r.runout["p50"] > date(2026, 12, 1)


def test_colder_forecast_runs_out_sooner() -> None:
    climate = ClimateModel.default(55)
    model = fitted_model()
    today = date(2026, 12, 1)

    def days_left(temp: float) -> float:
        inp = ForecastInput(
            today=today,
            day_fraction_left=1,
            stock_kg=300,
            silo_kg=None,
            temp_forecast={today + timedelta(days=i): temp for i in range(10)},
            seed=9,
            paths=200,
        )
        return run(inp, model, climate).days_left["p50"]

    assert days_left(-8.0) < days_left(6.0)


def test_season_end_defaults() -> None:
    assert default_season_end(date(2026, 10, 18), 55) == date(2027, 6, 1)
    assert default_season_end(date(2026, 3, 1), 55) == date(2026, 6, 1)
    assert default_season_end(date(2026, 3, 1), -34) == date(2026, 12, 1)


# -- history ----------------------------------------------------------------------


def test_history_closes_full_days_from_the_counter() -> None:
    h = History.empty()
    t = datetime(2026, 10, 17, 0, 0)
    counter = 1000.0
    closed_days = []
    for i in range(24 * 6):  # every 10 minutes from 00:00 to 23:50
        now = t + timedelta(minutes=10 * i)
        closed_days += h.observe(now, counter, 5.0 + (i % 6))
        counter += 0.1
    assert closed_days == []  # the very first day started mid-flight
    closed_days += h.observe(datetime(2026, 10, 18, 0, 5), counter, 5.0)
    assert closed_days == [date(2026, 10, 17)]
    rec = h.days["2026-10-17"]
    # The 17th was the first day seen, so it is not trusted as a full day, and no controller figure was given.
    assert rec.kg is None
    assert rec.temp == pytest.approx(7.5, abs=0.2)

    # The 18th is seen from 00:05 continuously: a full day.
    now = datetime(2026, 10, 18, 0, 5)
    start = counter
    while now < datetime(2026, 10, 19, 0, 0):
        now += timedelta(minutes=10)
        counter += 0.2
        h.observe(now, counter, 3.0)
    rec = h.days["2026-10-18"]
    assert rec.source == "counter"
    assert rec.kg == pytest.approx(counter - start, abs=0.3)
    assert rec.temp == pytest.approx(3.0, abs=0.1)  # the 00:05 reading of 5 °C holds for 10 minutes
    obs = h.observations()
    assert [o.day for o in obs] == [date(2026, 10, 18)]
    assert History.from_dict(h.to_dict()).days == h.days


def test_history_uses_controller_figure_and_backfill() -> None:
    h = History.empty()
    h.observe(datetime(2026, 10, 17, 15, 0), 100.0, 4.0)
    h.observe(datetime(2026, 10, 18, 0, 1), 120.0, 4.0, boiler_day_kg=lambda d: 22.5)
    assert h.days["2026-10-17"].kg == 22.5
    assert h.days["2026-10-17"].source == "controller"
    h.backfill(date(2026, 10, 10), 30.0, 1.5, "controller")
    h.backfill(date(2026, 10, 17), 99.0, None, "controller")  # never overwrites
    assert h.days["2026-10-17"].kg == 22.5
    assert DayObs(date(2026, 10, 10), 30.0, 1.5) in h.observations()
    assert h.burned_today(125.0) == 5.0
