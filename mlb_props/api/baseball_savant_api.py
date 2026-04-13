# api/baseball_savant_api.py
# Primary Statcast data source — scrapes Baseball Savant leaderboards.
# pybaseball / FanGraphs is the fallback if Savant is unavailable.

import logging
import time
from datetime import date, timedelta
from typing import Any

import requests

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import config

logger = logging.getLogger(__name__)

# Module-level cache reference injected by pipeline at startup
_cache = None


def set_cache(cache: Any) -> None:
    """Inject cache instance used by all functions in this module."""
    global _cache
    _cache = cache


def _get(url: str, params: dict) -> list[dict]:
    """Shared GET helper — returns parsed JSON list or empty list on failure."""
    try:
        resp = requests.get(
            url,
            params=params,
            headers=config.SAVANT_HEADERS,
            timeout=20,
        )
        resp.raise_for_status()

        # Guard against empty body (Savant returns empty string when no data)
        text = resp.text.strip()
        if not text:
            logger.warning("Baseball Savant returned empty body (%s %s)", url, params)
            return []

        # Some responses are CSV — detect and convert
        if text.startswith('"') or (text and text[0] not in '{['):
            return _parse_csv_fallback(text)

        data = resp.json()
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return data.get("data", data.get("rows", []))
        return []
    except Exception as exc:
        logger.warning("Baseball Savant request failed (%s %s): %s", url, params, exc)
        return []


def _parse_csv_fallback(text: str) -> list[dict]:
    """Parse a CSV-format response from Baseball Savant into a list of dicts.

    Baseball Savant CSV responses often include a UTF-8 BOM (\\ufeff) at the
    start. If left in place, DictReader splits the quoted "last_name, first_name"
    header at the comma, mangling that column and shifting all subsequent values.
    Stripping the BOM first ensures correct column alignment.
    """
    import csv, io
    try:
        # Strip UTF-8 BOM so "last_name, first_name" header parses as one quoted column
        clean = text.lstrip('\ufeff')
        reader = csv.DictReader(io.StringIO(clean))
        return [_normalize_savant_row(row) for row in reader]
    except Exception as exc:
        logger.warning("CSV parse failed: %s", exc)
        return []


def _normalize_savant_row(row: dict) -> dict:
    """Normalize Baseball Savant CSV column names to the field names used by normalizer.py.

    Savant CSV columns use different names than what normalizer.py looks up.
    This function adds aliased keys so both names are present in the dict.
    """
    r = dict(row)

    # Synthesize player_name from "last_name, first_name" CSV column
    raw_name = r.get("last_name, first_name", "")
    if raw_name and "," in raw_name:
        last, first = raw_name.split(",", 1)
        r["player_name"] = f"{first.strip()} {last.strip()}"
    elif raw_name:
        r["player_name"] = raw_name

    # Alias CSV column names → normalizer field names (add alias if dst not already present)
    _aliases = {
        "anglesweetspotpercent": "sweet_spot_percent",
        "avg_hit_angle":         "launch_angle_avg",
        "ev95percent":           "hard_hit_percent",
        "avg_best_speed":        "ev50",
        "brl_percent":           "barrel_batted_rate",
        "batting_avg":           "ba",
        # Pitcher aliases
        "p_era":                 "era",
        "p_xera":                "xera",
    }
    for src, dst in _aliases.items():
        if src in r and dst not in r:
            r[dst] = r[src]

    return r


def get_statcast_leaderboard(year: int, player_type: str) -> list[dict]:
    """Fetch exit velocity / barrels leaderboard from Baseball Savant.

    Args:
        year: Season year.
        player_type: 'batter' or 'pitcher'.

    Returns:
        List of player dicts with Statcast metrics.
    """
    cache_key = f"savant_statcast_{player_type}_{year}"
    if _cache:
        cached = _cache.get(cache_key)
        if cached is not None:
            return cached

    params: dict = {
        "type":    player_type,
        "year":    year,
        "min":     "10",           # "q" requires ~120 PA — too strict early season
        "sort":    "barrels_per_pa" if player_type == "batter" else "exit_velocity_avg",
        "sortDir": "desc",
        "results": "all",
        "csv":     "true",         # Force CSV response — confirmed returning 352 batters
    }
    time.sleep(config.SAVANT_REQUEST_DELAY_SECONDS)
    data = _get(config.SAVANT_STATCAST_LEADERBOARD_URL, params)

    if _cache and data:
        _cache.set(cache_key, data)
    return data


def get_custom_leaderboard(year: int, player_type: str) -> list[dict]:
    """Fetch custom leaderboard with xwOBA, whiff%, sweet spot%, etc.

    Args:
        year: Season year.
        player_type: 'batter' or 'pitcher'.

    Returns:
        List of player dicts with custom stat columns.
    """
    cache_key = f"savant_custom_{player_type}_{year}"
    if _cache:
        cached = _cache.get(cache_key)
        if cached is not None:
            return cached

    base_params = (
        config.SAVANT_BATTER_CUSTOM_PARAMS
        if player_type == "batter"
        else config.SAVANT_PITCHER_CUSTOM_PARAMS
    )
    params = {**base_params, "year": year, "min": "10", "results": "all", "csv": "true"}

    time.sleep(config.SAVANT_REQUEST_DELAY_SECONDS)
    data = _get(config.SAVANT_CUSTOM_LEADERBOARD_URL, params)

    if _cache and data:
        _cache.set(cache_key, data)
    return data


def get_expected_stats(year: int, player_type: str) -> list[dict]:
    """Fetch expected statistics leaderboard (xBA, xSLG, xwOBA, xERA).

    Args:
        year: Season year.
        player_type: 'batter' or 'pitcher'.

    Returns:
        List of player dicts with expected stat columns.
    """
    cache_key = f"savant_expected_{player_type}_{year}"
    if _cache:
        cached = _cache.get(cache_key)
        if cached is not None:
            return cached

    params: dict = {
        "type":    player_type,
        "year":    year,
        "min":     "10",    # "q" is too strict early season — use 10 PA/BF minimum
        "results": "all",
        "csv":     "true",
    }
    time.sleep(config.SAVANT_REQUEST_DELAY_SECONDS)
    data = _get(config.SAVANT_EXPECTED_STATS_URL, params)

    if _cache and data:
        _cache.set(cache_key, data)
    return data


def get_percentile_rankings(year: int, player_type: str) -> list[dict]:
    """Fetch Statcast percentile rankings for each metric.

    Args:
        year: Season year.
        player_type: 'batter' or 'pitcher'.

    Returns:
        List of player dicts with percentile ranks.
    """
    cache_key = f"savant_percentiles_{player_type}_{year}"
    if _cache:
        cached = _cache.get(cache_key)
        if cached is not None:
            return cached

    params: dict = {"type": player_type, "year": year}
    time.sleep(config.SAVANT_REQUEST_DELAY_SECONDS)
    data = _get(config.SAVANT_PERCENTILE_URL, params)

    if _cache and data:
        _cache.set(cache_key, data)
    return data


def get_player_page_stats(player_id: int, year: int) -> dict:
    """Fetch an individual player's Baseball Savant page stats.

    Args:
        player_id: MLBAM player ID.
        year: Season year.

    Returns:
        Dict of player page data, empty dict on failure.
    """
    cache_key = f"savant_player_{player_id}_{year}"
    if _cache:
        cached = _cache.get(cache_key)
        if cached is not None:
            return cached

    url = f"{config.SAVANT_PLAYER_URL}/{player_id}"
    params: dict = {"stats": "career", "type": "batter"}
    try:
        time.sleep(config.SAVANT_REQUEST_DELAY_SECONDS)
        resp = requests.get(url, params=params, headers=config.SAVANT_HEADERS, timeout=20)
        resp.raise_for_status()
        data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        if _cache and data:
            _cache.set(cache_key, data)
        return data
    except Exception as exc:
        logger.warning("Savant player page failed (id=%s): %s", player_id, exc)
        return {}


def get_recent_statcast(player_id: int, days: int, player_type: str) -> list[dict]:
    """Fetch recent Statcast search records for a player.

    Args:
        player_id: MLBAM player ID.
        days: Number of recent days to fetch.
        player_type: 'batter' or 'pitcher'.

    Returns:
        List of pitch/batted-ball records.
    """
    # 6-hour TTL: recent form data doesn't change mid-day, and we only need
    # one network call per player per session (threads share the cache).
    cache_key = f"savant_recent_{player_id}_{days}days"
    if _cache:
        cached = _cache.get(cache_key, ttl_hours=6)
        if cached is not None:
            return cached

    today = date.today()
    start = today - timedelta(days=days)
    params: dict = {
        "player_id_type": "mlbam",
        "player_id":      player_id,
        "game_date_gt":   start.strftime("%Y-%m-%d"),
        "game_date_lt":   today.strftime("%Y-%m-%d"),
        "type":           player_type,
        "hfSea":          f"{today.year}|",
        "min_results":    0,
        "group_by":       "name",
        "sort_col":       "pitches",
        "player_event_sort": "api_p_release_speed",
        "sort_order":     "desc",
        "min_pas":        0,
    }
    time.sleep(config.SAVANT_REQUEST_DELAY_SECONDS)
    data = _get(config.SAVANT_SEARCH_URL, params)

    if _cache:
        _cache.set(cache_key, data if data else [])  # cache even on empty result
    return data if data else []


def merge_savant_data(
    statcast_rows: list[dict],
    custom_rows: list[dict],
) -> dict[int, dict]:
    """Merge statcast leaderboard and custom leaderboard by player_id.

    Args:
        statcast_rows: Rows from get_statcast_leaderboard.
        custom_rows: Rows from get_custom_leaderboard.

    Returns:
        Dict keyed by player_id with all Savant metrics merged.
    """
    merged: dict[int, dict] = {}

    for row in statcast_rows:
        pid = _parse_player_id(row)
        if pid:
            merged[pid] = dict(row)

    for row in custom_rows:
        pid = _parse_player_id(row)
        if pid:
            if pid in merged:
                merged[pid].update(row)
            else:
                merged[pid] = dict(row)

    return merged


def calculate_recent_metrics_from_statcast(records: list[dict]) -> dict:
    """Derive recent-form metrics from raw Statcast search records.

    Args:
        records: Raw pitch/batted-ball records from get_recent_statcast.

    Returns:
        Dict with recent_avg, recent_hard_hit_pct, recent_barrel_pct,
        recent_exit_velo.
    """
    if not records:
        return {
            "recent_avg": 0.0,
            "recent_hard_hit_pct": 0.0,
            "recent_barrel_pct": 0.0,
            "recent_exit_velo": 0.0,
        }

    batted_balls = [r for r in records if r.get("type") == "X"]
    hits = [r for r in batted_balls if r.get("events") in (
        "single", "double", "triple", "home_run"
    )]
    hard_hits = [
        r for r in batted_balls
        if _safe_float(r.get("launch_speed")) >= 95.0
    ]
    barrels = [
        r for r in batted_balls
        if str(r.get("barrel", "0")) == "1"
    ]
    exit_velos = [
        _safe_float(r.get("launch_speed"))
        for r in batted_balls
        if _safe_float(r.get("launch_speed")) > 0
    ]

    ab_count = len(batted_balls) or 1
    recent_avg = len(hits) / ab_count
    recent_hard_hit_pct = len(hard_hits) / ab_count
    recent_barrel_pct = len(barrels) / ab_count
    recent_exit_velo = sum(exit_velos) / len(exit_velos) if exit_velos else 0.0

    return {
        "recent_avg":          round(recent_avg, 3),
        "recent_hard_hit_pct": round(recent_hard_hit_pct, 3),
        "recent_barrel_pct":   round(recent_barrel_pct, 3),
        "recent_exit_velo":    round(recent_exit_velo, 1),
    }


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_player_id(row: dict) -> int | None:
    """Extract player ID from a Savant row, trying multiple field names."""
    for key in ("player_id", "mlbam_id", "xMLBAMID", "MLBID"):
        val = row.get(key)
        if val is not None:
            try:
                return int(val)
            except (ValueError, TypeError):
                continue
    return None


def _safe_float(val: Any, default: float = 0.0) -> float:
    """Safely convert a value to float."""
    try:
        return float(val)
    except (TypeError, ValueError):
        return default
