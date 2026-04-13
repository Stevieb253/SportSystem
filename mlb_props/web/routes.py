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

logger = logging.getLogger(__name__)

bp = Blueprint("main", __name__)

# ── App-level singletons (created once, reused across all requests) ───────────
_cache    = Cache(config.CACHE_DIR, config.CACHE_TTL_HOURS)
_pipeline = DataPipeline(_cache)
_builder  = ModelBuilder(_pipeline, hit_probability, hr_probability)


# ── Page routes ───────────────────────────────────────────────────────────────

@bp.route("/")
def index():
    """Today's full model."""
    today = date.today().isoformat()
    model = _safe_model(today)
    return render_template("index.html", model=model, selected_date=today)


@bp.route("/date/<date_str>")
def index_date(date_str: str):
    """Model for any specific date."""
    model = _safe_model(date_str)
    return render_template("index.html", model=model, selected_date=date_str)


@bp.route("/player/<player_name>")
def player(player_name: str):
    """Individual player career page."""
    import config as cfg
    seasons = historical_service.get_player_career(
        player_name,
        _pipeline,
        cfg.STATCAST_START_YEAR,
        date.today().year,
    )
    return render_template("player.html", player_name=player_name, seasons=seasons)


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


@bp.route("/api/historical")
def api_historical():
    """Return career stats for a player."""
    player_name = request.args.get("player", "")
    start_year  = int(request.args.get("start_year", config.STATCAST_START_YEAR))
    end_year    = int(request.args.get("end_year", date.today().year))
    seasons = historical_service.get_player_career(
        player_name, _pipeline, start_year, end_year
    )
    return jsonify([dataclasses.asdict(s) for s in seasons])


@bp.route("/api/leaders")
def api_leaders():
    """Return all-time statistical leaders."""
    stat       = request.args.get("stat", "HR")
    top_n      = int(request.args.get("top_n", 25))
    start_year = int(request.args.get("start_year", config.STATCAST_START_YEAR))
    end_year   = int(request.args.get("end_year", date.today().year))
    leaders = historical_service.get_all_time_leaders(
        stat, _pipeline, start_year, end_year, top_n
    )
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
