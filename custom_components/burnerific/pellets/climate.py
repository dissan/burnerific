"""Seasonal temperature model for the house's location.

Daily mean temperature is modelled as a climatological curve plus a weather
anomaly that persists for a few days:

    T(d) = clim(d) + a(d)
    clim(d) = c0 + c1 cos(wd) + s1 sin(wd) + c2 cos(2wd) + s2 sin(2wd),   w = 2π / 365.2425
    a(d) = φ a(d-1) + sqrt(1 - φ²) σ(d) ε,   ε ~ N(0, 1)

σ(d) is itself seasonal (winters are more variable than summers in most
places). Two harmonics capture the asymmetric seasons (fast spring, slow
autumn) seen in maritime climates.

The model is fitted from years of daily history for the location (Open-Meteo
archive, fetched by the integration) and falls back to a sensible default for
the hemisphere when that is not available.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
import math

from .linalg import cholesky, solve_cholesky, weighted_normal_equations

OMEGA = 2.0 * math.pi / 365.2425
SQRT2 = math.sqrt(2.0)


def _features(d: date) -> list[float]:
    t = d.timetuple().tm_yday - 1 + 0.5
    return [1.0, math.cos(OMEGA * t), math.sin(OMEGA * t), math.cos(2 * OMEGA * t), math.sin(2 * OMEGA * t)]


def _phi(z: float) -> float:
    return math.exp(-0.5 * z * z) / math.sqrt(2.0 * math.pi)


def _Phi(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / SQRT2))


def expected_degree_days(base: float, mean: float, sd: float) -> float:
    """E[max(0, base - T)] for T ~ N(mean, sd²). Exact, so cold tails count even on mild days."""
    if sd <= 1e-9:
        return max(0.0, base - mean)
    z = (base - mean) / sd
    return (base - mean) * _Phi(z) + sd * _phi(z)


@dataclass(slots=True)
class ClimateModel:
    mean_coef: list[float]  # c0, c1, s1, c2, s2
    var_coef: list[float]  # log σ² ≈ v0 + v1 cos + v2 sin
    phi: float  # day to day persistence of anomalies
    source: str = "default"
    years: float = 0.0

    def mean(self, d: date) -> float:
        return sum(c * f for c, f in zip(self.mean_coef, _features(d), strict=True))

    def sd(self, d: date) -> float:
        f = _features(d)
        v = self.var_coef
        return math.sqrt(math.exp(v[0] + v[1] * f[1] + v[2] * f[2]))

    def degree_days(self, d: date, base: float) -> float:
        return expected_degree_days(base, self.mean(d), self.sd(d))

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict | None) -> ClimateModel | None:
        if not data:
            return None
        try:
            return cls(**data)
        except TypeError:
            return None

    @classmethod
    def default(cls, latitude: float | None = None) -> ClimateModel:
        """A temperate maritime climate (Denmark-ish), mirrored for the southern hemisphere.

        Mean 8.5 °C, ±8.3 °C seasonal swing, coldest late January, σ 3.4 °C in
        winter and 2.4 °C in summer, anomalies with ~4 day memory.
        """
        lat = 55.0 if latitude is None else latitude
        south = lat < 0
        # Peak warmth around 21 July (northern) / 21 January (southern).
        peak = 202 if not south else 21
        amp = 8.3
        c1 = amp * math.cos(OMEGA * peak)
        s1 = amp * math.sin(OMEGA * peak)
        # Variance: log σ² swings between log(3.4²) in winter and log(2.4²) in summer.
        mid = 0.5 * (math.log(3.4**2) + math.log(2.4**2))
        half = 0.5 * (math.log(3.4**2) - math.log(2.4**2))
        v1 = -half * math.cos(OMEGA * peak)
        v2 = -half * math.sin(OMEGA * peak)
        return cls([8.5, c1, s1, 0.0, 0.0], [mid, v1, v2], 0.78, "default", 0.0)

    @classmethod
    def fit(cls, days: list[tuple[date, float]]) -> ClimateModel:
        """Fit from daily mean temperatures. Needs at least a year to mean much."""
        if len(days) < 120:
            raise ValueError("need at least 120 days of temperatures")
        days = sorted(days)
        rows = [_features(d) for d, _ in days]
        ys = [t for _, t in days]
        xtx, xty, _ = weighted_normal_equations(rows, ys, [1.0] * len(ys))
        for i in range(len(xtx)):
            xtx[i][i] += 1e-6
        coef = solve_cholesky(cholesky(xtx), xty)

        resid = [y - sum(c * f for c, f in zip(coef, r, strict=True)) for r, y in zip(rows, ys, strict=True)]

        # Persistence from consecutive days only.
        num = den = 0.0
        for i in range(1, len(days)):
            if (days[i][0] - days[i - 1][0]).days == 1:
                num += resid[i] * resid[i - 1]
                den += resid[i - 1] ** 2
        phi = min(0.95, max(0.3, num / den)) if den > 0 else 0.78

        # Seasonal variance: regress log of 30-day-ish smoothed squared residuals on one harmonic.
        # Using log(r² + small) directly is biased by E[log χ²₁] = -1.27; correct for it.
        vrows = [r[:3] for r in rows]
        vys = [math.log(e * e + 1e-3) + 1.2704 for e in resid]
        vxtx, vxty, _ = weighted_normal_equations(vrows, vys, [1.0] * len(vys))
        for i in range(3):
            vxtx[i][i] += 1e-6
        vcoef = solve_cholesky(cholesky(vxtx), vxty)

        span = (days[-1][0] - days[0][0]).days / 365.2425
        return cls(coef, vcoef, phi, "history", round(span, 2))
