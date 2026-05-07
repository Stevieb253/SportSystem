# api/statcast_api.py
# pybaseball wrapper — FanGraphs data and fallback Statcast.
# This is the FALLBACK when Baseball Savant scraping fails.

import logging
import time
from typing import Any

import pandas as pd

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import config

logger = logging.getLogger(__name__)

_cache = None


def set_cache(cache: Any) -> None:
    """Inject cache instance."""
    global _cache
    _cache = cache


def _import_pybaseball():
    """Lazy import pybaseball so app starts even if not installed."""
    try:
        import pybaseball as pb
        pb.cache.enable()
        return pb
    except ImportError:
        logger.warning("pybaseball not installed — FanGraphs fallback unavailable")
        return None


def get_season_batting_fangraphs(year: int, min_pa: int = 25) -> pd.DataFrame:
    """FanGraphs batting stats — disabled (site returns 403 for all automated requests).

    The app falls back to MLB Stats API + Baseball Savant for all batter data,
    so this layer is not needed. Keeping the function signature so call sites
    don't need to change; it just returns an empty DataFrame immediately.
    """
    logger.debug("FanGraphs batting skipped (blocked) — using MLB API / Savant fallback")
    return pd.DataFrame()


def get_season_pitching_fangraphs(year: int, min_ip: int = 5) -> pd.DataFrame:
    """FanGraphs pitching stats — disabled (site returns 403 for all automated requests).

    The app falls back to MLB Stats API + Baseball Savant for all pitcher data,
    so this layer is not needed. Keeping the function signature so call sites
    don't need to change; it just returns an empty DataFrame immediately.
    """
    logger.debug("FanGraphs pitching skipped (blocked) — using MLB API / Savant fallback")
    return pd.DataFrame()


def get_park_factors(year: int) -> pd.DataFrame:
    """Park factor data via pybaseball.

    pybaseball's park_factors() function requires a valid season year and
    returns a DataFrame indexed by team. Returns empty DataFrame on failure;
    the model falls back to a neutral factor of 100 for all parks.
    """
    cache_key = f"park_factors_{year}"
    if _cache:
        cached = _cache.get(cache_key)
        if cached is not None:
            return pd.DataFrame(cached)

    pb = _import_pybaseball()
    if pb is None:
        return pd.DataFrame()

    fn = getattr(pb, "park_factors", None)
    if fn is None:
        logger.debug("pybaseball.park_factors not available — using neutral 100")
        return pd.DataFrame()

    try:
        df = fn(year)
        if isinstance(df, pd.DataFrame) and not df.empty:
            if _cache:
                _cache.set(cache_key, df.to_dict(orient="records"))
            return df
    except Exception as exc:
        logger.debug("Park factors unavailable (year=%s): %s — using neutral 100", year, exc)

    if _cache:
        _cache.set(cache_key, [])  # cache the miss so we don't retry this session
    return pd.DataFrame()


def get_player_id(first_name: str, last_name: str) -> int | None:
    """Look up MLBAM player ID by name via pybaseball.

    Args:
        first_name: Player first name.
        last_name: Player last name.

    Returns:
        MLBAM player ID or None if not found.
    """
    pb = _import_pybaseball()
    if pb is None:
        return None
    try:
        result = pb.playerid_lookup(last_name, first_name)
        if result.empty:
            return None
        return int(result.iloc[0].get("key_mlbam", 0)) or None
    except Exception as exc:
        logger.warning("Player ID lookup failed (%s %s): %s", first_name, last_name, exc)
        return None


def get_statcast_batter_fallback(
    player_id: int,
    start: str,
    end: str,
) -> pd.DataFrame:
    """Fetch raw Statcast data for a batter via pybaseball (fallback).

    Args:
        player_id: MLBAM player ID.
        start: Start date YYYY-MM-DD.
        end: End date YYYY-MM-DD.

    Returns:
        DataFrame of Statcast records, empty on failure.
    """
    pb = _import_pybaseball()
    if pb is None:
        return pd.DataFrame()
    try:
        return pb.statcast_batter(start, end, player_id=player_id)
    except Exception as exc:
        logger.warning("Statcast batter fallback failed (id=%s): %s", player_id, exc)
        return pd.DataFrame()


def get_historical_batting(
    start_year: int,
    end_year: int,
    min_pa: int = 25,
) -> pd.DataFrame:
    """Fetch multi-year FanGraphs batting stats.

    Args:
        start_year: First season to include.
        end_year: Last season to include (inclusive).
        min_pa: Minimum PA qualifier.

    Returns:
        Combined multi-year DataFrame with Season column.
    """
    frames: list[pd.DataFrame] = []
    for year in range(start_year, end_year + 1):
        df = get_season_batting_fangraphs(year, min_pa)
        if not df.empty:
            df = df.copy()
            df["Season"] = year
            frames.append(df)
        time.sleep(0.3)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
