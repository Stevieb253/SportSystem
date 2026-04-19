# api/odds_api.py
# The Odds API — free tier 500 requests/month.
#
# QUOTA POLICY (read before touching this file):
#   • 500 requests/month hard limit on the free tier.
#   • NEVER auto-fetch on page load — all fetches MUST be manually triggered
#     by the user via the "Refresh Odds" button.
#   • Cache all odds responses for ODDS_CACHE_TTL_HOURS (default 8 h).
#   • Track remaining requests from the X-Requests-Remaining response header.
#   • Typical cost: 1 (events) + N games (hit props) + N games (HR props)
#     ≈ 1 + 15 + 15 = 31 requests per full refresh on a heavy day.
#   • At 1 refresh/day = ~930/month — watch quota on busy days!
#   • Each user running the app locally uses their own .env ODDS_API_KEY.

import logging
from typing import Any

import requests

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import config

logger = logging.getLogger(__name__)

_cache = None

# In-memory quota tracking (resets on server restart — not persisted to disk)
_quota_remaining: int | None = None   # None = never seen a header yet
_quota_used_session: int     = 0      # calls made since server start

# Cache TTL for all odds data (conservative to protect monthly quota)
ODDS_CACHE_TTL_HOURS = 8.0


def set_cache(cache: Any) -> None:
    """Inject cache singleton (called from routes.py startup)."""
    global _cache
    _cache = cache


def get_quota_info() -> dict:
    """Return current quota snapshot for the status endpoint."""
    return {
        "remaining":     _quota_remaining,
        "used_session":  _quota_used_session,
        "key_configured": bool(config.ODDS_API_KEY),
    }


# ── Internal HTTP helper ──────────────────────────────────────────────────────

def _get(url: str, params: dict | None = None) -> Any:
    """HTTP GET with quota header tracking. Returns JSON or None on failure."""
    global _quota_remaining, _quota_used_session
    if not config.ODDS_API_KEY:
        return None
    try:
        resp = requests.get(url, params=params, timeout=10)
        # Parse remaining-requests header (case-insensitive)
        rem = resp.headers.get("x-requests-remaining") or resp.headers.get("X-Requests-Remaining")
        if rem is not None:
            try:
                _quota_remaining = int(rem)
            except ValueError:
                pass
        _quota_used_session += 1
        resp.raise_for_status()
        logger.debug("Odds API %s → %d  (remaining=%s)", url, resp.status_code, _quota_remaining)
        return resp.json()
    except Exception as exc:
        logger.warning("Odds API request failed (%s): %s", url, exc)
        return None


# ── Public API ────────────────────────────────────────────────────────────────

def get_events() -> list[dict]:
    """Fetch today's MLB event list (IDs required for prop lookups).

    Results cached for ODDS_CACHE_TTL_HOURS.  Only called during a manual
    odds refresh — never on page load.
    """
    cache_key = "odds_events"
    if _cache:
        cached = _cache.get(cache_key, ttl_hours=ODDS_CACHE_TTL_HOURS)
        if cached is not None:
            return cached

    url = f"{config.ODDS_API_URL}/sports/baseball_mlb/events"
    data = _get(url, {"apiKey": config.ODDS_API_KEY})
    result = data if isinstance(data, list) else []
    if _cache and result:
        _cache.set(cache_key, result)
    return result


def get_player_props(event_id: str, market: str) -> dict:
    """Fetch player prop odds for one game and one market.

    Args:
        event_id: Odds API event UUID from get_events().
        market:   "batter_hits" | "batter_home_runs" | "batter_total_bases"

    Results cached for ODDS_CACHE_TTL_HOURS.
    """
    cache_key = f"odds_props_{event_id}_{market}"
    if _cache:
        cached = _cache.get(cache_key, ttl_hours=ODDS_CACHE_TTL_HOURS)
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
    """Convert American odds integer to implied probability (0–1)."""
    if american_odds >= 0:
        return 100 / (american_odds + 100)
    return abs(american_odds) / (abs(american_odds) + 100)


def get_best_book_odds(props_dict: dict, player_name: str) -> dict | None:
    """Find the best OVER odds for a player across all sportsbooks.

    Args:
        props_dict:  Raw response from get_player_props().
        player_name: Full player name to search for.

    Returns:
        {"best_odds": "+350", "best_book": "DraftKings", "implied_prob": 0.222}
        or None if not found.
    """
    best_odds: int | None = None
    best_book: str = ""

    for bookmaker in props_dict.get("bookmakers", []):
        book_title = bookmaker.get("title", "")
        for market in bookmaker.get("markets", []):
            for outcome in market.get("outcomes", []):
                # Must be an OVER outcome
                if outcome.get("name", "").upper() not in ("OVER", "YES"):
                    continue
                # Match player name against description or participant field
                participant = outcome.get("participant") or outcome.get("description", "")
                if player_name.lower() not in str(participant).lower():
                    continue
                odds_val = outcome.get("price")
                if odds_val is None:
                    continue
                try:
                    odds_int = int(odds_val)
                except (ValueError, TypeError):
                    continue
                if best_odds is None or odds_int > best_odds:
                    best_odds = odds_int
                    best_book = book_title

    if best_odds is None:
        return None

    return {
        "best_odds":    f"+{best_odds}" if best_odds >= 0 else str(best_odds),
        "best_book":    best_book,
        "implied_prob": round(american_to_implied_prob(best_odds), 4),
    }


def fetch_all_props_for_today(player_names: list[str]) -> dict[str, dict]:
    """Fetch hit + HR props for all listed players across all today's games.

    This is the only function that should be called from the manual refresh
    endpoint (/api/odds/refresh).  It burns 1 + 2*N requests where N = number
    of games that have matching events.

    Returns:
        dict keyed by player_name (hit odds) or player_name+"_hr" (HR odds).
        Each value: {"best_odds", "best_book", "implied_prob", "bet_type"}
        Empty dict when no API key is configured or all calls fail.
    """
    if not config.ODDS_API_KEY:
        logger.info("Odds API key not configured — skipping odds fetch")
        return {}

    events = get_events()
    if not events:
        logger.warning("No MLB events returned from Odds API")
        return {}

    name_lower = {n.lower(): n for n in player_names}
    result: dict[str, dict] = {}

    def _match_name(description: str) -> str | None:
        """Find canonical player name whose name appears in description."""
        desc_lower = description.lower()
        for nl, canonical in name_lower.items():
            if nl in desc_lower or desc_lower in nl:
                return canonical
        return None

    def _absorb(props_dict: dict, bet_type: str) -> None:
        """Extract best OVER odds per player from a raw props response."""
        for bookmaker in props_dict.get("bookmakers", []):
            book_title = bookmaker.get("title", "")
            for market in bookmaker.get("markets", []):
                for outcome in market.get("outcomes", []):
                    if outcome.get("name", "").upper() not in ("OVER", "YES"):
                        continue
                    desc = outcome.get("participant") or outcome.get("description", "")
                    if not desc:
                        continue
                    canonical = _match_name(str(desc))
                    if not canonical:
                        continue
                    odds_val = outcome.get("price")
                    if odds_val is None:
                        continue
                    try:
                        odds_int = int(odds_val)
                    except (ValueError, TypeError):
                        continue

                    key = canonical + ("_hr" if bet_type == "hr" else "")
                    existing = result.get(key, {})
                    if not existing or odds_int > existing.get("_raw", -9999):
                        result[key] = {
                            "best_odds":    f"+{odds_int}" if odds_int >= 0 else str(odds_int),
                            "best_book":    book_title,
                            "implied_prob": round(american_to_implied_prob(odds_int), 4),
                            "bet_type":     bet_type,
                            "_raw":         odds_int,
                        }

    for event in events:
        event_id = event.get("id")
        if not event_id:
            continue
        hit_props = get_player_props(event_id, "batter_hits")
        if hit_props:
            _absorb(hit_props, "hit")
        hr_props = get_player_props(event_id, "batter_home_runs")
        if hr_props:
            _absorb(hr_props, "hr")

    # Strip internal tracking field
    for v in result.values():
        v.pop("_raw", None)

    logger.info(
        "Odds refresh complete: %d players found, quota remaining=%s",
        len(result), _quota_remaining,
    )
    return result
