# api/bvp_api.py
# Batter-vs-pitcher career stats from the MLB Stats API.
# No API key required. Results are cached 24 hours — career BvP numbers
# don't change intra-day and the MLB API endpoint is rate-limited.
#
# USAGE:
#   from api import bvp_api
#   bvp_api.set_cache(cache)
#   stats = bvp_api.get_bvp_stats(batter_id=660271, pitcher_id=543037)
#
# RETURNS:
#   {
#     "available": True,
#     "ab": 24, "hits": 8, "hr": 2, "doubles": 1, "triples": 0,
#     "k": 6, "bb": 4,
#     "avg": 0.333, "obp": 0.414, "slg": 0.583, "ops": 0.997,
#     "sample_size": "moderate",   # tiny / small / moderate / large
#     "warning": None,             # populated for tiny/small samples
#     "source": "MLB Stats API"
#   }
#   or {"available": False, ...nulls..., "source": "MLB Stats API"} on miss.

import logging
import sys
import os
from typing import Any

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import config

logger = logging.getLogger(__name__)

_cache: Any = None


def set_cache(cache: Any) -> None:
    """Inject shared cache instance."""
    global _cache
    _cache = cache


def get_bvp_stats(batter_id: int, pitcher_id: int) -> dict:
    """Return career batter-vs-pitcher stats from MLB Stats API.

    Cached 24 hours — safe to call per-player without burning quota.

    Args:
        batter_id:  MLBAM batter ID.
        pitcher_id: MLBAM pitcher ID.

    Returns:
        Stats dict with availability flag, sample-size label, and warning.
    """
    if not batter_id or not pitcher_id:
        return _empty_result()

    cache_key = f"bvp_rich_{batter_id}_{pitcher_id}"
    if _cache:
        cached = _cache.get(cache_key, ttl_hours=24)
        if cached is not None:
            return cached

    result = _fetch_bvp(batter_id, pitcher_id)

    if _cache and result["available"]:
        _cache.set(cache_key, result)

    return result


# ── Internal ───────────────────────────────────────────────────────────────────

def _fetch_bvp(batter_id: int, pitcher_id: int) -> dict:
    url = f"{config.MLB_API_BASE_URL}/people/{batter_id}/stats"
    params = {
        "stats":            "vsPlayer",
        "opposingPlayerId": pitcher_id,
        "group":            "hitting",
        "sportId":          1,
    }

    try:
        resp = requests.get(url, params=params, timeout=(5, 10))
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.warning(
            "BvP fetch failed (batter=%d pitcher=%d): %s", batter_id, pitcher_id, exc
        )
        return _empty_result()

    try:
        stats_list = data.get("stats", [])
        if not stats_list:
            return _empty_result()

        splits = stats_list[0].get("splits", [])
        if not splits:
            return _empty_result()

        stat = splits[0].get("stat", {})
        if not stat:
            return _empty_result()

        ab      = _int(stat, "atBats")
        hits    = _int(stat, "hits")
        hr      = _int(stat, "homeRuns")
        doubles = _int(stat, "doubles")
        triples = _int(stat, "triples")
        k       = _int(stat, "strikeOuts")
        bb      = _int(stat, "baseOnBalls")
        avg     = _flt(stat, "avg")
        obp     = _flt(stat, "obp")
        slg     = _flt(stat, "slg")
        ops     = _flt(stat, "ops")

        sample_size, warning = _classify_sample(ab)

        return {
            "available":   True,
            "ab":          ab,
            "hits":        hits,
            "hr":          hr,
            "doubles":     doubles,
            "triples":     triples,
            "k":           k,
            "bb":          bb,
            "avg":         avg,
            "obp":         obp,
            "slg":         slg,
            "ops":         ops,
            "sample_size": sample_size,
            "warning":     warning,
            "source":      "MLB Stats API",
        }

    except Exception as exc:
        logger.warning(
            "BvP parse failed (batter=%d pitcher=%d): %s", batter_id, pitcher_id, exc
        )
        return _empty_result()


def _classify_sample(ab: int) -> tuple[str, str | None]:
    """Return (label, warning_string) based on at-bat count."""
    if ab == 0:
        return "none", None
    if ab < config.BVP_MIN_AB:
        return "tiny", f"Very small sample ({ab} AB) — not statistically meaningful"
    if ab < config.BVP_SMALL_AB:
        return "small", f"Small sample ({ab} AB) — treat with caution"
    if ab < config.BVP_MODERATE_AB:
        return "moderate", None
    return "large", None


def _int(stat: dict, key: str) -> int:
    try:
        return int(stat.get(key, 0) or 0)
    except (TypeError, ValueError):
        return 0


def _flt(stat: dict, key: str) -> float | None:
    v = stat.get(key)
    if v in (None, "", "-.--", ".---", "---", "-.---"):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _empty_result() -> dict:
    return {
        "available":   False,
        "ab":          0,
        "hits":        0,
        "hr":          0,
        "doubles":     0,
        "triples":     0,
        "k":           0,
        "bb":          0,
        "avg":         None,
        "obp":         None,
        "slg":         None,
        "ops":         None,
        "sample_size": "none",
        "warning":     None,
        "source":      "MLB Stats API",
    }
