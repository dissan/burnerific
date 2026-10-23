"""How many pellets the house burns as a function of the weather.

Model (degree-day regression with wind and sun)::

    kg(d) = base_load
          + k   · HDD(d)                          HDD = max(0, T_limit - T_eff)
          + k_w · HDD(d) · (wind(d) - wind_ref) / 5
          - k_s · sun(d) · min(1, HDD(d) / 3)
          + ε,     ε ~ N(0, σ²)

* ``base_load``: kg/day that burns regardless of weather (hot water, standby losses).
* ``k``: kg per degree-day, i.e. how leaky and big the house is.
* ``k_w``: extra kg per degree-day for every 5 m/s of wind above normal. Wind
  strips heat off walls and pushes cold air through gaps, so a cold windy day
  costs more than a cold calm one.
* ``k_s``: kg saved per MJ/m² of sunshine on days that need heat (solar gain
  through windows). On days that need no heat the sun saves nothing.
* ``T_limit``: the heating limit, the outdoor temperature above which the house
  needs no heat.
* ``T_eff``: outdoor temperature smoothed over a day or so (thermal mass).

Fitting is Bayesian (Normal-Inverse-Gamma conjugate prior on the coefficients
and σ²) so it works from day one with only a guess of the yearly consumption,
and sharpens as data comes in. Older data is down-weighted (power prior with an
exponential forgetting factor) so the model follows changes in the house.

Evidence comes in two shapes:

* days: kg burned on one day and that day's weather.
* periods: kg burned over several days (the controller's monthly totals, or the
  gap between two logged silo fills) and the weather of every day in between.

``T_limit`` is not linear, so the fit runs on a grid of limits and each gets a
posterior probability from its marginal likelihood. Forecasts sample across all
of them (Bayesian model averaging).
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from datetime import date, timedelta

from .climate import ClimateModel, expected_degree_days
from .linalg import cholesky, inverse_spd, logdet_spd, matvec, weighted_normal_equations

SMOOTHING = 0.7  # weight of today's temperature in T_eff
LIMITS = [10.0 + 0.5 * i for i in range(25)]  # 10.0 .. 22.0 °C
LIMIT_PRIOR_MEAN = 16.0
LIMIT_PRIOR_SD = 2.5
PRIOR_SHAPE = 3.0
WIND_STEP = 5.0  # m/s per unit of the wind feature
SUN_RAMP = 3.0  # degree-days over which solar gain fades in
P = 4  # coefficients: base, k, k_wind, k_sun


@dataclass(slots=True, frozen=True)
class Weather:
    """One day's weather. Wind is the daily max at 10 m in m/s, sun the shortwave sum in MJ/m²."""

    temp: float
    wind: float | None = None
    sun: float | None = None


@dataclass(slots=True, frozen=True)
class DayObs:
    day: date
    kg: float
    temp: float | None  # daily mean outdoor °C
    wind: float | None = None
    sun: float | None = None


@dataclass(slots=True, frozen=True)
class PeriodObs:
    """kg burned from ``start`` for ``days`` days (both a month total and a fill interval fit this)."""

    start: date
    days: int
    kg: float
    weather: tuple[Weather, ...] | None = None  # one per day when known
    source: str = "controller"

    @property
    def end(self) -> date:
        return self.start + timedelta(days=self.days)

    def dates(self) -> list[date]:
        return [self.start + timedelta(days=i) for i in range(self.days)]


def MonthObs(year: int, month: int, kg: float, days: int, temps: tuple[float, ...] | None = None) -> PeriodObs:  # noqa: N802
    """Backwards compatible helper: a calendar month as a period."""
    weather = tuple(Weather(t) for t in temps) if temps is not None else None
    return PeriodObs(date(year, month, 1), days, kg, weather, "controller")


@dataclass(slots=True)
class Candidate:
    limit: float
    mean: list[float]
    cov: list[list[float]]  # Vn: posterior covariance of the coefficients is σ² · Vn
    shape: float  # aₙ
    scale: float  # bₙ
    log_evidence: float
    weight: float = 0.0
    _chol: list[list[float]] | None = field(default=None, repr=False)

    @property
    def sigma(self) -> float:
        """Posterior mean of σ (approx.)."""
        return math.sqrt(self.scale / max(self.shape - 1.0, 0.5))

    def chol(self) -> list[list[float]]:
        if self._chol is None:
            self._chol = cholesky(self.cov)
        return self._chol


def effective_temps(days: list[tuple[date, float]], alpha: float = SMOOTHING) -> dict[date, float]:
    """Exponentially smoothed temperatures. The smoothing restarts after gaps."""
    out: dict[date, float] = {}
    prev_day: date | None = None
    prev: float | None = None
    for d, t in sorted(days):
        if prev is not None and prev_day is not None and (d - prev_day).days == 1:
            prev = alpha * t + (1 - alpha) * prev
        else:
            prev = t
        out[d] = prev
        prev_day = d
    return out


def features(limit: float, teff: float, wind: float | None, sun: float | None, wind_ref: float) -> list[float]:
    hdd = max(0.0, limit - teff)
    w = 0.0 if wind is None else hdd * (wind - wind_ref) / WIND_STEP
    s = 0.0 if sun is None else -sun * min(1.0, hdd / SUN_RAMP)
    return [1.0, hdd, w, s]


@dataclass(slots=True)
class Sample:
    limit: float
    beta: list[float]
    sigma: float

    def kg(self, teff: float, wind: float | None, sun: float | None, wind_ref: float) -> float:
        x = features(self.limit, teff, wind, sun, wind_ref)
        return max(0.0, sum(b * v for b, v in zip(self.beta, x, strict=True)))


@dataclass(slots=True)
class ConsumptionModel:
    candidates: list[Candidate]
    n_days: int
    n_months: int
    prior_annual_kg: float
    wind_ref: float = 6.0
    n_periods: int = 0
    r2: float | None = None

    @property
    def best(self) -> Candidate:
        return max(self.candidates, key=lambda c: c.weight)

    def _avg(self, i: int) -> float:
        return sum(c.weight * c.mean[i] for c in self.candidates)

    @property
    def heating_limit(self) -> float:
        return sum(c.weight * c.limit for c in self.candidates)

    @property
    def base_load(self) -> float:
        return self._avg(0)

    @property
    def kg_per_degree_day(self) -> float:
        return self._avg(1)

    @property
    def wind_effect(self) -> float:
        """Extra burn per 5 m/s of extra wind, as a fraction of the degree-day burn."""
        k = self.kg_per_degree_day
        return self._avg(2) / k if k > 1e-6 else 0.0

    @property
    def sun_kg_per_mj(self) -> float:
        return self._avg(3)

    @property
    def sigma(self) -> float:
        return sum(c.weight * c.sigma for c in self.candidates)

    def expected(self, temp_eff: float, wind: float | None = None, sun: float | None = None) -> float:
        total = 0.0
        for c in self.candidates:
            x = features(c.limit, temp_eff, wind, sun, self.wind_ref)
            total += c.weight * max(0.0, sum(b * v for b, v in zip(c.mean, x, strict=True)))
        return total

    def sample(self, rng: random.Random) -> Sample:
        """Draw a whole model from the posterior, heating limit uncertainty included."""
        r = rng.random()
        acc = 0.0
        cand = self.candidates[-1]
        for c in self.candidates:
            acc += c.weight
            if r <= acc:
                cand = c
                break
        # σ² ~ InvGamma(a, b); β | σ² ~ N(m, σ² V)
        sigma = math.sqrt(cand.scale / rng.gammavariate(cand.shape, 1.0))
        lo = cand.chol()
        z = [rng.gauss(0, 1) for _ in range(P)]
        beta = [cand.mean[i] + sigma * sum(lo[i][j] * z[j] for j in range(i + 1)) for i in range(P)]
        beta[0] = max(0.0, beta[0])
        beta[1] = max(0.0, beta[1])
        return Sample(cand.limit, beta, sigma)

    def describe(self) -> dict[str, float | int | None]:
        return {
            "heating_limit_c": round(self.heating_limit, 1),
            "base_load_kg_per_day": round(self.base_load, 2),
            "kg_per_degree_day": round(self.kg_per_degree_day, 3),
            "wind_effect_pct_per_5ms": round(100 * self.wind_effect, 1),
            "sun_kg_per_mj": round(self.sun_kg_per_mj, 3),
            "daily_sigma_kg": round(self.sigma, 2),
            "days_observed": self.n_days,
            "months_observed": self.n_months,
            "periods_observed": self.n_periods,
            "r2": None if self.r2 is None else round(self.r2, 3),
        }


def fit(
    days: list[DayObs],
    climate: ClimateModel,
    *,
    today: date,
    prior_annual_kg: float = 4000.0,
    months: list[PeriodObs] | None = None,
    periods: list[PeriodObs] | None = None,
    half_life_days: float = 120.0,
    wind_ref: float | None = None,
) -> ConsumptionModel:
    """Fit the model from days and periods (``months`` and ``periods`` are treated the same)."""
    all_periods = list(months or []) + list(periods or [])
    decay = 0.5 ** (1.0 / half_life_days)
    with_temp = [d for d in days if d.temp is not None and d.kg >= 0]
    teff = effective_temps([(d.day, d.temp) for d in with_temp])  # type: ignore[misc]
    obs = [(d, teff[d.day]) for d in with_temp]

    winds = [d.wind for d in with_temp if d.wind is not None]
    winds += [w.wind for p in all_periods for w in (p.weather or ()) if w.wind is not None]
    if wind_ref is None:
        wind_ref = sum(winds) / len(winds) if winds else 6.0

    year = [today + timedelta(days=i - 182) for i in range(365)]
    clim_cache = [(climate.mean(d), climate.sd(d)) for d in year]
    sigma_prior = max(0.8, 0.25 * prior_annual_kg / 365.0)

    ys: list[float] = []
    ws: list[float] = []
    noise: list[float] = []  # residual scale of each row relative to one day
    for d, _ in obs:
        ys.append(d.kg)
        ws.append(decay ** max(0, (today - d.day).days))
        noise.append(1.0)
    period_teff: list[list[float] | None] = []
    for p in all_periods:
        measured = p.weather is not None and len(p.weather) == p.days
        period_teff.append(
            list(effective_temps([(d, w.temp) for d, w in zip(p.dates(), p.weather, strict=True)]).values())  # type: ignore[arg-type]
            if measured
            else None
        )
        ys.append(p.kg)
        # A period sum has ~days x the variance of a day. Without measured weather the
        # degree-days are a climate guess too, so it counts as half as informative.
        spread = 1.0 if measured else 2.0
        mid = p.start + timedelta(days=p.days // 2)
        ws.append(decay ** max(0, (today - mid).days) / (spread * p.days))
        noise.append(math.sqrt(spread * p.days))

    def design(limit: float) -> list[list[float]]:
        rows = [features(limit, t, d.wind, d.sun, wind_ref) for d, t in obs]
        for p, te in zip(all_periods, period_teff, strict=True):
            row = [0.0] * P
            if te is not None and p.weather is not None:
                for t, w in zip(te, p.weather, strict=True):
                    for i, v in enumerate(features(limit, t, w.wind, w.sun, wind_ref)):
                        row[i] += v
            else:
                for d in p.dates():
                    row[0] += 1.0
                    row[1] += climate.degree_days(d, limit)
            rows.append(row)
        return rows

    def prior(limit: float) -> tuple[list[float], list[list[float]], float, float]:
        hdd_year = sum(expected_degree_days(limit, m, s) for m, s in clim_cache) or 1.0
        b0 = 0.10 * prior_annual_kg / 365.0
        k0 = 0.90 * prior_annual_kg / hdd_year
        sds = [max(1.0, b0), max(0.15, 0.6 * k0), max(0.03, 0.15 * k0), 0.15]
        v0 = [[(sds[i] / sigma_prior) ** 2 if i == j else 0.0 for j in range(P)] for i in range(P)]
        return [b0, k0, 0.0, 0.05], v0, PRIOR_SHAPE, (PRIOR_SHAPE - 1.0) * sigma_prior**2

    # One robust pass at the prior heating limit, shared by all candidates so their
    # evidences stay comparable: down-weight rows the model calls absurd (boiler off, cleaning, gaps).
    if len(ys) >= 10:
        ref = _posterior(LIMIT_PRIOR_MEAN, design(LIMIT_PRIOR_MEAN), ys, ws, *prior(LIMIT_PRIOR_MEAN))
        sig = ref.sigma
        for i, (x, y, n) in enumerate(zip(design(LIMIT_PRIOR_MEAN), ys, noise, strict=True)):
            r = abs(y - sum(b * v for b, v in zip(ref.mean, x, strict=True))) / (sig * n)
            if r > 3.0:
                ws[i] *= 3.0 / r

    candidates = [_posterior(limit, design(limit), ys, ws, *prior(limit)) for limit in LIMITS]

    logs = [c.log_evidence - 0.5 * ((c.limit - LIMIT_PRIOR_MEAN) / LIMIT_PRIOR_SD) ** 2 for c in candidates]
    top = max(logs)
    weights = [math.exp(v - top) for v in logs]
    total = sum(weights)
    for c, w in zip(candidates, weights, strict=True):
        c.weight = w / total

    n_months = sum(1 for p in all_periods if p.source == "controller")
    model = ConsumptionModel(
        candidates, len(obs), n_months, prior_annual_kg, wind_ref, len(all_periods) - n_months
    )
    if len(obs) >= 5:
        ys_day = [d.kg for d, _ in obs]
        mean_y = sum(ys_day) / len(ys_day)
        ss_tot = sum((y - mean_y) ** 2 for y in ys_day)
        ss_res = sum((d.kg - model.expected(t, d.wind, d.sun)) ** 2 for d, t in obs)
        model.r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else None
    return model


def _posterior(
    limit: float,
    rows: list[list[float]],
    ys: list[float],
    ws: list[float],
    m0: list[float],
    v0: list[list[float]],
    a0: float,
    s0: float,
) -> Candidate:
    p = len(m0)
    v0_inv = inverse_spd(v0)
    if rows:
        xtx, xty, yty = weighted_normal_equations(rows, ys, ws)
    else:
        xtx, xty, yty = [[0.0] * p for _ in range(p)], [0.0] * p, 0.0
    prec = [[v0_inv[i][j] + xtx[i][j] for j in range(p)] for i in range(p)]
    vn = inverse_spd(prec)
    rhs = [a + b for a, b in zip(matvec(v0_inv, m0), xty, strict=True)]
    mn = matvec(vn, rhs)
    n_eff = sum(ws)
    an = a0 + 0.5 * n_eff
    quad0 = sum(m0[i] * sum(v0_inv[i][j] * m0[j] for j in range(p)) for i in range(p))
    quadn = sum(mn[i] * sum(prec[i][j] * mn[j] for j in range(p)) for i in range(p))
    bn = max(1e-6, s0 + 0.5 * (yty + quad0 - quadn))
    log_ev = (
        -0.5 * n_eff * math.log(2.0 * math.pi)
        + 0.5 * (logdet_spd(vn) - logdet_spd(v0))
        + a0 * math.log(s0)
        - an * math.log(bn)
        + math.lgamma(an)
        - math.lgamma(a0)
    )
    return Candidate(limit, mn, vn, an, bn, log_ev)
