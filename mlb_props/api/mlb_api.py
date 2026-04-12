# api/mlb_api.py
# Official MLB Stats API — free, no key required.

import logging
from typing import Any

import requests

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import config

logger = logging.getLogger(__name__)

_cache = None


def set_cache(cache: Any) -> None:
    """Inject cache instance."""
    global _cache
    _cache = cache


def _get(url: str, params: dict | None = None) -> dict:
    """GET helper — returns parsed JSON dict or empty dict on failure."""
    try:
        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        logger.warning("MLB API request failed (%s): %s", url, exc)
        return {}


def get_schedule(date_str: str) -> list[dict]:
    """Fetch MLB schedule for a given date.

    Args:
        date_str: Date in YYYY-MM-DD format.

    Returns:
        List of raw game dicts from the MLB API.
    """
    cache_key = f"mlb_schedule_{date_str}"
    if _cache:
        cached = _cache.get(cache_key)
        if cached is not None:
            return cached

    url = f"{config.MLB_API_BASE_URL}/schedule"
    params = {
        "date":    date_str,
        "sportId": 1,
        "hydrate": "probablePitcher,lineups,team,venue(timezone),weather",
    }
    data = _get(url, params)
    games: list[dict] = []
    for date_entry in data.get("dates", []):
        games.extend(date_entry.get("games", []))

    if _cache and games:
        _cache.set(cache_key, games)
    return games


def get_live_feed(game_pk: int) -> dict:
    """Fetch live game feed — current at-bat, pitch log, scores.

    Args:
        game_pk: MLB game primary key.

    Returns:
        Raw live feed dict.
    """
    url = f"{config.MLB_API_LIVE_URL}/game/{game_pk}/feed/live"
    return _get(url)


def get_boxscore(game_pk: int) -> dict:
    """Fetch completed game boxscore.

    Args:
        game_pk: MLB game primary key.

    Returns:
        Raw boxscore dict.
    """
    cache_key = f"mlb_boxscore_{game_pk}"
    if _cache:
        cached = _cache.get(cache_key)
        if cached is not None:
            return cached

    url = f"{config.MLB_API_BASE_URL}/game/{game_pk}/boxscore"
    data = _get(url)
    if _cache and data:
        _cache.set(cache_key, data)
    return data


def get_player_info(player_id: int) -> dict:
    """Fetch player profile including season hitting stats.

    Args:
        player_id: MLBAM player ID.

    Returns:
        Raw player info dict.
    """
    cache_key = f"mlb_player_{player_id}"
    if _cache:
        cached = _cache.get(cache_key)
        if cached is not None:
            return cached

    url = f"{config.MLB_API_BASE_URL}/people/{player_id}"
    params = {"hydrate": "stats(group=hitting,type=season)"}
    data = _get(url, params)
    if _cache and data:
        _cache.set(cache_key, data)
    return data


def get_standings(season: int) -> dict:
    """Fetch league standings for a season.

    Args:
        season: Season year.

    Returns:
        Raw standings dict.
    """
    cache_key = f"mlb_standings_{season}"
    if _cache:
        cached = _cache.get(cache_key)
        if cached is not None:
            return cached

    url = f"{config.MLB_API_BASE_URL}/standings"
    params = {"leagueId": "103,104", "season": season}
    data = _get(url, params)
    if _cache and data:
        _cache.set(cache_key, data)
    return data


def parse_live_pitches(live_feed: dict) -> list[dict]:
    """Extract structured pitch list from live feed response.

    Args:
        live_feed: Raw live feed dict from get_live_feed.

    Returns:
        List of pitch dicts sorted by pitch_number.
    """
    pitches: list[dict] = []
    try:
        plays = (
            live_feed.get("liveData", {})
            .get("plays", {})
            .get("allPlays", [])
        )
        pitch_num = 0
        for play in plays:
            events = play.get("playEvents", [])
            for event in events:
                if event.get("isPitch", False):
                    pitch_num += 1
                    details = event.get("pitchData", {})
                    count = event.get("count", {})
                    pitches.append({
                        "pitch_number": pitch_num,
                        "pitch_type":   event.get("details", {}).get("type", {}).get("description", ""),
                        "speed":        details.get("startSpeed", 0.0),
                        "zone":         details.get("zone", 0),
                        "description":  event.get("details", {}).get("description", ""),
                        "balls":        count.get("balls", 0),
                        "strikes":      count.get("strikes", 0),
                        "outs":         count.get("outs", 0),
                        "result":       play.get("result", {}).get("type", ""),
                        "event":        play.get("result", {}).get("event", ""),
                    })
    except Exception as exc:
        logger.warning("parse_live_pitches failed: %s", exc)
    return sorted(pitches, key=lambda p: p["pitch_number"])
