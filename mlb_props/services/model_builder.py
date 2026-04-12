# services/model_builder.py
# Orchestrates the complete daily model build.
# No API calls. No Flask. Receives all dependencies via constructor.

import logging
from datetime import datetime
from types import ModuleType
from typing import Any

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from data.pipeline import DataPipeline
from models.probability import HitProbabilityResult, HRProbabilityResult

logger = logging.getLogger(__name__)

# Default park factors when data unavailable
_DEFAULT_HIT_FACTOR = 100.0
_DEFAULT_HR_FACTOR  = 100.0


class ModelBuilder:
    """Builds the full daily props model for a given date.

    Receives all collaborators via constructor (dependency injection).
    """

    def __init__(
        self,
        pipeline: DataPipeline,
        hit_service: ModuleType,
        hr_service: ModuleType,
    ) -> None:
        """Initialise ModelBuilder.

        Args:
            pipeline: DataPipeline instance for all data fetching.
            hit_service: hit_probability module.
            hr_service: hr_probability module.
        """
        self.pipeline    = pipeline
        self.hit_service = hit_service
        self.hr_service  = hr_service

    def build_daily_model(self, date_str: str) -> dict:
        """Build the full props model for a single date.

        Steps:
            1. Load season Savant data (primary)
            2. Load season FanGraphs data (fallback)
            3. Load park factors
            4. Load all games for the date
            5. For each confirmed lineup player, calculate hit + HR prob
            6. Sort and return results dict

        Args:
            date_str: Date string YYYY-MM-DD.

        Returns:
            Dict with date, games, hit_probabilities, hr_probabilities,
            top_hit_plays, top_hr_plays, data_sources, generated_at.
        """
        season = int(date_str[:4])
        data_sources: list[str] = []

        logger.info("Building model for %s", date_str)

        # ── Load season-level data ─────────────────────────────────────────
        savant_data = self.pipeline.load_season_savant_data(season)
        if savant_data:
            data_sources.append("Baseball Savant")

        fg_data = self.pipeline.load_season_fangraphs_data(season)
        if not fg_data.get("batting_df", [None] if True else []).empty if hasattr(
            fg_data.get("batting_df"), "empty"
        ) else fg_data.get("batting_df"):
            data_sources.append("FanGraphs")

        park_factors_df = self.pipeline.load_park_factors(season)

        # ── Load games ────────────────────────────────────────────────────
        games = self.pipeline.load_games_for_date(date_str)
        if not games:
            logger.warning("No games found for %s", date_str)

        hit_results: list[HitProbabilityResult] = []
        hr_results:  list[HRProbabilityResult]  = []

        for game in games:
            game_hit, game_hr = self._process_game(
                game, savant_data, fg_data, park_factors_df
            )
            hit_results.extend(game_hit)
            hr_results.extend(game_hr)

        # Sort descending by probability
        hit_results.sort(key=lambda r: r.hit_probability, reverse=True)
        hr_results.sort(key=lambda r: r.hr_probability, reverse=True)

        top_hit = [r for r in hit_results if r.hit_verdict == "YES"]
        top_hr  = [r for r in hr_results  if r.hr_verdict  == "YES"]

        return {
            "date":              date_str,
            "generated_at":      datetime.utcnow().isoformat(),
            "games":             games,
            "hit_probabilities": hit_results,
            "hr_probabilities":  hr_results,
            "top_hit_plays":     top_hit,
            "top_hr_plays":      top_hr,
            "data_sources":      data_sources,
            "lineups_confirmed": bool(hit_results),
        }

    def get_model_for_date(self, date_str: str) -> dict:
        """Return cached model if fresh, otherwise build and cache.

        Args:
            date_str: Date string YYYY-MM-DD.

        Returns:
            Full model dict.
        """
        cache_key = f"model_{date_str}"
        cached = self.pipeline.cache.get(cache_key)
        if cached is not None:
            logger.info("Returning cached model for %s", date_str)
            return cached

        model = self.build_daily_model(date_str)
        # Store serialisable version
        self.pipeline.cache.set(cache_key, _serialise_model(model))
        return model

    # ── Internal ──────────────────────────────────────────────────────────────

    def _process_game(
        self,
        game: Any,
        savant_data: dict,
        fg_data: dict,
        park_factors_df: Any,
    ) -> tuple[list, list]:
        """Calculate hit and HR probabilities for all players in one game."""
        hit_results: list[HitProbabilityResult] = []
        hr_results:  list[HRProbabilityResult]  = []

        park_hit_factor, park_hr_factor = _get_park_factors(
            game.venue.name, park_factors_df
        )

        # Load enriched pitchers
        home_pitcher = self.pipeline.load_pitcher_data(
            game.home_pitcher.id,
            game.home_pitcher.name,
            game.home_pitcher.hand,
            savant_data,
            fg_data,
        )
        away_pitcher = self.pipeline.load_pitcher_data(
            game.away_pitcher.id,
            game.away_pitcher.name,
            game.away_pitcher.hand,
            savant_data,
            fg_data,
        )

        # Home lineup faces away pitcher; away lineup faces home pitcher
        from api import mlb_api
        lineup_data = mlb_api.get_schedule(game.date)
        home_lineup, away_lineup = _extract_lineups(game.game_pk, lineup_data)

        for lineup, vs_pitcher in ((home_lineup, away_pitcher), (away_lineup, home_pitcher)):
            for pos, player_info in lineup:
                batter = self.pipeline.load_batter_data(
                    player_id=player_info.get("id", 0),
                    player_name=player_info.get("fullName", ""),
                    lineup_pos=pos,
                    team=player_info.get("team", ""),
                    hand=player_info.get("batSide", "R"),
                    vs_pitcher=vs_pitcher,
                    savant_season_data=savant_data,
                    fangraphs_data=fg_data,
                )

                hit_prob, hit_components = self.hit_service.calculate_hit_probability(
                    batter, vs_pitcher, park_hit_factor, game.weather
                )
                hit_verdict = self.hit_service.get_verdict(hit_prob)

                hr_prob, hr_components = self.hr_service.calculate_hr_probability(
                    batter, vs_pitcher, park_hr_factor, game.weather
                )
                hr_verdict = self.hr_service.get_verdict(hr_prob)

                hit_results.append(HitProbabilityResult(
                    player=batter,
                    game=game,
                    vs_pitcher=vs_pitcher,
                    hit_probability=hit_prob,
                    hit_verdict=hit_verdict,
                    component_scores=hit_components,
                ))
                hr_results.append(HRProbabilityResult(
                    player=batter,
                    game=game,
                    vs_pitcher=vs_pitcher,
                    hr_probability=hr_prob,
                    hr_verdict=hr_verdict,
                    component_scores=hr_components,
                ))

        return hit_results, hr_results


# ── Module-level helpers ──────────────────────────────────────────────────────

def _get_park_factors(venue_name: str, park_df: Any) -> tuple[float, float]:
    """Extract hit and HR park factors for a venue."""
    if park_df is None or (hasattr(park_df, "empty") and park_df.empty):
        return _DEFAULT_HIT_FACTOR, _DEFAULT_HR_FACTOR

    try:
        name_col = "Team" if "Team" in park_df.columns else park_df.columns[0]
        row = park_df[park_df[name_col].str.lower().str.contains(
            venue_name.lower().split()[0], na=False
        )]
        if row.empty:
            return _DEFAULT_HIT_FACTOR, _DEFAULT_HR_FACTOR

        r = row.iloc[0]
        hit_factor = float(r.get("1B", r.get("H", _DEFAULT_HIT_FACTOR)))
        hr_factor  = float(r.get("HR", _DEFAULT_HR_FACTOR))
        return hit_factor, hr_factor
    except Exception:
        return _DEFAULT_HIT_FACTOR, _DEFAULT_HR_FACTOR


def _extract_lineups(game_pk: int, schedule_data: list[dict]) -> tuple[list, list]:
    """Pull home and away lineup lists from raw schedule data."""
    home_lineup: list[tuple[int, dict]] = []
    away_lineup: list[tuple[int, dict]] = []

    for raw in schedule_data:
        if raw.get("gamePk") != game_pk:
            continue
        lineups = raw.get("lineups", {})
        home_players = lineups.get("homePlayers", [])
        away_players = lineups.get("awayPlayers", [])

        for i, p in enumerate(home_players[:9], start=1):
            home_lineup.append((i, p.get("person", p)))
        for i, p in enumerate(away_players[:9], start=1):
            away_lineup.append((i, p.get("person", p)))
        break

    return home_lineup, away_lineup


def _serialise_model(model: dict) -> dict:
    """Convert model dict to JSON-safe format (replaces dataclasses with dicts)."""
    import dataclasses

    def _convert(obj: Any) -> Any:
        if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
            return dataclasses.asdict(obj)
        if isinstance(obj, list):
            return [_convert(i) for i in obj]
        if isinstance(obj, dict):
            return {k: _convert(v) for k, v in obj.items()}
        if isinstance(obj, datetime):
            return obj.isoformat()
        return obj

    return _convert(model)
