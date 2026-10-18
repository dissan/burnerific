"""How many pellets the house burns as a function of outdoor temperature.

Model (degree-day regression)::

    kg(d) = base_load + k · max(0, T_limit - T_eff(d)) + ε,     ε ~ N(0, σ²)

* ``base_load``: kg/day that burns regardless of weather (hot water, standby losses).
* ``k``: kg per degree-day, i.e. how leaky and big the house is.
* ``T_limit``: the heating limit, the outdoor temperature above which the house
  needs no heat. Usually 14-18 °C; better insulated houses have lower limits.
* ``T_eff``: outdoor temperature smoothed over a day or so, because the house
  has thermal mass and does not react to the weather instantly.

Fitting is Bayesian (Normal-Inverse-Gamma conjugate prior on base_load, k and
σ²) so it works from day one with only a guess of the yearly consumption, and
sharpens as days are observed. Older days are down-weighted (power prior with
an exponential forgetting factor) so the model follows changes in how the
house is used.

``T_limit`` is not linear in the model, so the fit is done on a grid of limits
and each candidate gets a posterior probability from its marginal likelihood
(evidence). Forecasts sample over all candidates: Bayesian model averaging,
so the uncertainty about the heating limit carries through to the run-out date.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
import math
import random

from .climate import ClimateModel, expected_degree_days
from .linalg import cholesky, inverse_spd, logdet_spd, matvec, weighted_normal_equations

SMOOTHING = 0.7  # weight of today's temperature in T_eff
LIMITS = [10.0 + 0.5 * i for i in range(25)]  # 10.0 .. 22.0 °C
LIMIT_PRIOR_MEAN = 16.0
LIMIT_PRIOR_SD = 2.5
PRIOR_SHAPE = 3.0


@dataclass(slots=True, frozen=True)
class DayObs:
    day: date
    kg: float
    temp: float | None  # daily mean outdoor °C


@dataclass(slots=True, frozen=True)
class MonthObs:
    year: int
    month: int
    kg: float
    days: int
    temps: tuple[float, ...] | None = None  # measured daily means for the whole month, if known


@dataclass(slots=True)
class Candidate:
    limit: float
    mean: list[float]  # base_load, k
    cov: list[list[float]]  # Vn: posterior covariance of (base_load, k) is σ² · Vn
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


def _lgamma(x: float) -> float:
    return math.lgamma(x)


@dataclass(slots=True)
class ConsumptionModel:
    candidates: list[Candidate]
    n_days: int
    n_months: int
    prior_annual_kg: float
    r2: float | None = None

    @property
    def best(self) -> Candidate:
        return max(self.candidates, key=lambda c: c.weight)

    @property
    def heating_limit(self) -> float:
        """Posterior mean heating limit."""
        return sum(c.weight * c.limit for c in self.candidates)

    @property
    def base_load(self) -> float:
        return sum(c.weight * c.mean[0] for c in self.candidates)

    @property
    def kg_per_degree_day(self) -> float:
        return sum(c.weight * c.mean[1] for c in self.candidates)

    @property
    def sigma(self) -> float:
        return sum(c.weight * c.sigma for c in self.candidates)

    def expected(self, temp_eff: float) -> float:
        return sum(c.weight * max(0.0, c.mean[0] + c.mean[1] * max(0.0, c.limit - temp_eff)) for c in self.candidates)

    def sample(self, rng: random.Random) -> tuple[float, float, float, float]:
        """Draw (limit, base_load, k, σ) from the posterior, model uncertainty included."""
        r = rng.random()
        acc = 0.0
        cand = self.candidates[-1]
        for c in self.candidates:
            acc += c.weight
            if r <= acc:
                cand = c
                break
        # σ² ~ InvGamma(a, b); (base, k) | σ² ~ N(m, σ² V)
        sigma2 = cand.scale / rng.gammavariate(cand.shape, 1.0)
        sigma = math.sqrt(sigma2)
        lo = cand.chol()
        z0, z1 = rng.gauss(0, 1), rng.gauss(0, 1)
        base = cand.mean[0] + sigma * (lo[0][0] * z0)
        k = cand.mean[1] + sigma * (lo[1][0] * z0 + lo[1][1] * z1)
        return cand.limit, max(0.0, base), max(0.0, k), sigma

    def describe(self) -> dict[str, float | int | None]:
        return {
            "heating_limit_c": round(self.heating_limit, 1),
            "base_load_kg_per_day": round(self.base_load, 2),
            "kg_per_degree_day": round(self.kg_per_degree_day, 3),
            "daily_sigma_kg": round(self.sigma, 2),
            "days_observed": self.n_days,
            "months_observed": self.n_months,
            "r2": None if self.r2 is None else round(self.r2, 3),
        }


def fit(
    days: list[DayObs],
    climate: ClimateModel,
    *,
    today: date,
    prior_annual_kg: float = 4000.0,
    months: list[MonthObs] | None = None,
    half_life_days: float = 120.0,
) -> ConsumptionModel:
    """Fit the degree-day model.

    ``days`` with a temperature are the main evidence. ``months`` (the
    controller's own monthly totals) are weaker evidence: their degree-days
    come from the climate model, not measurements, so they are down-weighted.
    """
    months = months or []
    decay = 0.5 ** (1.0 / half_life_days)
    with_temp = [d for d in days if d.temp is not None and d.kg >= 0]
    teff = effective_temps([(d.day, d.temp) for d in with_temp])  # type: ignore[misc]
    obs = [(d, teff[d.day]) for d in with_temp]

    # Rough annual degree-days from the climate, for turning the yearly guess into a prior.
    year = [date.fromordinal(today.toordinal() - 182 + i) for i in range(365)]
    clim_cache = [(climate.mean(d), climate.sd(d)) for d in year]

    sigma_prior = max(0.8, 0.25 * prior_annual_kg / 365.0)

    # Same observations and weights for every heating limit, only the degree-day column changes.
    ys: list[float] = []
    ws: list[float] = []
    noise: list[float] = []  # residual scale of each row relative to one day
    for d, _ in obs:
        ys.append(d.kg)
        ws.append(decay ** max(0, (today - d.day).days))
        noise.append(1.0)
    month_days: list[list[date]] = []
    month_teff: list[list[float] | None] = []
    for m in months:
        first = date(m.year, m.month, 1).toordinal()
        md = [date.fromordinal(first + i) for i in range(m.days)]
        month_days.append(md)
        measured = m.temps is not None and len(m.temps) == m.days
        month_teff.append(list(effective_temps(list(zip(md, m.temps, strict=True))).values()) if measured else None)  # type: ignore[arg-type]
        ys.append(m.kg)
        # A month sum has ~days x the variance of a day. Without measured temperatures the
        # weather is a climate guess too, so count it as half as informative.
        spread = 1.0 if measured else 2.0
        ws.append(decay ** max(0, (today - date(m.year, m.month, 15)).days) / (spread * m.days))
        noise.append(math.sqrt(spread * m.days))

    def design(limit: float) -> list[list[float]]:
        rows = [[1.0, max(0.0, limit - t)] for _, t in obs]
        for md, te in zip(month_days, month_teff, strict=True):
            if te is not None:
                hdd = sum(max(0.0, limit - t) for t in te)
            else:
                hdd = sum(climate.degree_days(d, limit) for d in md)
            rows.append([float(len(md)), hdd])
        return rows

    def prior(limit: float) -> tuple[list[float], list[list[float]], float, float]:
        hdd_year = sum(expected_degree_days(limit, m, s) for m, s in clim_cache) or 1.0
        b0 = 0.10 * prior_annual_kg / 365.0
        k0 = 0.90 * prior_annual_kg / hdd_year
        sd_b = max(1.0, b0)
        sd_k = max(0.15, 0.6 * k0)
        v0 = [[(sd_b / sigma_prior) ** 2, 0.0], [0.0, (sd_k / sigma_prior) ** 2]]
        return [b0, k0], v0, PRIOR_SHAPE, (PRIOR_SHAPE - 1.0) * sigma_prior**2

    # One robust pass, judged at the prior heating limit and shared by all candidates so their
    # evidences stay comparable: down-weight rows the model calls absurd (boiler off, cleaning, gaps).
    if len(ys) >= 10:
        ref = _posterior(LIMIT_PRIOR_MEAN, design(LIMIT_PRIOR_MEAN), ys, ws, *prior(LIMIT_PRIOR_MEAN))
        sig = ref.sigma
        for i, (x, y, n) in enumerate(zip(design(LIMIT_PRIOR_MEAN), ys, noise, strict=True)):
            r = abs(y - (ref.mean[0] * x[0] + ref.mean[1] * x[1])) / (sig * n)
            if r > 3.0:
                ws[i] *= 3.0 / r

    candidates = [_posterior(limit, design(limit), ys, ws, *prior(limit)) for limit in LIMITS]

    # Posterior over the heating limit: evidence × prior.
    logs = [c.log_evidence - 0.5 * ((c.limit - LIMIT_PRIOR_MEAN) / LIMIT_PRIOR_SD) ** 2 for c in candidates]
    top = max(logs)
    weights = [math.exp(v - top) for v in logs]
    total = sum(weights)
    for c, w in zip(candidates, weights, strict=True):
        c.weight = w / total

    model = ConsumptionModel(candidates, len(obs), len(months), prior_annual_kg)
    if len(obs) >= 5:
        ys_day = [d.kg for d, _ in obs]
        mean_y = sum(ys_day) / len(ys_day)
        ss_tot = sum((y - mean_y) ** 2 for y in ys_day)
        ss_res = sum((d.kg - model.expected(t)) ** 2 for d, t in obs)
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
    v0_inv = inverse_spd(v0)
    if rows:
        xtx, xty, yty = weighted_normal_equations(rows, ys, ws)
    else:
        xtx, xty, yty = [[0.0, 0.0], [0.0, 0.0]], [0.0, 0.0], 0.0
    prec = [[v0_inv[i][j] + xtx[i][j] for j in range(2)] for i in range(2)]
    vn = inverse_spd(prec)
    rhs = [a + b for a, b in zip(matvec(v0_inv, m0), xty, strict=True)]
    mn = matvec(vn, rhs)
    n_eff = sum(ws)
    an = a0 + 0.5 * n_eff
    quad0 = sum(m0[i] * sum(v0_inv[i][j] * m0[j] for j in range(2)) for i in range(2))
    quadn = sum(mn[i] * sum(prec[i][j] * mn[j] for j in range(2)) for i in range(2))
    bn = max(1e-6, s0 + 0.5 * (yty + quad0 - quadn))
    # Marginal likelihood of the (power-weighted) data under this heating limit.
    log_ev = (
        -0.5 * n_eff * math.log(2.0 * math.pi)
        + 0.5 * (logdet_spd(vn) - logdet_spd(v0))
        + a0 * math.log(s0)
        - an * math.log(bn)
        + _lgamma(an)
        - _lgamma(a0)
    )
    return Candidate(limit, mn, vn, an, bn, log_ev)
