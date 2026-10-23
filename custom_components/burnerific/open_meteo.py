"""Open-Meteo: free, keyless weather data for the home location.

* Archive (ERA5 reanalysis): years of daily mean temperatures, to fit the
  seasonal climate model for this exact place.
* Forecast API with ``past_days``: the last two months of daily means (to
  backfill days the controller has consumption for but we had no
  temperature) and a 16-day forecast beyond what the weather entity offers.

Only rounded coordinates are sent.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

import aiohttp

_LOGGER = logging.getLogger(__name__)

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
TIMEOUT = aiohttp.ClientTimeout(total=30)


class OpenMeteoError(Exception):
    pass


DAILY = "temperature_2m_mean,wind_speed_10m_max,shortwave_radiation_sum"

# (day, mean °C, max wind m/s or None, sunshine MJ/m² or None)
Row = tuple[date, float, float | None, float | None]


def _daily(payload: dict) -> list[Row]:
    daily = payload.get("daily") or {}
    days = daily.get("time") or []
    temps = daily.get("temperature_2m_mean") or []
    winds = daily.get("wind_speed_10m_max") or [None] * len(days)
    suns = daily.get("shortwave_radiation_sum") or [None] * len(days)
    out: list[Row] = []
    for d, t, w, s in zip(days, temps, winds, suns, strict=False):
        if t is None:
            continue
        out.append((date.fromisoformat(d), float(t), None if w is None else float(w), None if s is None else float(s)))
    return out


async def _get(session: aiohttp.ClientSession, url: str, params: dict) -> dict:
    try:
        async with session.get(url, params=params, timeout=TIMEOUT) as resp:
            if resp.status != 200:
                raise OpenMeteoError(f"{url} answered {resp.status}")
            return await resp.json()
    except (aiohttp.ClientError, TimeoutError) as err:
        raise OpenMeteoError(str(err)) from err


def _where(lat: float, lon: float) -> dict:
    return {"latitude": f"{lat:.2f}", "longitude": f"{lon:.2f}", "timezone": "auto", "wind_speed_unit": "ms"}


async def fetch_history(
    session: aiohttp.ClientSession, lat: float, lon: float, today: date, years: int = 6, start: date | None = None
) -> list[Row]:
    """Daily weather for the last ``years`` years (archive lags ~5 days)."""
    end = today - timedelta(days=6)
    start = start or date(end.year - years, end.month, 1)
    params = _where(lat, lon) | {
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "daily": DAILY,
    }
    return _daily(await _get(session, ARCHIVE_URL, params))


async def fetch_recent_and_forecast(
    session: aiohttp.ClientSession, lat: float, lon: float, past_days: int = 62
) -> list[Row]:
    """Daily weather from ``past_days`` ago through 16 days ahead."""
    params = _where(lat, lon) | {
        "daily": DAILY,
        "past_days": str(past_days),
        "forecast_days": "16",
    }
    return _daily(await _get(session, FORECAST_URL, params))
