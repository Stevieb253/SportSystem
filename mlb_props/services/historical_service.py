# services/historical_service.py
# Historical data queries — no API calls, no Flask.

import logging
from typing import Any

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from models.player import CareerSeason

logger = logging.getLogger(__name__)

_AVAILABLE_STATS = {
    "HR", "AVG", "OPS", "WAR", "RBI",
    "xwOBA", "barrel_pct", "hard_hit_pct", "exit_velocity",
}


def get_player_career(
    player_name: str,
    pipeline: Any,
    start_year: int,
    end_year: int,
) -> list[CareerSeason]:
    """Fetch year-by-year career data for a player.

    Args:
        player_name: Full player name.
        pipeline: DataPipeline instance.
        start_year: First season.
        end_year: Last season (inclusive).

    Returns:
        List of CareerSeason sorted ascending by year.
    """
    return pipeline.load_historical_player(player_name, start_year, end_year)


def get_player_career_mlb(
    player_id: int,
    player_name: str,
) -> list[CareerSeason]:
    """Fetch year-by-year career hitting stats from the MLB Stats API.

    Uses the reliable /people/{id}/stats?stats=yearByYear endpoint which
    always works without scraping. Returns traditional stats only for
    historical seasons; current season Statcast data is added separately
    by the route from the daily model.

    Args:
        player_id: MLBAM player ID.
        player_name: Player's full name (used for logging only).

    Returns:
        List of CareerSeason sorted ascending by season year.
    """
    from api import mlb_api

    raw = mlb_api.get_player_career_stats(player_id)
    if not raw:
        logger.warning("No MLB career stats found for %s (id=%s)", player_name, player_id)
        return []

    seasons: list[CareerSeason] = []
    for s in raw:
        seasons.append(CareerSeason(
            season=s["season"],
            team=s["team"],
            games=s["games"],
            pa=s["pa"],
            avg=s["avg"],
            hr=s["hr"],
            rbi=s["rbi"],
            ops=s["ops"],
        ))
    return seasons


def get_all_time_leaders(
    stat: str,
    pipeline: Any,
    start_year: int,
    end_year: int,
    top_n: int = 25,
) -> list[dict]:
    """Return top-N players ranked by a cumulative or peak stat.

    Available stats: HR, AVG, OPS, WAR, RBI, xwOBA, barrel_pct,
    hard_hit_pct, exit_velocity.

    Args:
        stat: Stat name from the available set.
        pipeline: DataPipeline instance.
        start_year: First season to include.
        end_year: Last season to include.
        top_n: Number of results to return.

    Returns:
        List of dicts with rank, player, team, value — sorted descending.
    """
    from api import statcast_api
    import pandas as pd

    stat_col_map = {
        "HR":           "HR",
        "AVG":          "AVG",
        "OPS":          "OPS",
        "WAR":          "WAR",
        "RBI":          "RBI",
        "xwOBA":        "xwOBA",
        "barrel_pct":   "Barrel%",
        "hard_hit_pct": "Hard%",
        "exit_velocity": "EV",
    }

    col = stat_col_map.get(stat, "HR")
    frames = []
    for year in range(start_year, end_year + 1):
        df = statcast_api.get_season_batting_fangraphs(year, 25)
        if not df.empty:
            df = df.copy()
            df["_season"] = year
            frames.append(df)

    if not frames:
        return []

    combined = pd.concat(frames, ignore_index=True)
    if col not in combined.columns:
        logger.warning("Stat column '%s' not found in FanGraphs data", col)
        return []

    name_col = "Name" if "Name" in combined.columns else combined.columns[0]
    team_col = "Team" if "Team" in combined.columns else None

    agg_map = {col: "sum" if stat in ("HR", "RBI", "WAR") else "mean"}
    if team_col:
        agg_map[team_col] = "last"

    grouped = combined.groupby(name_col).agg(agg_map).reset_index()
    grouped = grouped.sort_values(col, ascending=False).head(top_n)

    results = []
    for rank, (_, row) in enumerate(grouped.iterrows(), start=1):
        results.append({
            "rank":   rank,
            "player": row.get(name_col, ""),
            "team":   str(row.get(team_col, "")) if team_col else "",
            "value":  round(float(row.get(col, 0.0)), 3),
        })
    return results


def compare_seasons(
    player_name: str,
    pipeline: Any,
    years: list[int],
) -> list[CareerSeason]:
    """Return CareerSeason entries for only the specified years.

    Args:
        player_name: Full player name.
        pipeline: DataPipeline instance.
        years: List of specific years to return.

    Returns:
        List of matching CareerSeason instances sorted ascending.
    """
    if not years:
        return []
    all_seasons = pipeline.load_historical_player(
        player_name, min(years), max(years)
    )
    year_set = set(years)
    return [s for s in all_seasons if s.season in year_set]
