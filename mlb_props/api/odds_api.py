# api/odds_api.py
# The Odds API — free tier 500 requests/month.

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


def _get(url: str, params: dict | None = None) -> Any:
    """GET helper — returns parsed JSON or None on failure."""
    if not config.ODDS_API_KEY:
        return None
    try:
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        logger.warning("Odds API request failed (%s): %s", url, exc)
        return None


def get_events() -> list[dict]:
    """Fetch upcoming MLB events with IDs for prop lookups.

    Returns:
        List of event dicts, empty list if API key missing or call fails.
    """
    url = f"{config.ODDS_API_URL}/sports/baseball_mlb/events"
    data = _get(url, {"apiKey": config.ODDS_API_KEY})
    return data if isinstance(data, list) else []


def get_player_props(event_id: str, market: str) -> dict:
    """Fetch player prop odds for a specific game and market.

    Args:
        event_id: Odds API event ID from get_events.
        market: One of batter_hits, batter_home_runs, batter_total_bases,
                pitcher_strikeouts, batter_rbis.

    Returns:
        Raw props dict, empty dict on failure.
    """
    cache_key = f"odds_props_{event_id}_{market}"
    if _cache:
        cached = _cache.get(cache_key)
        if cached is not None:
            return cached

    url = f"{config.ODDS_API_URL}/sports/baseball_mlb/events/{event_id}/odds"
    params = {
        "apiKey":     config.ODDS_API_KEY,
        "regions":    "us",
        "markets":    market,
        "bookmakers": "draftkings,fanduel,betmgm",
    }
    data = _get(url, params)
    result = data if isinstance(data, dict) else {}
    if _cache and result:
        _cache.set(cache_key, result)
    return result


def american_to_implied_prob(american_odds: int) -> float:
    """Convert American odds to implied probability.

    Args:
        american_odds: Odds as integer e.g. +350 or -110.

    Returns:
        Implied probability as float between 0 and 1.
    """
    if american_odds >= 0:
        return 100 / (american_odds + 100)
    return abs(american_odds) / (abs(american_odds) + 100)


def get_best_book_odds(props_dict: dict, player_name: str) -> dict | None:
    """Find the best available odds for a player across all sportsbooks.

    Args:
        props_dict: Raw response from get_player_props.
        player_name: Player name to search for.

    Returns:
        Dict with best_odds, best_book, implied_prob — or None if not found.
    """
    best_odds: int | None = None
    best_book: str = ""

    for bookmaker in props_dict.get("bookmakers", []):
        book_title = bookmaker.get("title", "")
        for market in bookmaker.get("markets", []):
            for outcome in market.get("outcomes", []):
                if player_name.lower() in outcome.get("description", "").lower():
                    odds_val = outcome.get("price")
                    if odds_val is not None:
                        try:
                            odds_int = int(odds_val)
                        except (ValueError, TypeError):
                            continue
                        # Prefer most positive odds (best value for bettor)
                        if best_odds is None or odds_int > best_odds:
                            best_odds = odds_int
                            best_book = book_title

    if best_odds is None:
        return None

    implied = american_to_implied_prob(best_odds)
    return {
        "best_odds":    f"+{best_odds}" if best_odds >= 0 else str(best_odds),
        "best_book":    best_book,
        "implied_prob": round(implied, 4),
    }
