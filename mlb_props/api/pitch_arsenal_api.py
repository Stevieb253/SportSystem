# api/pitch_arsenal_api.py
# Pitch arsenal and batter-vs-pitch-type data from Baseball Savant leaderboards.
#
# Uses two leaderboard endpoints:
#   leaderboard/pitch-arsenal-stats?type=pitcher  — per-pitcher, per-pitch-type stats
#   leaderboard/pitch-arsenal-stats?type=batter   — per-batter, per-pitch-type stats
#   leaderboard/pitch-arsenals?type=avg_speed      — per-pitcher avg velocity by pitch
#
# Leaderboards are fetched once per year, cached 12h, then filtered by player_id.
# Never uses the broken statcast_search endpoint for individual player queries.

import csv
import io
import logging
import time
from typing import Any

import requests

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import config

logger = logging.getLogger(__name__)

_cache: Any = None
_CACHE_TTL_HOURS = 12
_SAVANT_BASE = "https://baseballsavant.mlb.com"


def set_cache(cache: Any) -> None:
    global _cache
    _cache = cache


# ── Public API ────────────────────────────────────────────────────────────────

def get_pitcher_arsenal(pitcher_id: int, year: int) -> dict:
    """Return pitch mix breakdown for a pitcher.

    Returns:
        {available, pitches: [{pitch_type, label, usage_pct, avg_velocity,
        whiff_pct, put_away_pct, hard_hit_pct_allowed, woba_allowed}],
        primary_pitch, source}
    """
    cache_key = f"arsenal_pitcher_{pitcher_id}_{year}"
    if _cache:
        cached = _cache.get(cache_key, ttl_hours=_CACHE_TTL_HOURS)
        if cached is not None:
            return cached

    result = _build_pitcher_arsenal(pitcher_id, year)
    if _cache:
        _cache.set(cache_key, result)
    return result


def get_batter_vs_pitch_type(batter_id: int, year: int) -> dict:
    """Return batter performance split by pitch type.

    Returns:
        {available, splits: [{pitch_type, label, pa, avg_exit_velo_pct,
        hard_hit_pct, whiff_pct, ba, woba}], source}
    """
    cache_key = f"arsenal_batter_{batter_id}_{year}"
    if _cache:
        cached = _cache.get(cache_key, ttl_hours=_CACHE_TTL_HOURS)
        if cached is not None:
            return cached

    result = _build_batter_splits(batter_id, year)
    if _cache:
        _cache.set(cache_key, result)
    return result


# ── Leaderboard fetching (cached per year) ────────────────────────────────────

def _get_pitcher_arsenal_stats(year: int) -> list[dict]:
    cache_key = f"lb_arsenal_stats_pitcher_{year}"
    if _cache:
        cached = _cache.get(cache_key, ttl_hours=_CACHE_TTL_HOURS)
        if cached is not None:
            return cached

    url = f"{_SAVANT_BASE}/leaderboard/pitch-arsenal-stats"
    params = {"type": "pitcher", "pitchType": "", "year": year, "team": "", "min": 10, "csv": "true"}
    rows = _fetch_csv(url, params)
    if _cache:
        _cache.set(cache_key, rows)
    return rows


def _get_pitcher_avg_speed(year: int) -> list[dict]:
    cache_key = f"lb_arsenal_speed_{year}"
    if _cache:
        cached = _cache.get(cache_key, ttl_hours=_CACHE_TTL_HOURS)
        if cached is not None:
            return cached

    url = f"{_SAVANT_BASE}/leaderboard/pitch-arsenals"
    params = {"year": year, "min": 10, "type": "avg_speed", "hand": "", "csv": "true"}
    rows = _fetch_csv(url, params)
    if _cache:
        _cache.set(cache_key, rows)
    return rows


def _get_batter_arsenal_stats(year: int) -> list[dict]:
    cache_key = f"lb_arsenal_stats_batter_{year}"
    if _cache:
        cached = _cache.get(cache_key, ttl_hours=_CACHE_TTL_HOURS)
        if cached is not None:
            return cached

    url = f"{_SAVANT_BASE}/leaderboard/pitch-arsenal-stats"
    params = {"type": "batter", "pitchType": "", "year": year, "team": "", "min": 10, "csv": "true"}
    rows = _fetch_csv(url, params)
    if _cache:
        _cache.set(cache_key, rows)
    return rows


# ── Builders ──────────────────────────────────────────────────────────────────

def _build_pitcher_arsenal(pitcher_id: int, year: int) -> dict:
    empty = {"available": False, "pitches": [], "primary_pitch": None, "source": "Baseball Savant"}

    stats_rows   = _get_pitcher_arsenal_stats(year)
    speed_rows   = _get_pitcher_avg_speed(year)

    # Filter arsenal-stats by pitcher
    pid_str = str(pitcher_id)
    my_rows = [r for r in stats_rows if r.get("player_id") == pid_str]
    if not my_rows:
        return empty

    # Build speed lookup from wide-format speed row
    speed_map: dict[str, float | None] = {}
    my_speed = next((r for r in speed_rows if r.get("pitcher") == pid_str), None)
    if my_speed:
        for pt_lower in ("ff", "si", "fc", "sl", "ch", "cu", "fs", "kn", "st", "sv"):
            val = _flt(my_speed.get(f"{pt_lower}_avg_speed"))
            if val:
                speed_map[pt_lower.upper()] = val

    pitches = []
    for r in my_rows:
        pt        = r.get("pitch_type", "").strip()
        pt_name   = r.get("pitch_name", "").strip() or pt
        usage     = _flt(r.get("pitch_usage"))
        pitches_n = _int(r.get("pitches"))
        if not pt or not usage or pitches_n < 5:
            continue
        pitches.append({
            "pitch_type":       pt,
            "label":            pt_name,
            "usage_pct":        round(usage, 1),
            "avg_velocity":     speed_map.get(pt),
            "whiff_pct":        _flt(r.get("whiff_percent")),
            "put_away_pct":     _flt(r.get("put_away")),
            "hard_hit_pct_allowed": _flt(r.get("hard_hit_percent")),
            "woba_allowed":     _flt(r.get("woba")),
        })

    if not pitches:
        return empty

    pitches.sort(key=lambda x: x["usage_pct"], reverse=True)
    return {
        "available":     True,
        "pitches":       pitches,
        "primary_pitch": pitches[0]["label"],
        "source":        "Baseball Savant",
    }


def _build_batter_splits(batter_id: int, year: int) -> dict:
    empty = {"available": False, "splits": [], "source": "Baseball Savant"}

    stats_rows = _get_batter_arsenal_stats(year)
    pid_str = str(batter_id)
    my_rows = [r for r in stats_rows if r.get("player_id") == pid_str]
    if not my_rows:
        return empty

    splits = []
    for r in my_rows:
        pt      = r.get("pitch_type", "").strip()
        pt_name = r.get("pitch_name", "").strip() or pt
        pa      = _int(r.get("pa"))
        if not pt or pa < 5:
            continue
        splits.append({
            "pitch_type":    pt,
            "label":         pt_name,
            "pa":            pa,
            "hard_hit_pct":  _flt(r.get("hard_hit_percent")),
            "whiff_pct":     _flt(r.get("whiff_percent")),
            "ba":            _flt(r.get("ba")),
            "slg":           _flt(r.get("slg")),
            "woba":          _flt(r.get("woba")),
            "est_woba":      _flt(r.get("est_woba")),
        })

    if not splits:
        return empty

    splits.sort(key=lambda x: x["pa"], reverse=True)
    return {"available": True, "splits": splits, "source": "Baseball Savant"}


# ── HTTP helper ───────────────────────────────────────────────────────────────

def _fetch_csv(url: str, params: dict) -> list[dict]:
    time.sleep(config.SAVANT_REQUEST_DELAY_SECONDS)
    try:
        resp = requests.get(url, params=params, headers=config.SAVANT_HEADERS, timeout=(5, 20))
        resp.raise_for_status()
        text = resp.text.strip()
        if not text or text.startswith("<"):
            return []
        clean = text.lstrip("﻿")
        reader = csv.DictReader(io.StringIO(clean))
        return list(reader)
    except Exception as exc:
        logger.warning("Pitch arsenal fetch failed (%s %s): %s", url, params, exc)
        return []


def _flt(val: Any) -> float | None:
    try:
        f = float(val)
        return f if f != 0.0 else None
    except (TypeError, ValueError):
        return None


def _int(val: Any) -> int:
    try:
        return int(val)
    except (TypeError, ValueError):
        return 0
