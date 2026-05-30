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

# ── Explicit coordinate aliases for renamed / alternate venue names ───────────
# Checked BEFORE fuzzy matching to prevent generic words like "field" or "park"
# from matching the wrong stadium.  Add entries here whenever the MLB API
# starts using a new venue name mid-season.
_VENUE_COORD_ALIASES: dict[str, dict] = {
    # HOU 2025 rename: Minute Maid Park → Daikin Park (same retractable-roof building)
    "Daikin Park":                    {"lat": 29.7573, "lon": -95.3555},
    # CWS rename: Guaranteed Rate Field → Rate Field (Southside Chicago)
    "Rate Field":                     {"lat": 41.8300, "lon": -87.6339},
    # LAD sponsorship overlay name (same GPS as Dodger Stadium)
    "UNIQLO Field at Dodger Stadium": {"lat": 34.0739, "lon": -118.2400},
    # MLB API casing variant (lowercase p)
    "loanDepot park":                 {"lat": 25.7781, "lon": -80.2197},
}

# Words too generic to drive a fuzzy match — "field" matches Globe Life Field,
# American Family Field, AND Guaranteed Rate Field; "park" matches most others.
_FUZZY_STOP_WORDS: frozenset[str] = frozenset({
    "park", "field", "stadium", "centre", "center", "ballpark", "arena",
})

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
        data = resp.json()

        # ── Diagnostic logging (DEBUG) ─────────────────────────────────────
        current = data.get("current", {})
        logger.debug(
            "Open-Meteo raw fields — lat=%.4f lon=%.4f | "
            "temp=%.1f°F  wind=%.1f mph@%.0f°  weather_code=%s  "
            "cloud_cover=%s%%  precipitation=%s mm",
            lat, lon,
            current.get("temperature_2m", "MISSING"),
            current.get("wind_speed_10m", "MISSING"),
            current.get("wind_direction_10m", "MISSING"),
            current.get("weather_code", "MISSING"),
            current.get("cloud_cover", "MISSING"),
            current.get("precipitation", "MISSING"),
        )

        return data
    except Exception as exc:
        logger.warning("Weather API failed (lat=%s, lon=%s): %s", lat, lon, exc)
        return {}


def get_stadium_weather(stadium_name: str, ttl_hours: float = 1.0) -> Weather:
    """Fetch weather for an MLB stadium by name.

    Dome stadiums receive neutral defaults — weather is irrelevant indoors.

    Args:
        stadium_name: Stadium name (full MLB API name accepted).
        ttl_hours:    Cache TTL in hours (default 1.0).  Pass a shorter value
                      (e.g. 0.5) for fresher data on the game detail page.

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
        cached = _cache.get(cache_key, ttl_hours=ttl_hours)
        if cached is not None:
            # Restore the original fetch time from the cached dict so downstream
            # code (and the game detail template) can show an accurate timestamp.
            raw_fetched = cached.get("fetched_at")
            try:
                fetched_at = datetime.fromisoformat(raw_fetched) if raw_fetched else datetime.utcnow()
            except (ValueError, TypeError):
                fetched_at = datetime.utcnow()
            return Weather(
                stadium=stadium_name,
                temp_f=cached.get("temp_f", _DEFAULT_TEMP_F),
                wind_speed_mph=cached.get("wind_speed_mph", _DEFAULT_WIND_MPH),
                wind_direction_deg=cached.get("wind_direction_deg", _DEFAULT_WIND_DEG),
                condition_code=cached.get("condition_code", _DEFAULT_CONDITION),
                fetched_at=fetched_at,
                is_dome=False,
                condition_text=cached.get("condition_text", "Unknown"),
                precipitation_mm=cached.get("precipitation_mm", 0.0),
                cloud_cover_pct=cached.get("cloud_cover_pct", 0),
            )

    coords = (
        config.STADIUM_COORDS.get(stadium_name)
        or _VENUE_COORD_ALIASES.get(stadium_name)
        or _fuzzy_coords(stadium_name)
    )
    if not coords:
        logger.warning(
            "No coordinates for stadium %r — returning default weather "
            "(72°F / 5 mph / Unknown condition). Add to _VENUE_COORD_ALIASES "
            "in weather_api.py to fix.", stadium_name,
        )
        return _default_weather(stadium_name)

    logger.debug("Fetching weather for %r at lat=%.4f lon=%.4f",
                 stadium_name, coords["lat"], coords["lon"])

    raw = get_weather(coords["lat"], coords["lon"])
    if not raw:
        logger.warning("Weather API returned empty response for %r", stadium_name)
        return _default_weather(stadium_name)

    current = raw.get("current", {})
    code    = int(current.get("weather_code", _DEFAULT_CONDITION))

    # ── Diagnostic logging for condition and cloud cover ──────────────────
    condition_str = wmo_to_text(code)
    cloud_raw     = current.get("cloud_cover")
    logger.debug(
        "Stadium weather parsed — %r: code=%d → %r  cloud_cover=%s%%",
        stadium_name, code, condition_str, cloud_raw,
    )

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
            "temp_f":             weather.temp_f,
            "wind_speed_mph":     weather.wind_speed_mph,
            "wind_direction_deg": weather.wind_direction_deg,
            "condition_code":     weather.condition_code,
            "condition_text":     weather.condition_text,
            "precipitation_mm":   weather.precipitation_mm,
            "cloud_cover_pct":    weather.cloud_cover_pct,
            # Store the actual fetch time so cache reads can restore it accurately
            "fetched_at":         weather.fetched_at.isoformat(),
        })

    return weather


def _default_weather(stadium_name: str) -> Weather:
    """Return a clearly-labelled fallback when coordinates or API fetch fails.

    Uses condition_text='Data unavailable' (not 'Unknown') so the UI can
    display a meaningful message rather than showing misleading default values.
    The temp/wind values are also set to None-equivalent so downstream code can
    detect that this is a fallback and render it differently if desired.
    """
    return Weather(
        stadium=stadium_name,
        temp_f=_DEFAULT_TEMP_F,
        wind_speed_mph=_DEFAULT_WIND_MPH,
        wind_direction_deg=_DEFAULT_WIND_DEG,
        condition_code=_DEFAULT_CONDITION,
        fetched_at=datetime.utcnow(),
        is_dome=False,
        condition_text="Data unavailable",
        precipitation_mm=0.0,
        cloud_cover_pct=0,
    )


def _fuzzy_coords(stadium_name: str) -> dict | None:
    """Find coordinates by partial name match against config.STADIUM_COORDS.

    Two-pass approach:
      1. Substring: is any known name fully contained in the given name?
         e.g. "Camden Yards" ⊂ "Oriole Park at Camden Yards" → match
      2. Distinctive-word: does any long, non-generic word from the given
         name appear in a known name?
         e.g. "dodger" from "UNIQLO Field at Dodger Stadium" → "Dodger Stadium"

    Words in _FUZZY_STOP_WORDS ("field", "park", etc.) are excluded from
    pass 2 to prevent "Rate Field" matching "Globe Life Field" (wrong city).

    Args:
        stadium_name: Full stadium name string from the MLB API.

    Returns:
        Coords dict or None if no confident match found.
    """
    name_lower = stadium_name.lower()
    for known_name, coords in config.STADIUM_COORDS.items():
        # Pass 1: known name is a substring of the provided name
        if known_name.lower() in name_lower:
            logger.debug("Fuzzy coord match (substring): %r → %r", stadium_name, known_name)
            return coords

    for known_name, coords in config.STADIUM_COORDS.items():
        # Pass 2: any distinctive long word from the provided name appears in a known name
        words = [
            w for w in name_lower.split()
            if len(w) > 4 and w not in _FUZZY_STOP_WORDS
        ]
        if words and any(w in known_name.lower() for w in words):
            logger.debug("Fuzzy coord match (word): %r → %r (words=%s)", stadium_name, known_name, words)
            return coords

    return None
