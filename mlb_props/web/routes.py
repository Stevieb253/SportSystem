# web/routes.py
# All Flask routes. Business logic lives in services — not here.

import dataclasses
import logging
from datetime import date

from flask import Blueprint, jsonify, render_template, request

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import config
from data.cache import Cache
from data.logger import PredictionLogger
from data.pipeline import DataPipeline
from services import hit_probability, hr_probability
from services.model_builder import ModelBuilder
from services import historical_service
from services import best_bets as best_bets_service
from services import odds_service
from services import prop_context_service
from services import gemini_service
from services import game_context_service

logger = logging.getLogger(__name__)

bp = Blueprint("main", __name__)

# ── App-level singletons (created once, reused across all requests) ───────────
_cache             = Cache(config.CACHE_DIR, config.CACHE_TTL_HOURS)
_pipeline          = DataPipeline(_cache)
_prediction_logger = PredictionLogger(config.PREDICTIONS_DB_PATH)
_builder           = ModelBuilder(_pipeline, hit_probability, hr_probability, _prediction_logger)

# Wire API cache injections (after _cache is created)
from api import odds_api as _odds_api
_odds_api.set_cache(_cache)
from api import bvp_api as _bvp_api
_bvp_api.set_cache(_cache)
from api import pitch_arsenal_api as _pitch_arsenal_api
_pitch_arsenal_api.set_cache(_cache)

# TTL for cached odds-by-player summary (mirrors odds_api.ODDS_CACHE_TTL_HOURS)
_ODDS_SUMMARY_TTL = 8.0


# ── Page routes ───────────────────────────────────────────────────────────────

@bp.route("/")
def index():
    """Today's full model."""
    today = date.today().isoformat()
    model = _safe_model(today)
    best_bets = _build_best_bets(model, today)
    return render_template(
        "index.html",
        model=model,
        selected_date=today,
        best_bets=best_bets,
        odds_key_configured=bool(config.ODDS_API_KEY),
    )


@bp.route("/date/<date_str>")
def index_date(date_str: str):
    """Model for any specific date."""
    model = _safe_model(date_str)
    best_bets = _build_best_bets(model, date_str)
    return render_template(
        "index.html",
        model=model,
        selected_date=date_str,
        best_bets=best_bets,
        odds_key_configured=bool(config.ODDS_API_KEY),
    )


@bp.route("/player/<player_name>")
def player(player_name: str):
    """Individual player career page.

    Looks up the player from today's model to get their MLBAM player_id,
    then fetches year-by-year career stats from the MLB Stats API (always
    reliable). Today's Statcast metrics (xBA, barrel%, etc.) are injected
    directly from the loaded model — no FanGraphs needed.
    """
    # Allow player_id to be passed as a query param (from autocomplete nav)
    player_id: int = request.args.get("id", 0, type=int)

    # If not in URL, find from today's model
    if not player_id:
        today_model = _safe_model(date.today().isoformat())
        for r in today_model.get("hit_probabilities", []):
            pdict = r.get("player", {}) if isinstance(r, dict) else {}
            if pdict.get("name", "").lower() == player_name.lower():
                player_id = int(pdict.get("player_id", 0) or 0)
                break

    # Get year-by-year career stats from MLB API (fast, always works)
    seasons = []
    if player_id:
        seasons = historical_service.get_player_career_mlb(player_id, player_name)

    # Pull today's Statcast BatterMetrics for the current-season highlights card
    batter_metrics: dict | None = None
    mlb_raw_seasons: list[dict] = []
    if player_id:
        from api import mlb_api as _mlb
        today_model = _safe_model(date.today().isoformat())
        for r in today_model.get("hit_probabilities", []):
            pdict = r.get("player", {}) if isinstance(r, dict) else {}
            if int(pdict.get("player_id", 0) or 0) == player_id:
                batter_metrics = pdict
                break
        mlb_raw_seasons = _mlb.get_player_career_stats(player_id)

    return render_template(
        "player.html",
        player_name=player_name,
        player_id=player_id,
        seasons=seasons,
        batter_metrics=batter_metrics,
        mlb_raw_seasons=mlb_raw_seasons,
    )


@bp.route("/historical")
def historical():
    """Historical data browser."""
    return render_template("historical.html")


@bp.route("/game/<int:game_pk>")
def game_detail(game_pk: int):
    """Individual game detail page."""
    today = date.today().isoformat()
    model = _safe_model(today)
    game = next((g for g in model.get("games", []) if _game_pk(g) == game_pk), None)
    hit_results = [
        r for r in model.get("hit_probabilities", [])
        if _result_game_pk(r) == game_pk
    ]
    hr_results = [
        r for r in model.get("hr_probabilities", [])
        if _result_game_pk(r) == game_pk
    ]
    return render_template(
        "game.html",
        game=game,
        hit_results=hit_results,
        hr_results=hr_results,
        selected_date=today,
        debug=config.DEBUG,
    )


# ── JSON API routes ───────────────────────────────────────────────────────────

@bp.route("/api/model")
def api_model():
    """Return today's model as JSON."""
    today = date.today().isoformat()
    model = _safe_model(today)
    return jsonify(_to_json(model))


@bp.route("/api/scores/<date_str>")
def api_scores(date_str: str):
    """Return fresh game scores for a date — short-TTL, bypasses the 24h model cache."""
    from api import mlb_api
    from data import normalizer
    import time

    # Use a 2-minute cache so scores update quickly without hammering the API
    cache_key = f"scores_{date_str}_{int(time.time() // 120)}"
    cached = _cache.get(cache_key)
    if cached is not None:
        return jsonify(cached)

    try:
        # Fetch fresh from MLB API with linescore hydration — bypass the model cache
        import config as cfg
        import requests as _req
        url = f"{cfg.MLB_API_BASE_URL}/schedule"
        params = {
            "date":    date_str,
            "sportId": 1,
            "hydrate": "linescore,team",
        }
        resp = _req.get(url, params=params, timeout=8)
        resp.raise_for_status()
        raw = resp.json()

        result = []
        for date_entry in raw.get("dates", []):
            for rg in date_entry.get("games", []):
                status_code = rg.get("status", {}).get("abstractGameState", "Preview")
                status_map  = {"Preview": "scheduled", "Live": "live", "Final": "final"}
                status = status_map.get(status_code, "scheduled")
                linescore = rg.get("linescore", {})
                result.append({
                    "game_pk":     rg.get("gamePk", 0),
                    "status":      status,
                    "away_score":  linescore.get("teams", {}).get("away", {}).get("runs", 0),
                    "home_score":  linescore.get("teams", {}).get("home", {}).get("runs", 0),
                    "inning":      linescore.get("currentInning", ""),
                    "inning_half": linescore.get("inningHalf", ""),
                })
        _cache.set(cache_key, result)
        return jsonify(result)
    except Exception as exc:
        logger.error("Scores fetch failed for %s: %s", date_str, exc)
        return jsonify([])


@bp.route("/api/players/search")
def api_players_search():
    """Return players matching a name query from today's model.

    Used by the nav search autocomplete. Searches hit_probabilities for
    players whose names contain the query string (case-insensitive).

    Query params:
        q: Search string (min 2 chars).

    Returns:
        JSON list of {name, team, player_id, position} dicts, max 10 results.
    """
    query = request.args.get("q", "").strip().lower()
    if len(query) < 2:
        return jsonify([])

    today = date.today().isoformat()
    model = _safe_model(today)

    seen: set[int] = set()
    results: list[dict] = []

    for r in model.get("hit_probabilities", []):
        pdict = r.get("player", {}) if isinstance(r, dict) else {}
        name  = pdict.get("name", "") or ""
        if query not in name.lower():
            continue
        pid = int(pdict.get("player_id", 0) or 0)
        if pid in seen:
            continue
        seen.add(pid)
        results.append({
            "name":      name,
            "team":      pdict.get("team", ""),
            "player_id": pid,
            "hand":      pdict.get("hand", ""),
        })
        if len(results) >= 10:
            break

    # Sort so exact-start matches rise to the top
    results.sort(key=lambda p: (not p["name"].lower().startswith(query), p["name"]))
    return jsonify(results)


@bp.route("/api/refresh/<date_str>", methods=["POST"])
def api_refresh(date_str: str):
    """Force-clear cached model and schedule for a date, then rebuild.

    Used by the Refresh button so users can pick up newly confirmed lineups
    without restarting the server or waiting for TTL to expire.
    """
    try:
        _builder.invalidate_date(date_str)
        model = _safe_model(date_str)
        lineup_mode = model.get("lineup_mode", "unknown")
        n_games = len(model.get("games", []))
        n_players = len(model.get("hit_probabilities", []))
        logger.info(
            "Manual refresh for %s: %d games, %d players, lineups=%s",
            date_str, n_games, n_players, lineup_mode,
        )
        return jsonify({
            "ok": True,
            "date": date_str,
            "games": n_games,
            "players": n_players,
            "lineup_mode": lineup_mode,
        })
    except Exception as exc:
        logger.error("Refresh failed for %s: %s", date_str, exc)
        return jsonify({"ok": False, "error": str(exc)}), 500


@bp.route("/api/bvp/<int:batter_id>/<int:pitcher_id>")
def api_bvp(batter_id: int, pitcher_id: int):
    """Return career batter-vs-pitcher stats from the MLB Stats API.

    Cached for 24 hours — the underlying career numbers never change within a season.
    Returns: {has_data, ab, hits, hr, k, bb, avg} or {has_data: false}.
    """
    cache_key = f"bvp_{batter_id}_{pitcher_id}"
    cached = _cache.get(cache_key)
    if cached is not None:
        return jsonify(cached)

    try:
        import requests as _req
        import config as cfg
        url = f"{cfg.MLB_API_BASE_URL}/people/{batter_id}/stats"
        params = {
            "stats":             "vsPlayer",
            "opposingPlayerId":  pitcher_id,
            "group":             "hitting",
        }
        resp = _req.get(url, params=params, timeout=8)
        resp.raise_for_status()
        raw = resp.json()

        result: dict = {"has_data": False}
        for stat_group in raw.get("stats", []):
            splits = stat_group.get("splits", [])
            if splits:
                s = splits[0].get("stat", {})
                result = {
                    "has_data": True,
                    "ab":       int(s.get("atBats",      0)),
                    "hits":     int(s.get("hits",         0)),
                    "hr":       int(s.get("homeRuns",     0)),
                    "k":        int(s.get("strikeOuts",   0)),
                    "bb":       int(s.get("baseOnBalls",  0)),
                    "avg":      s.get("avg", ".000"),
                }
                break

        _cache.set(cache_key, result)
        return jsonify(result)

    except Exception as exc:
        logger.error("BvP fetch failed %d vs %d: %s", batter_id, pitcher_id, exc)
        return jsonify({"has_data": False})


@bp.route("/api/prop/<int:player_id>/<prop_type>/context")
def api_prop_context(player_id: int, prop_type: str):
    """Return structured context JSON for a single player prop.

    Assembles batter season stats, pitcher stats, BvP history, weather,
    park factors, and odds into one dict.  Every field is either a real
    value or explicitly null + available=False — nothing is invented.

    Path params:
        player_id:  MLBAM batter ID.
        prop_type:  "hit" or "hr".

    Query params:
        date: YYYY-MM-DD  (defaults to today)

    Returns:
        {player, prop, batter_season, pitcher, bvp, context, data_availability}
    """
    if prop_type not in ("hit", "hr"):
        return jsonify({"error": "prop_type must be 'hit' or 'hr'"}), 400

    date_str = request.args.get("date") or date.today().isoformat()
    ctx, err_response = _build_prop_ctx(player_id, prop_type, date_str)
    if err_response is not None:
        return err_response
    return jsonify(ctx)


@bp.route("/api/prop/<int:player_id>/<prop_type>/explain")
def api_prop_explain(player_id: int, prop_type: str):
    """Return structured context + Gemini plain-English explanation for a player prop.

    Calls /context internally, then asks Gemini to explain the data.
    If Gemini is unavailable (missing creds, API error), still returns the full
    context with explanation.available=False and explanation.error set.

    Path params:
        player_id:  MLBAM batter ID.
        prop_type:  "hit" or "hr".

    Query params:
        date: YYYY-MM-DD  (defaults to today)

    Returns:
        Full context dict (same as /context) plus an "explanation" key.
    """
    if prop_type not in ("hit", "hr"):
        return jsonify({"error": "prop_type must be 'hit' or 'hr'"}), 400

    date_str = request.args.get("date") or date.today().isoformat()
    ctx, err_response = _build_prop_ctx(player_id, prop_type, date_str)
    if err_response is not None:
        return err_response

    gemini_cache_key = f"gemini_explain_{player_id}_{prop_type}_{date_str}_{gemini_service.PROP_ANALYSIS_CACHE_VERSION}"
    explanation = gemini_service.explain_prop(
        ctx,
        cache=_cache,
        cache_key=gemini_cache_key,
    )

    return jsonify({**ctx, "explanation": explanation})


@bp.route("/api/game/<int:game_pk>/explain")
def api_game_explain(game_pk: int):
    """Return an AI Game Breakdown (Gemini) for a single game.

    Builds game context from today's model data, then asks Gemini to produce
    a structured plain-English breakdown. Cached 2 hours per game+date.

    Query params:
        date:  YYYY-MM-DD  (defaults to today)
        force: 1           (bypass + invalidate cache, regenerate fresh)

    Returns:
        Dict with keys: summary, pitching_edge, offensive_edge,
        weather_park_impact, reasons_away_could_win, reasons_home_could_win,
        data_caveats, available, model, error, used_fallback_summary.
    """
    date_str = request.args.get("date") or date.today().isoformat()
    force    = request.args.get("force") == "1"
    if force:
        logger.info(
            "api_game_explain: force=1 received — cache will be bypassed  "
            "game_pk=%s  date=%s",
            game_pk, date_str,
        )
    model = _safe_model(date_str)

    game = next((g for g in model.get("games", []) if _game_pk(g) == game_pk), None)
    if game is None:
        return jsonify({"error": f"game {game_pk} not found for {date_str}"}), 404

    hit_results = [r for r in model.get("hit_probabilities", []) if _result_game_pk(r) == game_pk]
    hr_results  = [r for r in model.get("hr_probabilities",  []) if _result_game_pk(r) == game_pk]

    try:
        park_factors_df = _pipeline.load_park_factors(int(date_str[:4]))
    except Exception:
        park_factors_df = None

    from api import mlb_api as _mlb_api
    ctx = game_context_service.build_game_context(
        game            = game,
        hit_results     = hit_results,
        hr_results      = hr_results,
        park_factors_df = park_factors_df,
        mlb_api         = _mlb_api,
        date_str        = date_str,
    )

    gemini_cache_key = f"gemini_game_{game_pk}_{date_str}_{gemini_service.GAME_ANALYSIS_CACHE_VERSION}"
    breakdown = gemini_service.explain_game(
        ctx,
        cache=_cache,
        cache_key=gemini_cache_key,
        force=force,
    )

    return jsonify({**ctx, "breakdown": breakdown})


@bp.route("/api/live")
def api_live():
    """Return ESPN live scoreboard."""
    from api import espn_api
    data = espn_api.get_scoreboard()
    return jsonify(data)


@bp.route("/api/live/game/<int:game_pk>")
def api_live_game(game_pk: int):
    """Return live pitch log for a game."""
    from api import mlb_api
    feed = mlb_api.get_live_feed(game_pk)
    pitches = mlb_api.parse_live_pitches(feed)
    return jsonify({"game_pk": game_pk, "pitches": pitches})


@bp.route("/api/player/lookup")
def api_player_lookup():
    """Search for players by name using the MLB Stats API.

    Returns a list of matching players with player_id, name, team, position.
    Used by the historical page search and nav autocomplete fallback.
    """
    query = request.args.get("q", "").strip()
    if len(query) < 2:
        return jsonify([])
    from api import mlb_api as _mlb
    results = _mlb.search_players(query)
    return jsonify(results)


@bp.route("/api/historical")
def api_historical():
    """Return year-by-year career hitting stats for a player.

    Accepts either player_id (preferred) or player name (will search for id).
    Uses MLB Stats API — no FanGraphs dependency.
    """
    from api import mlb_api as _mlb

    player_id   = request.args.get("id", 0, type=int)
    player_name = request.args.get("player", "").strip()

    # If no id, search by name
    if not player_id and player_name:
        matches = _mlb.search_players(player_name)
        if matches:
            player_id = matches[0]["player_id"]

    if not player_id:
        return jsonify([])

    seasons = _mlb.get_player_career_stats(player_id)
    return jsonify(seasons)


@bp.route("/api/odds/status")
def api_odds_status():
    """Return current odds cache status and quota info (no API calls made)."""
    today = date.today().isoformat()
    cached_odds = _cache.get(f"best_bets_odds_{today}", ttl_hours=_ODDS_SUMMARY_TTL)
    quota = _odds_api.get_quota_info()
    return jsonify({
        "has_odds":       cached_odds is not None,
        "player_count":   len(cached_odds) if cached_odds else 0,
        "quota":          quota,
        "key_configured": quota["key_configured"],
    })


@bp.route("/api/odds/refresh", methods=["POST"])
def api_odds_refresh():
    """Manually trigger an odds fetch for today's players.

    Burns Odds API quota — only called when the user explicitly clicks
    "Refresh Odds".  Results are cached for 8 hours.
    """
    from api import odds_api as _mlb_odds

    if not _mlb_odds.get_quota_info()["key_configured"]:
        return jsonify({"ok": False, "error": "No ODDS_API_KEY configured in .env"}), 400

    try:
        body = request.get_json(silent=True) or {}
        today = body.get("date") or date.today().isoformat()
        model = _safe_model(today)

        # Collect all player names from today's model
        player_names: list[str] = []
        seen: set[str] = set()
        for r in model.get("hit_probabilities", []):
            name = (r.get("player", {}) if isinstance(r, dict) else {}).get("name", "")
            if name and name not in seen:
                player_names.append(name)
                seen.add(name)
        for r in model.get("hr_probabilities", []):
            name = (r.get("player", {}) if isinstance(r, dict) else {}).get("name", "")
            if name and name not in seen:
                player_names.append(name)
                seen.add(name)

        # Fetch all props (respects 8h cache — won't burn quota if already fresh)
        odds_by_player = _mlb_odds.fetch_all_props_for_today(player_names)

        logger.info(
            "Odds refresh: target_date=%s, players_in_model=%d, players_with_odds=%d",
            today, len(player_names), len(odds_by_player),
        )

        # Persist the merged summary so index() can read it on next page load
        _cache.set(f"best_bets_odds_{today}", odds_by_player)

        # Write odds + edge to SQLite training DB (non-fatal if it fails)
        _persist_odds_to_db(today, model, odds_by_player)

        quota = _mlb_odds.get_quota_info()
        logger.info(
            "Odds refresh: %d players, quota remaining=%s",
            len(odds_by_player), quota.get("remaining"),
        )
        return jsonify({
            "ok":           True,
            "player_count": len(odds_by_player),
            "quota":        quota,
        })

    except Exception as exc:
        logger.error("Odds refresh failed: %s", exc)
        return jsonify({"ok": False, "error": str(exc)}), 500


@bp.route("/api/leaders")
def api_leaders():
    """Return statistical leaders for a season using the MLB Stats API."""
    from api import mlb_api as _mlb

    # Map friendly stat names to MLB API leaderCategories
    stat_map = {
        "HR":      "homeRuns",
        "AVG":     "battingAverage",
        "OPS":     "onBasePlusSlugging",
        "RBI":     "runsBattedIn",
        "H":       "hits",
        "SB":      "stolenBases",
        "BB":      "walks",
        "OBP":     "onBasePercentage",
        "SLG":     "sluggingPercentage",
        "R":       "runs",
    }
    stat    = request.args.get("stat", "HR")
    season  = int(request.args.get("season", date.today().year))
    top_n   = int(request.args.get("top_n", 25))
    category = stat_map.get(stat, "homeRuns")
    leaders = _mlb.get_season_leaders(category, season, top_n)
    return jsonify(leaders)


@bp.route("/api/player/<player_name>/stats")
def api_player_stats(player_name: str):
    """Return full career stats for a player as JSON."""
    import config as cfg
    seasons = historical_service.get_player_career(
        player_name,
        _pipeline,
        cfg.STATCAST_START_YEAR,
        date.today().year,
    )
    return jsonify([dataclasses.asdict(s) for s in seasons])


# ── Helpers ───────────────────────────────────────────────────────────────────

def _persist_odds_to_db(date_str: str, model: dict, odds_by_player: dict) -> None:
    """Write sportsbook odds + edge to prediction rows in SQLite.

    Called after a manual odds refresh.  Non-fatal — a failure here must never
    affect the odds response returned to the browser.
    """
    try:
        updated = 0
        hit_matched = 0
        hit_skipped_no_odds = 0
        hit_skipped_no_implied = 0

        for r in model.get("hit_probabilities", []):
            if not isinstance(r, dict):
                continue
            name      = (r.get("player") or {}).get("name", "")
            odds_info = odds_by_player.get(name)
            if not odds_info:
                hit_skipped_no_odds += 1
                continue
            hit_matched += 1
            prob      = float(r.get("hit_probability", 0))
            edge_data = odds_service.enrich_with_edge(prob, odds_info)
            if edge_data["implied_probability"] is None:
                hit_skipped_no_implied += 1
                continue
            _prediction_logger.update_odds(
                date_str            = date_str,
                player_name         = name,
                prop_type           = "hit",
                sportsbook_odds     = edge_data["sportsbook_odds"] or "",
                sportsbook_line     = edge_data["sportsbook_line"] or 0.5,
                implied_probability = edge_data["implied_probability"],
                best_book           = edge_data["best_book"] or "",
                edge                = edge_data["edge"],
            )
            updated += 1

        logger.info(
            "_persist_odds hit: date=%s matched=%d skipped_no_odds=%d skipped_no_implied=%d written=%d",
            date_str, hit_matched, hit_skipped_no_odds, hit_skipped_no_implied, updated,
        )

        hr_start = updated
        for r in model.get("hr_probabilities", []):
            if not isinstance(r, dict):
                continue
            name      = (r.get("player") or {}).get("name", "")
            odds_info = odds_by_player.get(name + "_hr") or odds_by_player.get(name)
            if not odds_info:
                continue
            prob      = float(r.get("hr_probability", 0))
            edge_data = odds_service.enrich_with_edge(prob, odds_info)
            if edge_data["implied_probability"] is None:
                continue
            _prediction_logger.update_odds(
                date_str            = date_str,
                player_name         = name,
                prop_type           = "hr",
                sportsbook_odds     = edge_data["sportsbook_odds"] or "",
                sportsbook_line     = edge_data["sportsbook_line"] or 0.5,
                implied_probability = edge_data["implied_probability"],
                best_book           = edge_data["best_book"] or "",
                edge                = edge_data["edge"],
            )
            updated += 1

        logger.info(
            "_persist_odds hr: date=%s written=%d",
            date_str, updated - hr_start,
        )
        logger.info("Persisted odds to SQLite: %d rows updated for %s", updated, date_str)
    except Exception as exc:
        logger.warning("_persist_odds_to_db failed (non-fatal): %s", exc)


def _build_prop_ctx(
    player_id: int,
    prop_type: str,
    date_str: str,
) -> tuple:
    """Build prop context dict for routes that need it.

    Returns (ctx_dict, None) on success or (None, error_response) on failure.
    Shared by /context and /explain so the setup logic is not duplicated.
    """
    model = _safe_model(date_str)
    if not model.get("hit_probabilities") and not model.get("hr_probabilities"):
        return None, (jsonify({"error": f"model unavailable for {date_str}"}), 503)

    odds_by_player = _cache.get(
        f"best_bets_odds_{date_str}", ttl_hours=_ODDS_SUMMARY_TTL
    ) or {}

    try:
        park_factors_df = _pipeline.load_park_factors(int(date_str[:4]))
    except Exception:
        park_factors_df = None

    ctx = prop_context_service.build_prop_context(
        player_id         = player_id,
        prop_type         = prop_type,
        model             = model,
        bvp_api           = _bvp_api,
        odds_by_player    = odds_by_player,
        park_factors_df   = park_factors_df,
        pitch_arsenal_api = _pitch_arsenal_api,
    )

    if ctx is None:
        return None, (
            jsonify({"error": f"player {player_id} not found in {prop_type} model for {date_str}"}),
            404,
        )

    return ctx, None


def _build_best_bets(model: dict, date_str: str) -> dict:
    """Build best bets, reading cached odds if available (never fetching fresh)."""
    try:
        odds_by_player = _cache.get(f"best_bets_odds_{date_str}", ttl_hours=_ODDS_SUMMARY_TTL) or {}
        return best_bets_service.build_best_bets(
            hit_probabilities=model.get("hit_probabilities", []),
            hr_probabilities=model.get("hr_probabilities", []),
            odds_by_player=odds_by_player,
        )
    except Exception as exc:
        logger.error("Best bets build failed for %s: %s", date_str, exc)
        return {"hit_bets": [], "hr_bets": [], "parlays": [], "has_odds": False, "mode": "model_only"}


def _safe_model(date_str: str) -> dict:
    """Build or retrieve model, returning empty dict on error."""
    try:
        return _builder.get_model_for_date(date_str)
    except Exception as exc:
        logger.error("Model build failed for %s: %s", date_str, exc)
        return {
            "date": date_str,
            "games": [],
            "hit_probabilities": [],
            "hr_probabilities": [],
            "top_hit_plays": [],
            "top_hr_plays": [],
            "data_sources": [],
            "lineups_confirmed": False,
            "error": str(exc),
        }


def _game_pk(game) -> int:
    """Extract game_pk from Game dataclass or dict."""
    if dataclasses.is_dataclass(game):
        return game.game_pk
    return game.get("game_pk", 0)


def _result_game_pk(result) -> int:
    """Extract game_pk from probability result dataclass or dict."""
    if dataclasses.is_dataclass(result):
        return result.game.game_pk
    return result.get("game", {}).get("game_pk", 0)


def _to_json(obj):
    """Recursively convert dataclasses to dicts for JSON serialisation."""
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {k: _to_json(v) for k, v in dataclasses.asdict(obj).items()}
    if isinstance(obj, list):
        return [_to_json(i) for i in obj]
    if isinstance(obj, dict):
        return {k: _to_json(v) for k, v in obj.items()}
    return obj
