# api/weather_api.py
# Open-Meteo weather — free, no key required.

import logging
from datetime import datetime

import requests

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import config
from models.weather import Weather

logger = logging.getLogger(__name__)

_cache = None

_DEFAULT_TEMP_F    = 72.0
_DEFAULT_WIND_MPH  = 5.0
_DEFAULT_WIND_DEG  = 180.0
_DEFAULT_CONDITION = 0

# WMO Weather Interpretation Codes → human-readable label
# https://open-meteo.com/en/docs#weathervariables
_WMO_CODES: dict[int, str] = {
    0:  "Clear Sky",
    1:  "Mainly Clear",
    2:  "Partly Cloudy",
    3:  "Overcast",
    45: "Foggy",
    48: "Icy Fog",
    51: "Light Drizzle",
    53: "Drizzle",
    55: "Heavy Drizzle",
    56: "Freezing Drizzle",
    57: "Heavy Freezing Drizzle",
    61: "Light Rain",
    63: "Rain",
    65: "Heavy Rain",
    66: "Freezing Rain",
    67: "Heavy Freezing Rain",
    71: "Light Snow",
    73: "Snow",
    75: "Heavy Snow",
    77: "Snow Grains",
    80: "Rain Showers",
    81: "Moderate Showers",
    82: "Heavy Showers",
    85: "Snow Showers",
    86: "Heavy Snow Showers",
    95: "Thunderstorm",
    96: "Thunderstorm w/ Hail",
    99: "Heavy Thunderstorm",
}


def set_cache(cache) -> None:
    """Inject cache instance."""
    global _cache
    _cache = cache


def wmo_to_text(code: int) -> str:
    """Convert WMO weather code to human-readable condition string.

    Args:
        code: WMO weather interpretation code.

    Returns:
        Condition label e.g. 'Partly Cloudy', 'Rain', 'Clear Sky'.
    """
    return _WMO_CODES.get(code, f"Code {code}")


def get_weather(lat: float, lon: float) -> dict:
    """Fetch current weather from Open-Meteo including condition, precip, cloud cover.

    Args:
        lat: Latitude.
        lon: Longitude.

    Returns:
        Raw Open-Meteo response dict, empty dict on failure.
    """
    try:
        params = {
            "latitude":         lat,
            "longitude":        lon,
            "current":          (
                "temperature_2m,wind_speed_10m,wind_direction_10m,"
                "weather_code,precipitation,cloud_cover"
            ),
            "temperature_unit": "fahrenheit",
            "wind_speed_unit":  "mph",
            "forecast_days":    1,
        }
        resp = requests.get(config.WEATHER_API_URL, params=params, timeout=10)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        logger.warning("Weather API failed (lat=%s, lon=%s): %s", lat, lon, exc)
        return {}


def get_stadium_weather(stadium_name: str) -> Weather:
    """Fetch weather for an MLB stadium by name.

    Dome stadiums receive neutral defaults — weather is irrelevant indoors.

    Args:
        stadium_name: Stadium name (full MLB API name accepted).

    Returns:
        Weather dataclass instance with condition_text populated.
    """
    is_dome = stadium_name in config.DOME_STADIUMS or any(
        d.lower() in stadium_name.lower() for d in config.DOME_STADIUMS
    )

    if is_dome:
        return Weather(
            stadium=stadium_name,
            temp_f=_DEFAULT_TEMP_F,
            wind_speed_mph=0.0,
            wind_direction_deg=0.0,
            condition_code=_DEFAULT_CONDITION,
            fetched_at=datetime.utcnow(),
            is_dome=True,
            condition_text="Indoor",
            precipitation_mm=0.0,
            cloud_cover_pct=0,
        )

    cache_key = f"weather_{stadium_name.replace(' ', '_')}"
    if _cache:
        cached = _cache.get(cache_key)
        if cached is not None:
            return Weather(
                stadium=stadium_name,
                temp_f=cached.get("temp_f", _DEFAULT_TEMP_F),
                wind_speed_mph=cached.get("wind_speed_mph", _DEFAULT_WIND_MPH),
                wind_direction_deg=cached.get("wind_direction_deg", _DEFAULT_WIND_DEG),
                condition_code=cached.get("condition_code", _DEFAULT_CONDITION),
                fetched_at=datetime.utcnow(),
                is_dome=False,
                condition_text=cached.get("condition_text", "Unknown"),
                precipitation_mm=cached.get("precipitation_mm", 0.0),
                cloud_cover_pct=cached.get("cloud_cover_pct", 0),
            )

    coords = config.STADIUM_COORDS.get(stadium_name) or _fuzzy_coords(stadium_name)
    if not coords:
        logger.warning("No coordinates for stadium: %s — using defaults", stadium_name)
        return _default_weather(stadium_name)

    raw = get_weather(coords["lat"], coords["lon"])
    if not raw:
        return _default_weather(stadium_name)

    current = raw.get("current", {})
    code    = int(current.get("weather_code", _DEFAULT_CONDITION))

    weather = Weather(
        stadium=stadium_name,
        temp_f=float(current.get("temperature_2m", _DEFAULT_TEMP_F)),
        wind_speed_mph=float(current.get("wind_speed_10m", _DEFAULT_WIND_MPH)),
        wind_direction_deg=float(current.get("wind_direction_10m", _DEFAULT_WIND_DEG)),
        condition_code=code,
        fetched_at=datetime.utcnow(),
        is_dome=False,
        condition_text=wmo_to_text(code),
        precipitation_mm=float(current.get("precipitation", 0.0)),
        cloud_cover_pct=int(current.get("cloud_cover", 0)),
    )

    if _cache:
        _cache.set(cache_key, {
            "temp_f":            weather.temp_f,
            "wind_speed_mph":    weather.wind_speed_mph,
            "wind_direction_deg": weather.wind_direction_deg,
            "condition_code":    weather.condition_code,
            "condition_text":    weather.condition_text,
            "precipitation_mm":  weather.precipitation_mm,
            "cloud_cover_pct":   weather.cloud_cover_pct,
        })

    return weather


def _default_weather(stadium_name: str) -> Weather:
    """Return neutral default weather for unknown/failed lookups."""
    return Weather(
        stadium=stadium_name,
        temp_f=_DEFAULT_TEMP_F,
        wind_speed_mph=_DEFAULT_WIND_MPH,
        wind_direction_deg=_DEFAULT_WIND_DEG,
        condition_code=_DEFAULT_CONDITION,
        fetched_at=datetime.utcnow(),
        is_dome=False,
        condition_text="Unknown",
        precipitation_mm=0.0,
        cloud_cover_pct=0,
    )


def _fuzzy_coords(stadium_name: str) -> dict | None:
    """Find coordinates by partial name match.

    Handles full MLB API names like 'Oriole Park at Camden Yards'
    matching config key 'Camden Yards'.

    Args:
        stadium_name: Full stadium name string.

    Returns:
        Coords dict or None.
    """
    name_lower = stadium_name.lower()
    for known_name, coords in config.STADIUM_COORDS.items():
        if known_name.lower() in name_lower:
            return coords
        words = [w for w in name_lower.split() if len(w) > 4]
        if any(w in known_name.lower() for w in words):
            return coords
    return None
