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
    """Fetch FanGraphs season batting stats via pybaseball.

    Args:
        year: Season year.
        min_pa: Minimum plate appearances to qualify.

    Returns:
        DataFrame with batting stats, empty DataFrame on failure.
    """
    cache_key = f"fangraphs_batting_{year}_{min_pa}"
    if _cache:
        cached = _cache.get(cache_key)
        if cached is not None:
            return pd.DataFrame(cached)

    pb = _import_pybaseball()
    if pb is None:
        return pd.DataFrame()

    try:
        df = pb.batting_stats(year, qual=min_pa)
        if _cache and not df.empty:
            _cache.set(cache_key, df.to_dict(orient="records"))
        return df
    except Exception as exc:
        logger.warning("FanGraphs batting stats failed (year=%s): %s", year, exc)

    # Fallback to Baseball Reference via pybaseball
    # bref scraping is brittle — can fail with "list index out of range" on HTML changes.
    try:
        df = pb.batting_stats_bref(year)
        if not isinstance(df, pd.DataFrame):
            raise ValueError(f"bref returned non-DataFrame: {type(df)}")
        if df.empty:
            return pd.DataFrame()
        if _cache:
            _cache.set(cache_key, df.to_dict(orient="records"))
        return df
    except (IndexError, KeyError, ValueError, AttributeError) as exc2:
        # HTML structure changed or empty page — log and return empty, caller will use MLB API
        logger.warning(
            "Baseball Reference batting stats failed (year=%s) — falling back to MLB API: %s",
            year, exc2,
        )
        return pd.DataFrame()
    except Exception as exc2:
        logger.warning("Baseball Reference batting stats also failed (year=%s): %s", year, exc2)
        return pd.DataFrame()


def get_season_pitching_fangraphs(year: int, min_ip: int = 5) -> pd.DataFrame:
    """Fetch FanGraphs season pitching stats via pybaseball.

    Args:
        year: Season year.
        min_ip: Minimum innings pitched to qualify.

    Returns:
        DataFrame with pitching stats, empty DataFrame on failure.
    """
    cache_key = f"fangraphs_pitching_{year}_{min_ip}"
    if _cache:
        cached = _cache.get(cache_key)
        if cached is not None:
            return pd.DataFrame(cached)

    pb = _import_pybaseball()
    if pb is None:
        return pd.DataFrame()

    try:
        df = pb.pitching_stats(year, qual=min_ip)
        if _cache and not df.empty:
            _cache.set(cache_key, df.to_dict(orient="records"))
        return df
    except Exception as exc:
        logger.warning("FanGraphs pitching stats failed (year=%s): %s", year, exc)

    try:
        df = pb.pitching_stats_bref(year)
        if not isinstance(df, pd.DataFrame):
            raise ValueError(f"bref returned non-DataFrame: {type(df)}")
        if df.empty:
            return pd.DataFrame()
        if _cache:
            _cache.set(cache_key, df.to_dict(orient="records"))
        return df
    except (IndexError, KeyError, ValueError, AttributeError) as exc2:
        logger.warning(
            "Baseball Reference pitching stats failed (year=%s) — falling back to MLB API: %s",
            year, exc2,
        )
        return pd.DataFrame()
    except Exception as exc2:
        logger.warning("Baseball Reference pitching stats also failed (year=%s): %s", year, exc2)
        return pd.DataFrame()


def get_park_factors(year: int) -> pd.DataFrame:
    """Fetch park factor data via pybaseball.

    Args:
        year: Season year.

    Returns:
        DataFrame with park factors, empty DataFrame on failure.
    """
    cache_key = f"park_factors_{year}"
    if _cache:
        cached = _cache.get(cache_key)
        if cached is not None:
            return pd.DataFrame(cached)

    pb = _import_pybaseball()
    if pb is None:
        return pd.DataFrame()

    # Try multiple pybaseball park factor functions (API changed across versions)
    for fn_name in ("park_factors", "statcast_single_game"):
        fn = getattr(pb, fn_name, None)
        if fn is None:
            continue
        try:
            df = fn(year)
            if not df.empty and _cache:
                _cache.set(cache_key, df.to_dict(orient="records"))
            return df
        except Exception as exc:
            logger.warning("Park factors via %s failed (year=%s): %s", fn_name, year, exc)

    logger.warning("Park factors unavailable — using neutral 100 for all parks")
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
