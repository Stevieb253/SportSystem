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
from data.pipeline import DataPipeline
from services import hit_probability, hr_probability
from services.model_builder import ModelBuilder
from services import historical_service
from services import best_bets as best_bets_service

logger = logging.getLogger(__name__)

bp = Blueprint("main", __name__)

# ── App-level singletons (created once, reused across all requests) ───────────
_cache    = Cache(config.CACHE_DIR, config.CACHE_TTL_HOURS)
_pipeline = DataPipeline(_cache)
_builder  = ModelBuilder(_pipeline, hit_probability, hr_probability)

# Wire odds API cache injection (manual refresh only — never auto-called)
from api import odds_api as _odds_api
_odds_api.set_cache(_cache)

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
        today = date.today().isoformat()
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

        # Persist the merged summary so index() can read it on next page load
        _cache.set(f"best_bets_odds_{today}", odds_by_player)

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
