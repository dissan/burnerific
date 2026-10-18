"""Open-Meteo: free, keyless weather data for the home location.

* Archive (ERA5 reanalysis): years of daily mean temperatures, to fit the
  seasonal climate model for this exact place.
* Forecast API with ``past_days``: the last two months of daily means (to
  backfill days the controller has consumption for but we had no
  temperature) and a 16-day forecast beyond what the weather entity offers.

Only rounded coordinates are sent.
"""

from __future__ import annotations

from datetime import date, timedelta
import logging

import aiohttp

_LOGGER = logging.getLogger(__name__)

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
TIMEOUT = aiohttp.ClientTimeout(total=30)


class OpenMeteoError(Exception):
    pass


def _daily(payload: dict) -> list[tuple[date, float]]:
    daily = payload.get("daily") or {}
    days = daily.get("time") or []
    temps = daily.get("temperature_2m_mean") or []
    out = []
    for d, t in zip(days, temps, strict=False):
        if t is None:
            continue
        out.append((date.fromisoformat(d), float(t)))
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
    return {"latitude": f"{lat:.2f}", "longitude": f"{lon:.2f}", "timezone": "auto"}


async def fetch_history(
    session: aiohttp.ClientSession, lat: float, lon: float, today: date, years: int = 6
) -> list[tuple[date, float]]:
    """Daily mean temperatures for the last ``years`` years (archive lags ~5 days)."""
    end = today - timedelta(days=6)
    start = date(end.year - years, end.month, 1)
    params = _where(lat, lon) | {
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "daily": "temperature_2m_mean",
    }
    return _daily(await _get(session, ARCHIVE_URL, params))


async def fetch_recent_and_forecast(
    session: aiohttp.ClientSession, lat: float, lon: float, past_days: int = 62
) -> list[tuple[date, float]]:
    """Daily means from ``past_days`` ago through 16 days ahead."""
    params = _where(lat, lon) | {
        "daily": "temperature_2m_mean",
        "past_days": str(past_days),
        "forecast_days": "16",
    }
    return _daily(await _get(session, FORECAST_URL, params))
