"""When will the pellets run out? Monte Carlo over weather and model uncertainty.

Each simulated future ("path"):

1. Draws a consumption model from the posterior (heating limit, base load,
   kg/degree-day and daily noise σ), so we are honest about how well we know the house.
2. Walks forward day by day. While a weather forecast exists the temperature
   is the forecast plus an error that grows with lead time and is correlated
   from day to day. After the forecast runs out, the forecast's last anomaly
   decays towards the seasonal climate as an AR(1) process with the location's
   own persistence and seasonal variability.
3. Burns pellets: smoothed temperature -> expected kg -> plus daily noise.
4. Records when the silo empties and when the whole stock does.

Quantiles over a few hundred paths give the run-out date with an honest
spread (P10 = only 10 % chance it happens earlier).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
import math
import random

from .climate import ClimateModel
from .consumption import SMOOTHING, ConsumptionModel

FORECAST_ERROR_GROWTH = 0.28  # °C of extra error per day of lead time
FORECAST_ERROR_BASE = 0.8  # °C error of today's forecast mean
FORECAST_ERROR_PHI = 0.85  # forecast errors persist from day to day


@dataclass(slots=True)
class ForecastInput:
    today: date
    day_fraction_left: float
    stock_kg: float
    silo_kg: float | None
    temp_forecast: dict[date, float] = field(default_factory=dict)
    last_temp_eff: float | None = None
    burned_today_kg: float = 0.0
    season_end: date | None = None
    horizon_days: int = 1095
    paths: int = 400
    seed: int | None = None
    curve_days: int = 30


@dataclass(slots=True)
class ForecastResult:
    runout: dict[str, date | None]  # p10 / p50 / p90; None = not within horizon
    days_left: dict[str, float | None]
    silo_empty: dict[str, date | None]
    kg_today: float
    kg_next_7: float
    kg_next_30: float
    season_end: date | None
    need_until_season_end: dict[str, float | None]
    order_kg: float | None
    prob_runout_before_season_end: float | None
    curve: list[dict]  # expected kg/day and temperature, for charts
    paths: int


def default_season_end(today: date, latitude: float | None) -> date:
    """End of the heating season: 1 June north of the equator, 1 December south of it."""
    month = 12 if (latitude or 0) < 0 else 6
    end = date(today.year, month, 1)
    return end if end > today else date(today.year + 1, month, 1)


def _quantile(sorted_vals: list[float], q: float) -> float:
    if not sorted_vals:
        return math.nan
    pos = q * (len(sorted_vals) - 1)
    lo = math.floor(pos)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = pos - lo
    return sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac


def run(inp: ForecastInput, model: ConsumptionModel, climate: ClimateModel) -> ForecastResult:
    rng = random.Random(inp.seed)
    horizon = max(31, inp.horizon_days)
    days = [inp.today + timedelta(days=i) for i in range(horizon)]
    clim_mean = [climate.mean(d) for d in days]
    clim_sd = [climate.sd(d) for d in days]
    fc = [inp.temp_forecast.get(d) for d in days]
    last_fc = max((i for i, t in enumerate(fc) if t is not None), default=-1)
    phi = climate.phi
    shock = math.sqrt(max(0.0, 1.0 - phi * phi))
    fshock = math.sqrt(1.0 - FORECAST_ERROR_PHI**2)
    season_idx = (inp.season_end - inp.today).days if inp.season_end and inp.season_end > inp.today else None
    curve_n = min(inp.curve_days, horizon)

    stock = max(0.0, inp.stock_kg)
    silo = inp.silo_kg
    frac_today = min(1.0, max(0.0, inp.day_fraction_left))

    runouts: list[float] = []
    silo_outs: list[float] = []
    needs: list[float] = []
    curve_kg = [0.0] * curve_n
    curve_t = [0.0] * curve_n
    sum_today = sum_7 = sum_30 = 0.0
    n = max(20, inp.paths)

    for _ in range(n):
        limit, base, k, sigma = model.sample(rng)
        ref = max(1e-6, base + k * max(1.0, limit - 2.0))  # a typical winter day
        teff = inp.last_temp_eff
        err = rng.gauss(0.0, 1.0)
        anom = 0.0
        cum = 0.0
        run_t = math.inf
        silo_t = math.inf if silo is not None else math.nan
        need: float | None = None
        stop_at = max(curve_n, 30, (season_idx or 0) + 1)
        for i in range(horizon):
            if i == season_idx:
                need = cum
            m, sd = clim_mean[i], clim_sd[i]
            if fc[i] is not None:
                err = FORECAST_ERROR_PHI * err + fshock * rng.gauss(0.0, 1.0)
                spread = min(sd, FORECAST_ERROR_BASE + FORECAST_ERROR_GROWTH * i)
                temp = fc[i] + spread * err  # type: ignore[operator]
                anom = temp - m
            else:
                if i == last_fc + 1 and last_fc < 0:
                    anom = rng.gauss(0.0, sd)  # no forecast at all: today's weather is a climate draw
                anom = phi * anom + shock * sd * rng.gauss(0.0, 1.0)
                temp = m + anom
            teff = temp if teff is None else SMOOTHING * temp + (1 - SMOOTHING) * teff
            mean = base + k * max(0.0, limit - teff)
            # σ is fitted mostly on heating days. Let the noise shrink like √mean on mild days
            # (burn is a count of auger runs), otherwise clipping at 0 would inflate summer burn.
            kg = max(0.0, mean + sigma * rng.gauss(0.0, 1.0) * min(1.0, math.sqrt(mean / ref)))
            if i == 0:
                kg *= frac_today
                sum_today += kg
            if i < 7:
                sum_7 += kg
            if i < 30:
                sum_30 += kg
            if i < curve_n:
                curve_kg[i] += kg
                curve_t[i] += temp
            prev = cum
            cum += kg
            if silo is not None and silo_t == math.inf and cum >= silo:
                silo_t = i + ((silo - prev) / kg if kg > 0 else 0.0)
            if run_t == math.inf and cum >= stock:
                run_t = i + ((stock - prev) / kg if kg > 0 else 0.0)
            if run_t != math.inf and (silo is None or silo_t != math.inf) and i + 1 >= stop_at:
                break
        runouts.append(run_t)
        if silo is not None:
            silo_outs.append(silo_t)
        if need is not None:
            needs.append(need)

    runouts.sort()
    silo_outs.sort()
    needs.sort()

    def as_date(x: float) -> date | None:
        return None if not math.isfinite(x) else inp.today + timedelta(days=math.floor(x))

    qs = {"p10": 0.1, "p50": 0.5, "p90": 0.9}
    runout = {k: as_date(_quantile(runouts, q)) for k, q in qs.items()}
    days_left = {k: (None if not math.isfinite(v := _quantile(runouts, q)) else round(v, 1)) for k, q in qs.items()}
    silo_empty = {k: (as_date(_quantile(silo_outs, q)) if silo_outs else None) for k, q in qs.items()}

    need = {k: (round(_quantile(needs, q), 1) if needs else None) for k, q in qs.items()}
    order = None if need["p90"] is None else max(0.0, need["p90"] - stock)
    prob = None
    if season_idx is not None:
        prob = sum(1 for r in runouts if r < season_idx) / len(runouts)

    curve = [
        {"date": days[i].isoformat(), "kg": round(curve_kg[i] / n, 2), "temp": round(curve_t[i] / n, 1)}
        for i in range(curve_n)
    ]
    return ForecastResult(
        runout=runout,
        days_left=days_left,
        silo_empty=silo_empty,
        kg_today=round(inp.burned_today_kg + sum_today / n, 2),
        kg_next_7=round(sum_7 / n, 1),
        kg_next_30=round(sum_30 / n, 1),
        season_end=inp.season_end,
        need_until_season_end=need,
        order_kg=None if order is None else round(order, 1),
        prob_runout_before_season_end=None if prob is None else round(prob, 3),
        curve=curve,
        paths=n,
    )
