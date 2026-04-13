# data/pipeline.py
# Orchestrates fetching and combining all data sources.
# All services get their data from pipeline — never from APIs directly.

import logging
from datetime import date

import pandas as pd

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from data.cache import Cache
from models.game import Game, ProbablePitcher
from models.player import BatterMetrics, CareerSeason

logger = logging.getLogger(__name__)


class DataPipeline:
    """Orchestrates multi-source data fetching for the model.

    Injects cache into each API module at construction time.
    All service layer code calls pipeline methods — never API modules directly.
    """

    def __init__(self, cache: Cache) -> None:
        """Initialise pipeline and inject cache into API modules.

        Args:
            cache: Shared Cache instance.
        """
        self.cache = cache
        self._season = date.today().year
        self._inject_cache()

    def _inject_cache(self) -> None:
        """Inject cache reference into all API modules."""
        from api import mlb_api, baseball_savant_api, statcast_api
        from api import espn_api, odds_api, weather_api
        mlb_api.set_cache(self.cache)
        baseball_savant_api.set_cache(self.cache)
        statcast_api.set_cache(self.cache)
        espn_api.set_cache(self.cache)
        odds_api.set_cache(self.cache)
        weather_api.set_cache(self.cache)

    # ── Season-level data ─────────────────────────────────────────────────────

    def load_season_savant_data(self, year: int) -> dict[int, dict]:
        """Fetch and merge all Baseball Savant leaderboards for a season.

        Primary data load. Attempts batters and pitchers from statcast,
        custom, and expected stats leaderboards, then merges by player_id.

        Args:
            year: Season year.

        Returns:
            Dict keyed by player_id with all merged Savant metrics.
        """
        cache_key = f"savant_season_{year}"
        cached = self.cache.get(cache_key)
        if cached is not None:
            return {int(k): v for k, v in cached.items()}

        from api import baseball_savant_api as sv_api

        batter_statcast  = sv_api.get_statcast_leaderboard(year, "batter")
        batter_custom    = sv_api.get_custom_leaderboard(year, "batter")
        batter_expected  = sv_api.get_expected_stats(year, "batter")   # ba, xba, woba, xwoba
        pitcher_statcast = sv_api.get_statcast_leaderboard(year, "pitcher")
        pitcher_custom   = sv_api.get_custom_leaderboard(year, "pitcher")
        pitcher_expected = sv_api.get_expected_stats(year, "pitcher")

        # Merge batter data in two passes: statcast+custom, then layer expected stats on top
        batter_merged = sv_api.merge_savant_data(batter_statcast, batter_custom)
        for row in batter_expected:
            from api.baseball_savant_api import _parse_player_id
            pid = _parse_player_id(row)
            if pid:
                if pid in batter_merged:
                    batter_merged[pid].update(row)
                else:
                    batter_merged[pid] = dict(row)

        # Merge pitcher data in two passes: statcast+custom first, then layer
        # expected stats (xERA, xwOBA) on top via a second update pass.
        pitcher_merged = sv_api.merge_savant_data(pitcher_statcast, pitcher_custom)
        for row in pitcher_expected:
            from api.baseball_savant_api import _parse_player_id
            pid = _parse_player_id(row)
            if pid:
                if pid in pitcher_merged:
                    pitcher_merged[pid].update(row)
                else:
                    pitcher_merged[pid] = dict(row)

        combined = {**batter_merged, **pitcher_merged}
        if combined:
            self.cache.set(cache_key, {str(k): v for k, v in combined.items()})
        logger.info(
            "Savant season data loaded: %d batters (+%d expected), %d pitchers",
            len(batter_merged), len(batter_expected), len(pitcher_merged),
        )
        return combined

    def load_season_fangraphs_data(self, year: int) -> dict:
        """Fetch FanGraphs batting and pitching DataFrames (fallback).

        Args:
            year: Season year.

        Returns:
            Dict with 'batting_df' and 'pitching_df' keys.
        """
        cache_key = f"fangraphs_season_{year}"
        cached = self.cache.get(cache_key)
        if cached is not None:
            return {
                "batting_df":  pd.DataFrame(cached.get("batting", [])),
                "pitching_df": pd.DataFrame(cached.get("pitching", [])),
            }

        from api import statcast_api
        batting_df  = statcast_api.get_season_batting_fangraphs(year, 25)
        pitching_df = statcast_api.get_season_pitching_fangraphs(year, 5)

        if not batting_df.empty or not pitching_df.empty:
            self.cache.set(cache_key, {
                "batting":  batting_df.to_dict(orient="records") if not batting_df.empty else [],
                "pitching": pitching_df.to_dict(orient="records") if not pitching_df.empty else [],
            })
        return {"batting_df": batting_df, "pitching_df": pitching_df}

    def load_park_factors(self, year: int) -> pd.DataFrame:
        """Fetch park factor DataFrame.

        Args:
            year: Season year.

        Returns:
            Park factors DataFrame.
        """
        from api import statcast_api
        return statcast_api.get_park_factors(year)

    # ── Daily game data ───────────────────────────────────────────────────────

    def load_games_for_date(self, date_str: str) -> list[Game]:
        """Fetch and normalise all games scheduled for a date.

        Args:
            date_str: Date in YYYY-MM-DD format.

        Returns:
            List of Game dataclasses.
        """
        from api import mlb_api, weather_api
        from data import normalizer

        raw_games = mlb_api.get_schedule(date_str)
        games: list[Game] = []
        for raw in raw_games:
            venue_name = raw.get("venue", {}).get("name", "")
            weather = weather_api.get_stadium_weather(venue_name)
            try:
                game = normalizer.normalize_game(raw, weather)
                games.append(game)
            except Exception as exc:
                logger.warning("Failed to normalise game %s: %s", raw.get("gamePk"), exc)
        logger.info("Loaded %d games for %s", len(games), date_str)
        return games

    # ── Player-level data ─────────────────────────────────────────────────────

    def load_batter_data(
        self,
        player_id: int,
        player_name: str,
        lineup_pos: int,
        team: str,
        hand: str,
        vs_pitcher: ProbablePitcher,
        savant_season_data: dict,
        fangraphs_data: dict,
    ) -> BatterMetrics:
        """Build BatterMetrics for a single player.

        Args:
            player_id: MLBAM player ID.
            player_name: Display name.
            lineup_pos: Lineup position 1-9.
            team: Team abbreviation.
            hand: Batting hand R/L/S.
            vs_pitcher: Opposing pitcher.
            savant_season_data: Full season Savant dict.
            fangraphs_data: FanGraphs data dict from load_season_fangraphs_data.

        Returns:
            BatterMetrics dataclass.
        """
        from api import baseball_savant_api as sv_api
        from data import normalizer
        import config as cfg

        cache_key = f"batter_{player_id}_{date.today().isoformat()}"
        cached = self.cache.get(cache_key)
        if cached is not None:
            return BatterMetrics(**cached)

        savant_player = savant_season_data.get(player_id)

        # FanGraphs fallback row
        fg_row = None
        batting_df: pd.DataFrame = fangraphs_data.get("batting_df", pd.DataFrame())
        if not batting_df.empty:
            name_col = "Name" if "Name" in batting_df.columns else batting_df.columns[0]
            matches = batting_df[batting_df[name_col].str.lower() == player_name.lower()]
            if not matches.empty:
                fg_row = matches.iloc[0]

        # MLB Stats API fallback: always fetch when FanGraphs is unavailable (fg_row is None)
        # to populate homeRuns + gamesPlayed for hr_per_game; also fills core stats when
        # Savant is missing them entirely.
        _needs_core   = not savant_player or not any(
            savant_player.get(k) for k in ("ba", "batting_avg", "xba", "est_ba", "woba")
        )
        _needs_counts = fg_row is None  # FanGraphs blocked → HR/G counts unavailable
        if player_id and (_needs_core or _needs_counts):
            try:
                from api import mlb_api
                mlb_stats = mlb_api.get_player_stats(player_id, "hitting", self._season)
                if mlb_stats:
                    mlb_mapped: dict = {
                        "homeRuns":     mlb_stats.get("homeRuns", 0),
                        "gamesPlayed":  mlb_stats.get("gamesPlayed", 0),
                    }
                    if _needs_core:
                        mlb_mapped.update({
                            "ba":          mlb_stats.get("avg", 0),
                            "batting_avg": mlb_stats.get("avg", 0),
                            "obp":         mlb_stats.get("obp", 0),
                            "slg":         mlb_stats.get("slg", 0),
                            "ops":         mlb_stats.get("ops", 0),
                            "pa":          mlb_stats.get("plateAppearances", 0),
                            "strikeOuts":  mlb_stats.get("strikeOuts", 0),
                            "baseOnBalls": mlb_stats.get("baseOnBalls", 0),
                            "atBats":      mlb_stats.get("atBats", 0),
                        })
                        # Derive K% and BB% from raw counts
                        pa = int(mlb_stats.get("plateAppearances", 0) or 0)
                        if pa > 0:
                            mlb_mapped["k_percent"] = round(
                                int(mlb_stats.get("strikeOuts", 0) or 0) / pa, 4
                            )
                            mlb_mapped["bb_percent"] = round(
                                int(mlb_stats.get("baseOnBalls", 0) or 0) / pa, 4
                            )
                    savant_player = {**(savant_player or {}), **mlb_mapped}
                    logger.debug("MLB API stats loaded for batter %s (%s)", player_name, player_id)
            except Exception as exc:
                logger.warning("MLB API batter fallback failed (%s): %s", player_id, exc)

        # Recent form from Baseball Savant
        recent_records = sv_api.get_recent_statcast(
            player_id, cfg.RECENT_FORM_DAYS, "batter"
        )

        metrics = normalizer.normalize_batter(
            player_id=player_id,
            player_name=player_name,
            lineup_pos=lineup_pos,
            team=team,
            hand=hand,
            savant_data=savant_player,
            fangraphs_row=fg_row,
            recent_savant_records=recent_records,
            vs_pitcher=vs_pitcher,
            season=self._season,
        )

        return metrics

    def load_pitcher_data(
        self,
        pitcher_id: int,
        pitcher_name: str,
        hand: str,
        savant_season_data: dict,
        fangraphs_data: dict,
    ) -> ProbablePitcher:
        """Build ProbablePitcher with full Savant/FanGraphs metrics.

        Args:
            pitcher_id: MLBAM pitcher ID.
            pitcher_name: Display name.
            hand: Pitching hand R or L.
            savant_season_data: Full season Savant dict.
            fangraphs_data: FanGraphs data dict.

        Returns:
            ProbablePitcher with enriched stats.
        """
        from data import normalizer

        savant_pitcher = savant_season_data.get(pitcher_id)

        fg_row = None
        pitching_df: pd.DataFrame = fangraphs_data.get("pitching_df", pd.DataFrame())
        if not pitching_df.empty:
            name_col = "Name" if "Name" in pitching_df.columns else pitching_df.columns[0]
            matches = pitching_df[pitching_df[name_col].str.lower() == pitcher_name.lower()]
            if not matches.empty:
                fg_row = matches.iloc[0]

        # MLB Stats API supplement: Savant leaderboard doesn't include WHIP, K/9, BB/9, HR/9.
        # Fetch from MLB Stats API whenever FanGraphs is unavailable (fg_row is None)
        # OR when the saved pitcher data is missing these traditional fields.
        _sv_whip = float(savant_pitcher.get("whip", 0) or 0) if savant_pitcher else 0
        _sv_era  = float(savant_pitcher.get("era", 0) or savant_pitcher.get("p_era", 0) or 0) if savant_pitcher else 0
        _need_mlb_supplement = pitcher_id and (fg_row is None or _sv_whip == 0)
        if _need_mlb_supplement:
            try:
                from api import mlb_api
                mlb_stats = mlb_api.get_player_stats(pitcher_id, "pitching", self._season)
                if mlb_stats:
                    # Map MLB API stat keys → Savant-compatible keys used by normalizer
                    mlb_mapped: dict = {
                        "era":           mlb_stats.get("era", 0),
                        "p_era":         mlb_stats.get("era", 0),
                        "whip":          mlb_stats.get("whip", 0),
                        "strikeOuts":    mlb_stats.get("strikeOuts", 0),
                        "baseOnBalls":   mlb_stats.get("baseOnBalls", 0),
                        "homeRuns":      mlb_stats.get("homeRuns", 0),
                        "inningsPitched": mlb_stats.get("inningsPitched", 0),
                    }
                    # Derive K/9, BB/9, HR/9 and K%/BB% from raw counts
                    try:
                        # MLB API returns innings like "34.2" meaning 34 full + 2 outs
                        ip_parts = str(mlb_stats.get("inningsPitched", "0")).split(".")
                        full_inn = int(ip_parts[0]) if ip_parts[0] else 0
                        partial = int(ip_parts[1]) if len(ip_parts) > 1 and ip_parts[1] else 0
                        true_ip = full_inn + partial / 3
                        ks  = int(mlb_stats.get("strikeOuts", 0) or 0)
                        bbs = int(mlb_stats.get("baseOnBalls", 0) or 0)
                        hrs = int(mlb_stats.get("homeRuns", 0) or 0)
                        bf  = int(mlb_stats.get("battersFaced", 0) or 0)
                        if true_ip > 0:
                            mlb_mapped["k9"]  = round(ks  * 9 / true_ip, 2)
                            mlb_mapped["bb9"] = round(bbs * 9 / true_ip, 2)
                            mlb_mapped["hr9"] = round(hrs * 9 / true_ip, 2)
                        if bf > 0:
                            # Store as 0-100 percentage so _svp() auto-converts to 0-1
                            mlb_mapped["k_percent"]  = round(ks  / bf * 100, 1)
                            mlb_mapped["bb_percent"] = round(bbs / bf * 100, 1)
                    except Exception:
                        pass
                    savant_pitcher = {**(savant_pitcher or {}), **mlb_mapped}
                    logger.debug("MLB API fallback stats loaded for pitcher %s (%s)", pitcher_name, pitcher_id)
            except Exception as exc:
                logger.warning("MLB API pitcher fallback failed (%s): %s", pitcher_id, exc)

        raw_pitcher = {"id": pitcher_id, "fullName": pitcher_name, "pitchHand": {"code": hand}}
        return normalizer.normalize_probable_pitcher(raw_pitcher, savant_pitcher, fg_row)

    def load_historical_player(
        self,
        player_name: str,
        start_year: int,
        end_year: int,
    ) -> list[CareerSeason]:
        """Fetch year-by-year career data for a player.

        Args:
            player_name: Player display name.
            start_year: First season.
            end_year: Last season (inclusive).

        Returns:
            List of CareerSeason sorted ascending by year.
        """
        from api import statcast_api

        seasons: list[CareerSeason] = []
        for year in range(start_year, end_year + 1):
            df = statcast_api.get_season_batting_fangraphs(year, 1)
            if df.empty:
                continue
            name_col = "Name" if "Name" in df.columns else df.columns[0]
            row = df[df[name_col].str.lower() == player_name.lower()]
            if row.empty:
                continue
            r = row.iloc[0]

            def _v(col: str, default: float = 0.0) -> float:
                try:
                    return float(r.get(col, default)) if hasattr(r, "get") else float(r[col])
                except Exception:
                    return default

            seasons.append(CareerSeason(
                season=year,
                team=str(r.get("Team", r.get("team", "")) if hasattr(r, "get") else r.get("Team", "")),
                games=int(_v("G")),
                pa=int(_v("PA")),
                avg=_v("AVG"),
                hr=int(_v("HR")),
                rbi=int(_v("RBI")),
                ops=_v("OPS"),
                woba=_v("wOBA"),
                xba=_v("xBA"),
                xwoba=_v("xwOBA"),
                barrel_pct=_v("Barrel%") or _v("Barrel"),
                hard_hit_pct=_v("Hard%") or _v("HardHit%"),
                sweet_spot_pct=_v("SweetSpot%"),
                avg_exit_velo=_v("EV"),
                ev50=_v("EV50"),
                whiff_pct=_v("Whiff%"),
                k_pct=_v("K%"),
                bb_pct=_v("BB%"),
                war=_v("WAR"),
            ))

        return sorted(seasons, key=lambda s: s.season)
