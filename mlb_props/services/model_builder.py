# services/model_builder.py
# Orchestrates the complete daily model build.
# No API calls. No Flask. Receives all dependencies via constructor.

import concurrent.futures
import logging
import threading
import time
from datetime import datetime
from types import ModuleType
from typing import Any

# Per-date build locks — ensures only one thread builds a given date's model.
# Other threads that arrive during the build wait, then read from cache.
_date_locks: dict[str, threading.Lock] = {}
_date_locks_mutex = threading.Lock()


def _get_date_lock(date_str: str) -> threading.Lock:
    with _date_locks_mutex:
        if date_str not in _date_locks:
            _date_locks[date_str] = threading.Lock()
        return _date_locks[date_str]

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from data.pipeline import DataPipeline
from models.probability import (
    HitProbabilityResult, HRProbabilityResult,
    LINEUP_OFFICIAL, LINEUP_PROBABLE_RECENT, LINEUP_PROBABLE_ROSTER,
)

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
        prediction_logger=None,
    ) -> None:
        """Initialise ModelBuilder.

        Args:
            pipeline: DataPipeline instance for all data fetching.
            hit_service: hit_probability module.
            hr_service: hr_probability module.
            prediction_logger: Optional PredictionLogger; if None, logging is skipped.
        """
        self.pipeline           = pipeline
        self.hit_service        = hit_service
        self.hr_service         = hr_service
        self.prediction_logger  = prediction_logger

    def build_daily_model(self, date_str: str, batch_timeout_seconds: int = 600) -> dict:
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
            batch_timeout_seconds: How long to wait for all game futures.
                600s (default) captures virtually every slate.
                300s is safe for scheduled reliability with --fast flag.

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

        def _build_game(game):
            """Enrich pitchers and compute all player probabilities for one game."""
            t0 = time.time()
            game.home_pitcher = self.pipeline.load_pitcher_data(
                game.home_pitcher.id, game.home_pitcher.name, game.home_pitcher.hand,
                savant_data, fg_data,
            )
            game.away_pitcher = self.pipeline.load_pitcher_data(
                game.away_pitcher.id, game.away_pitcher.name, game.away_pitcher.hand,
                savant_data, fg_data,
            )
            result = self._process_game(game, savant_data, fg_data, park_factors_df)
            logger.info(
                "  [%s @ %s] complete in %.1fs",
                game.away_team.abbreviation, game.home_team.abbreviation,
                time.time() - t0,
            )
            return result

        # Process all games concurrently — each game is independent.
        # max_workers capped at number of games to avoid spawning idle threads.
        max_workers = min(len(games), 10)
        if max_workers > 1:
            batch_start = time.time()
            executor = concurrent.futures.ThreadPoolExecutor(max_workers=max_workers)
            try:
                futures = {executor.submit(_build_game, game): game for game in games}
                # wait() with a hard cap so stalled games don't block the whole build.
                # Default 600s: captures virtually all real-world slates.
                # --fast mode uses 300s (scheduler-safe).
                done, not_done = concurrent.futures.wait(futures, timeout=batch_timeout_seconds)
                if not_done:
                    game_labels = [
                        f"{g.away_team.abbreviation}@{g.home_team.abbreviation}"
                        for g in (futures[f] for f in not_done)
                    ]
                    logger.warning(
                        "  %d game(s) timed out after %ds — abandoning: %s",
                        len(not_done), batch_timeout_seconds, ", ".join(game_labels),
                    )
                for future in done:
                    try:
                        game_hit, game_hr = future.result()
                        hit_results.extend(game_hit)
                        hr_results.extend(game_hr)
                    except Exception as exc:
                        logger.warning("Game build failed: %s", exc)
                logger.info(
                    "  Game batch: %d/%d completed in %.1fs",
                    len(done), len(futures), time.time() - batch_start,
                )
                if len(done) == 0 and len(futures) > 0:
                    logger.critical(
                        "  ALL %d game futures timed out — model will have 0 player projections. "
                        "Check circuit breaker status and API call latency.",
                        len(futures),
                    )
            finally:
                executor.shutdown(wait=False, cancel_futures=True)
        else:
            for game in games:
                game_hit, game_hr = _build_game(game)
                hit_results.extend(game_hit)
                hr_results.extend(game_hr)

        # Sort descending by probability (stable sort preserves game/lineup insertion order
        # as natural tiebreaker — players from the same game stay grouped together).
        hit_results.sort(key=lambda r: r.hit_probability, reverse=True)
        hr_results.sort(key=lambda r: r.hr_probability,  reverse=True)

        # Reassign hit verdicts by percentile rank within today's full player pool.
        # HR verdicts keep absolute thresholds until Phase 3 adds enough HR training data.
        self.hit_service.assign_percentile_verdicts(hit_results)

        top_hit = [r for r in hit_results if r.hit_verdict == "YES"]
        top_hr  = [r for r in hr_results  if r.hr_verdict  == "YES"]

        # Determine overall lineup mode for the banner message
        statuses = {r.lineup_status for r in hit_results}
        if not statuses:
            lineup_mode = "none"
        elif statuses == {LINEUP_OFFICIAL}:
            lineup_mode = "official"
        elif LINEUP_OFFICIAL in statuses:
            lineup_mode = "mixed"
        else:
            lineup_mode = "probable"

        # Build-quality metadata so callers can detect incomplete builds.
        hit_player_count = len(hit_results)
        hr_player_count  = len(hr_results)
        games_count      = len(games)
        if games_count > 0 and hit_player_count == 0:
            build_status = "failed"
        elif hit_player_count > 0:
            built_pks = {r.game.game_pk for r in hit_results if r.game is not None}
            build_status = "complete" if len(built_pks) >= games_count else "partial"
        else:
            build_status = "complete"  # off-day — no games, no players expected

        return {
            "date":                   date_str,
            "generated_at":           datetime.utcnow().isoformat(),
            "games":                  games,
            "hit_probabilities":      hit_results,
            "hr_probabilities":       hr_results,
            "top_hit_plays":          top_hit,
            "top_hr_plays":           top_hr,
            "data_sources":           data_sources,
            "lineups_confirmed":      lineup_mode == "official",
            "lineup_mode":            lineup_mode,
            # Validation fields — checked before caching and on cache reads.
            "build_status":           build_status,
            "games_count":            games_count,
            "hit_player_count":       hit_player_count,
            "hr_player_count":        hr_player_count,
            "total_projection_count": hit_player_count + hr_player_count,
        }

    def get_model_for_date(self, date_str: str, batch_timeout_seconds: int = 600) -> dict:
        """Return cached model if fresh, otherwise build and cache.

        Uses a per-date lock so that if multiple requests arrive simultaneously
        for an uncached date, only the first one builds the model — the rest
        wait and then read from cache instead of each building independently.

        TTL policy:
          - Complete builds today: 2-hour TTL (refreshes when new lineups post)
          - Partial builds today:  30-minute TTL (retries sooner for full build)
          - Past / future dates:   12-hour default

        A model with 0 player projections when games are scheduled is treated as
        invalid — it is never cached and never returned from cache.

        Args:
            date_str: Date string YYYY-MM-DD.
            batch_timeout_seconds: Forwarded to build_daily_model.

        Returns:
            Full model dict.
        """
        from datetime import date as _date
        is_today = date_str == _date.today().isoformat()
        cache_key = f"model_{date_str}"

        # Fast path — cached and still fresh.
        # Apply a 30-min effective TTL to partial builds so they're replaced sooner.
        if is_today:
            cached = self.pipeline.cache.get(cache_key, ttl_hours=2.0)
            if cached is not None and cached.get("build_status") == "partial":
                if self.pipeline.cache.is_expired(cache_key, ttl_hours=0.5):
                    cached = None  # partial build is >30 min old — refresh
        else:
            cached = self.pipeline.cache.get(cache_key, ttl_hours=None)

        if cached is not None:
            if _is_valid_model(cached):
                logger.info(
                    "Returning cached model for %s (build_status=%s)",
                    date_str, cached.get("build_status", "unknown"),
                )
                return cached
            logger.warning(
                "Cached model for %s is invalid (build_status=%s, hits=%d, games=%d) — "
                "invalidating and rebuilding",
                date_str,
                cached.get("build_status", "unknown"),
                len(cached.get("hit_probabilities", [])),
                len(cached.get("games", [])),
            )
            self.pipeline.cache.invalidate(cache_key)

        # Slow path — acquire per-date lock so only one thread builds
        lock = _get_date_lock(date_str)
        with lock:
            # Check again after acquiring lock (another thread may have built it)
            if is_today:
                cached = self.pipeline.cache.get(cache_key, ttl_hours=2.0)
                if cached is not None and cached.get("build_status") == "partial":
                    if self.pipeline.cache.is_expired(cache_key, ttl_hours=0.5):
                        cached = None
            else:
                cached = self.pipeline.cache.get(cache_key, ttl_hours=None)

            if cached is not None and _is_valid_model(cached):
                logger.info("Returning cached model for %s (built by concurrent request)", date_str)
                return cached
            if cached is not None:
                self.pipeline.cache.invalidate(cache_key)

            model = self.build_daily_model(date_str, batch_timeout_seconds=batch_timeout_seconds)
            # Always serialise to plain dicts before caching and returning.
            serialised = _serialise_model(model)
            # Attach matchup notes to each result (uses only existing data — no new API calls).
            _attach_matchup_notes(serialised)

            # Only cache valid models — never store a build that produced 0 projections
            # when games were scheduled (all-timeout scenario).
            build_status = serialised.get("build_status", "unknown")
            if _is_valid_model(serialised):
                self.pipeline.cache.set(cache_key, serialised)
                if build_status == "partial":
                    logger.warning(
                        "Partial model cached for %s (%d/%d games) — "
                        "will auto-refresh in 30 min; run again for complete build",
                        date_str,
                        serialised.get("hit_player_count", 0),
                        serialised.get("games_count", 0),
                    )
            else:
                logger.critical(
                    "Model for %s has build_status=%s with %d hits from %d games — "
                    "NOT caching to prevent serving stale empty data on next request",
                    date_str,
                    build_status,
                    len(serialised.get("hit_probabilities", [])),
                    len(serialised.get("games", [])),
                )

            # Log predictions for ML training — failure must never affect app output.
            if self.prediction_logger is not None:
                try:
                    self.prediction_logger.log_from_model(serialised, date_str)
                except Exception as _log_exc:
                    logger.warning("Prediction logging failed (non-fatal): %s", _log_exc)
            return serialised

    def invalidate_date(self, date_str: str) -> None:
        """Force-clear cached model and schedule for a specific date.

        Call this to immediately pick up newly confirmed lineups without
        waiting for the TTL to expire.

        Args:
            date_str: Date string YYYY-MM-DD.
        """
        self.pipeline.cache.invalidate(f"model_{date_str}")
        self.pipeline.cache.invalidate(f"mlb_schedule_{date_str}")
        logger.info("Cache invalidated for %s — next request will rebuild", date_str)

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

        # Use the already-enriched pitchers from the game object (set in build_daily_model)
        home_pitcher = game.home_pitcher
        away_pitcher = game.away_pitcher

        from api import mlb_api

        # ── Resolve lineups (three-tier) ──────────────────────────────────────
        t_lineup = time.time()
        lineup_data = mlb_api.get_schedule(game.date)
        home_official, away_official = _extract_official_lineups(game.game_pk, lineup_data)

        home_lineup, home_status = _resolve_lineup(
            official=home_official,
            team_id=game.home_team.id,
            team_abbr=game.home_team.abbreviation,
            game_date=game.date,
            savant_data=savant_data,
        )
        away_lineup, away_status = _resolve_lineup(
            official=away_official,
            team_id=game.away_team.id,
            team_abbr=game.away_team.abbreviation,
            game_date=game.date,
            savant_data=savant_data,
        )

        logger.info(
            "  [%s @ %s] lineups resolved in %.1fs — home: %s (%d), away: %s (%d)",
            game.away_team.abbreviation, game.home_team.abbreviation,
            time.time() - t_lineup,
            home_status, len(home_lineup),
            away_status, len(away_lineup),
        )

        # Pre-fetch bat sides for all players in both lineups in ONE batch API call.
        # This avoids 18 sequential HTTP requests per game (one per player).
        all_player_ids = [
            p.get("id", 0)
            for lineup in (home_lineup, away_lineup)
            for _, p in lineup
            if p.get("id", 0)
        ]
        bat_sides: dict[int, str] = {}
        if all_player_ids:
            try:
                t_bat = time.time()
                bat_sides = mlb_api.get_players_bat_sides(all_player_ids)
                logger.debug(
                    "  [%s @ %s] bat-side batch (%d players) in %.1fs",
                    game.away_team.abbreviation, game.home_team.abbreviation,
                    len(all_player_ids), time.time() - t_bat,
                )
            except Exception as exc:
                logger.warning("Batch bat-side lookup failed: %s", exc)

        for lineup, lineup_status, vs_pitcher, team_abbr in (
            (home_lineup, home_status, away_pitcher, game.home_team.abbreviation),
            (away_lineup, away_status, home_pitcher, game.away_team.abbreviation),
        ):
            for pos, player_info in lineup:
                pid      = player_info.get("id", 0)
                pname    = player_info.get("fullName", "")
                # Use batch-fetched bat side; fall back to lineup dict extraction
                phand    = bat_sides.get(pid) or _extract_bat_side(player_info)

                if not pid or not pname:
                    continue

                batter = self.pipeline.load_batter_data(
                    player_id=pid,
                    player_name=pname,
                    lineup_pos=pos,
                    team=team_abbr,
                    hand=phand,
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
                    lineup_status=lineup_status,
                ))
                hr_results.append(HRProbabilityResult(
                    player=batter,
                    game=game,
                    vs_pitcher=vs_pitcher,
                    hr_probability=hr_prob,
                    hr_verdict=hr_verdict,
                    component_scores=hr_components,
                    lineup_status=lineup_status,
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


def _extract_official_lineups(
    game_pk: int,
    schedule_data: list[dict],
) -> tuple[list[tuple[int, dict]], list[tuple[int, dict]]]:
    """Extract official home/away lineups from raw schedule data.

    Returns (home_lineup, away_lineup) where each is a list of (pos, player_dict).
    Both lists are empty when official lineups have not been posted yet.
    """
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


def _resolve_lineup(
    official: list[tuple[int, dict]],
    team_id: int,
    team_abbr: str,
    game_date: str,
    savant_data: dict,
) -> tuple[list[tuple[int, dict]], str]:
    """Resolve the best available lineup for a team using a three-tier fallback.

    Tier 1: Official same-day lineup from schedule hydration (best).
    Tier 2: Most recent completed game's batting order from boxscore.
    Tier 3: Active roster position players sorted by Savant PA (weakest).

    Args:
        official: Pre-extracted official lineup (may be empty).
        team_id:  MLB team ID.
        team_abbr: Team abbreviation e.g. 'NYY'.
        game_date: YYYY-MM-DD of the game being modelled.
        savant_data: Season Savant dict keyed by player_id.

    Returns:
        (lineup, status_string) — lineup is list of (batting_pos, player_dict).
    """
    # Tier 1 — official lineup already confirmed
    if official:
        logger.info("%s: CONFIRMED lineup (%d players)", team_abbr, len(official))
        return official, LINEUP_OFFICIAL

    from api import mlb_api
    from datetime import date as _date

    season = int(game_date[:4])

    # Tier 2 — most recent completed game's batting order from boxscore
    try:
        t2_start = time.time()
        bs_lineup, src_date = mlb_api.get_recent_boxscore_lineup(team_id, game_date)
        t2_elapsed = time.time() - t2_start
        if bs_lineup:
            for p in bs_lineup:
                p.setdefault("team", team_abbr)
            logger.info(
                "%s: using recent boxscore lineup from %s (%d players) [%.1fs]",
                team_abbr, src_date, len(bs_lineup), t2_elapsed,
            )
            return [(i + 1, p) for i, p in enumerate(bs_lineup[:9])], LINEUP_PROBABLE_RECENT
        else:
            logger.info(
                "%s: live lineup unavailable after %.1fs — falling back to roster",
                team_abbr, t2_elapsed,
            )
    except Exception as exc:
        logger.warning("Boxscore lineup fallback failed (%s): %s", team_abbr, exc)

    # Tier 3 — active roster sorted by Savant PA (most active position players first)
    try:
        roster = mlb_api.get_team_roster(team_id, season)
        if roster:
            # Sort by Savant plate appearances descending so starters come first
            def _pa(player: dict) -> int:
                sv = savant_data.get(player["id"], {})
                try:
                    return int(sv.get("pa") or sv.get("plateAppearances") or 0)
                except (TypeError, ValueError):
                    return 0

            roster_sorted = sorted(roster, key=_pa, reverse=True)[:9]
            for p in roster_sorted:
                p.setdefault("team", team_abbr)
            logger.info(
                "%s: using roster fallback lineup (%d players)",
                team_abbr, len(roster_sorted),
            )
            return [(i + 1, p) for i, p in enumerate(roster_sorted)], LINEUP_PROBABLE_ROSTER
    except Exception as exc:
        logger.warning("Roster lineup fallback failed (%s): %s", team_abbr, exc)

    # Nothing available
    return [], "none"


def _extract_bat_side(player_info: dict) -> str:
    """Extract batting handedness from a player info dict.

    Handles both nested {'code': 'R'} format and plain string format.
    """
    bat_side = player_info.get("batSide", player_info.get("hand", "R"))
    if isinstance(bat_side, dict):
        return bat_side.get("code", "R")
    return str(bat_side) if bat_side else "R"


def _attach_matchup_notes(serialised: dict) -> None:
    """In-place: append matchup_notes list to every hit/HR probability result.

    Called after serialisation so every value is a plain Python type.
    Silently skips individual results if note generation fails — the page
    still renders cleanly, just without notes for that batter.
    """
    try:
        from services.matchup_notes import generate_hit_notes, generate_hr_notes
    except Exception:
        try:
            from matchup_notes import generate_hit_notes, generate_hr_notes
        except Exception:
            logger.debug("matchup_notes module not importable — skipping note generation")
            return

    for r in serialised.get("hit_probabilities") or []:
        if not isinstance(r, dict):
            continue
        try:
            r["matchup_notes"] = generate_hit_notes(r)
        except Exception as exc:
            logger.debug("Hit note generation failed for %s: %s", r.get("player", {}).get("name"), exc)
            r["matchup_notes"] = []

    for r in serialised.get("hr_probabilities") or []:
        if not isinstance(r, dict):
            continue
        try:
            r["matchup_notes"] = generate_hr_notes(r)
        except Exception as exc:
            logger.debug("HR note generation failed for %s: %s", r.get("player", {}).get("name"), exc)
            r["matchup_notes"] = []


def _is_valid_model(model: dict) -> bool:
    """Return False if the model has scheduled games but zero player projections.

    A valid model is either:
      - An off-day model (no games, no players — both zero is fine)
      - A build that produced at least some player projections
    An invalid model is one where games are scheduled but every game future
    timed out, leaving hit_probabilities empty.  We never cache or serve these.
    """
    if not isinstance(model, dict):
        return False
    if model.get("build_status") == "failed":
        return False
    games_count = len(model.get("games", []))
    hit_count   = len(model.get("hit_probabilities", []))
    if games_count > 0 and hit_count == 0:
        return False
    return True


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
