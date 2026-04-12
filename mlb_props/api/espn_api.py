# api/espn_api.py
# ESPN unofficial endpoints — free, no key required.

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
    """GET helper — returns parsed JSON or empty dict on failure."""
    try:
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        logger.warning("ESPN API request failed (%s): %s", url, exc)
        return {}


def get_scoreboard(date_str: str | None = None) -> dict:
    """Fetch ESPN MLB scoreboard, optionally for a specific date.

    Args:
        date_str: Optional date in YYYYMMDD format (ESPN format).

    Returns:
        Raw ESPN scoreboard dict.
    """
    url = f"{config.ESPN_API_BASE_URL}/scoreboard"
    params: dict = {}
    if date_str:
        params["dates"] = date_str.replace("-", "")

    cache_key = f"espn_scoreboard_{date_str or 'today'}"
    if _cache:
        cached = _cache.get(cache_key)
        if cached is not None:
            return cached

    data = _get(url, params)
    if _cache and data:
        _cache.set(cache_key, data)
    return data


def get_game_summary(espn_game_id: str) -> dict:
    """Fetch detailed game summary from ESPN.

    Args:
        espn_game_id: ESPN game identifier.

    Returns:
        Raw game summary dict.
    """
    url = f"{config.ESPN_API_BASE_URL}/summary"
    return _get(url, {"event": espn_game_id})


def get_news() -> list[dict]:
    """Fetch latest MLB news headlines from ESPN.

    Returns:
        List of news article dicts.
    """
    url = f"{config.ESPN_API_BASE_URL}/news"
    data = _get(url)
    return data.get("articles", [])
