# services/game_context_service.py
# Builds a structured context dict for an individual game — used by Gemini's
# explain_game() to generate a plain-English AI Game Breakdown.
#
# Only uses data that is reliably available from the existing model pipeline:
#   - Game / Venue / Team / ProbablePitcher dataclasses
#   - HitProbabilityResult / HRProbabilityResult lists for both teams
#   - MLB Standings (via mlb_api.get_standings) — W-L, streak, home/away records
#   - Park factors DataFrame (may be None — handled gracefully)
#   - Weather object from the Game dataclass
#
# Deliberately excluded (not yet available):
#   - Bullpen stats, injuries, game-level odds, team OPS / runs per game

from __future__ import annotations

import dataclasses
import logging
from typing import Any

logger = logging.getLogger(__name__)


def build_game_context(
    game: Any,
    hit_results: list,
    hr_results: list,
    park_factors_df: Any = None,
    mlb_api: Any = None,
    date_str: str = "",
) -> dict:
    """Build a structured game-context dict for Gemini explain_game().

    Args:
        game:            Game dataclass (or dict) for the game.
        hit_results:     List of HitProbabilityResult for *this* game.
        hr_results:      List of HRProbabilityResult for *this* game.
        park_factors_df: Park factors DataFrame from DataPipeline (may be None).
        mlb_api:         mlb_api module — used to call get_standings(). May be None.
        date_str:        YYYY-MM-DD string for the game date (used for season lookup).

    Returns:
        Structured dict ready to be serialised and passed to Gemini.
    """
    gdict = _to_dict(game)

    home = gdict.get("home_team") or {}
    away = gdict.get("away_team") or {}
    home = _to_dict(home)
    away = _to_dict(away)

    hp = _to_dict(gdict.get("home_pitcher") or {})
    ap = _to_dict(gdict.get("away_pitcher") or {})

    w    = _to_dict(gdict.get("weather") or {})
    v    = _to_dict(gdict.get("venue") or {})

    home_abbr = home.get("abbreviation", "")
    away_abbr = away.get("abbreviation", "")
    home_id   = home.get("id", 0)
    away_id   = away.get("id", 0)

    # ── Fresh weather lookup (30-min TTL, independent of the model cache) ─────
    # The game dict carries weather baked in at model-build time (up to 2h old).
    # Re-fetching here with a short TTL gives the game detail page current data
    # without requiring a full model rebuild.
    venue_name_for_wx = v.get("name", "")
    if venue_name_for_wx:
        try:
            from api import weather_api as _weather_api
            fresh = _weather_api.get_stadium_weather(venue_name_for_wx, ttl_hours=0.5)
            if fresh is not None:
                fresh_dict = _to_dict(fresh)
                if fresh_dict.get("temp_f") is not None or fresh_dict.get("is_dome"):
                    w = fresh_dict  # override model snapshot with fresh data
                    logger.debug(
                        "build_game_context: using fresh weather for '%s' (fetched_at=%s)",
                        venue_name_for_wx,
                        fresh_dict.get("fetched_at", "?"),
                    )
        except Exception as exc:
            logger.debug("build_game_context: fresh weather fetch failed ('%s'): %s", venue_name_for_wx, exc)
            # Fall through — use the model's baked-in weather

    # ── Split results by team ──────────────────────────────────────────────────
    home_hit = [r for r in hit_results if _player_team(r) == home_abbr]
    away_hit = [r for r in hit_results if _player_team(r) == away_abbr]
    home_hr  = [r for r in hr_results  if _player_team(r) == home_abbr]
    away_hr  = [r for r in hr_results  if _player_team(r) == away_abbr]

    # ── Standings + team pitching ──────────────────────────────────────────────
    standings = {}
    home_pitching_stats: dict = {}
    away_pitching_stats: dict = {}
    if mlb_api and date_str:
        season = int(date_str[:4])
        try:
            raw = mlb_api.get_standings(season) or {}
            standings = _parse_standings(raw)
        except Exception as exc:
            logger.warning("Standings fetch failed in game_context_service: %s", exc)
        home_pitching_stats = _fetch_team_pitching(home_id, mlb_api, season)
        away_pitching_stats = _fetch_team_pitching(away_id, mlb_api, season)

    home_record = standings.get(home_id) or standings.get(str(home_id)) or {}
    away_record = standings.get(away_id) or standings.get(str(away_id)) or {}

    # ── Park factors ───────────────────────────────────────────────────────────
    park_ctx = _get_park_ctx(v.get("name", ""), park_factors_df, home_abbr)

    # ── Team form & bullpen ────────────────────────────────────────────────────
    home_form    = _team_form(home_record)
    away_form    = _team_form(away_record)
    home_bullpen = _bullpen_ctx(home_pitching_stats)
    away_bullpen = _bullpen_ctx(away_pitching_stats)

    # ── Aggregate offensive projections ───────────────────────────────────────
    home_offense = _aggregate_offense(home_hit, home_hr)
    away_offense = _aggregate_offense(away_hit, away_hr)

    # ── Weather context ────────────────────────────────────────────────────────
    # Prefer static park data for dome/retractable status — more reliable than
    # the weather object, which may not set is_dome for retractable-roof parks.
    is_dome        = park_ctx.get("is_dome",        bool(w.get("is_dome")))
    is_retractable = park_ctx.get("is_retractable", False)

    weather_ctx = {
        "is_dome":          is_dome,
        "is_retractable":   is_retractable,
        "temp_f":           w.get("temp_f"),
        "wind_speed_mph":   w.get("wind_speed_mph"),
        "wind_direction_deg": w.get("wind_direction_deg"),
        "condition_text":   w.get("condition_text"),
        "cloud_cover_pct":  w.get("cloud_cover_pct"),
        "precipitation_mm": w.get("precipitation_mm", 0),
    }

    data_availability = {
        "standings":    bool(home_record or away_record),
        "park_factors": park_ctx.get("available", False),
        "park_source":  park_ctx.get("source", "none"),  # "static", "live+static", "neutral_fallback"
        "weather":      weather_ctx.get("temp_f") is not None,
        "home_lineup":  len(home_hit) > 0,
        "away_lineup":  len(away_hit) > 0,
        "pitchers":     bool(hp.get("name") or ap.get("name")),
        "team_pitching": bool(home_bullpen.get("available") or away_bullpen.get("available")),
    }

    # ── Edges (computed after all data is ready) ───────────────────────────────
    edges = _compute_edges(hp, ap, home_offense, away_offense, weather_ctx, park_ctx, data_availability)

    # ── Key players ────────────────────────────────────────────────────────────
    key_players = _build_key_players(home_hit, away_hit, home_hr, away_hr, home_abbr, away_abbr)

    return {
        "game_pk":      gdict.get("game_pk", 0),
        "date":         date_str or gdict.get("date", ""),
        "status":       gdict.get("status", "scheduled"),
        "venue":        v.get("name", ""),
        "is_dome":      is_dome,
        "is_retractable": is_retractable,

        "home_team": {
            "name":         home.get("name", ""),
            "abbreviation": home_abbr,
            "league":       home.get("league", ""),
            "division":     home.get("division", ""),
            "wins":         home_record.get("wins"),
            "losses":       home_record.get("losses"),
            "win_pct":      home_record.get("win_pct"),
            "streak":       home_record.get("streak"),
            "home_wins":    home_record.get("home_wins"),
            "home_losses":  home_record.get("home_losses"),
            "away_wins":    home_record.get("away_wins"),
            "away_losses":  home_record.get("away_losses"),
        },
        "away_team": {
            "name":         away.get("name", ""),
            "abbreviation": away_abbr,
            "league":       away.get("league", ""),
            "division":     away.get("division", ""),
            "wins":         away_record.get("wins"),
            "losses":       away_record.get("losses"),
            "win_pct":      away_record.get("win_pct"),
            "streak":       away_record.get("streak"),
            "home_wins":    away_record.get("home_wins"),
            "home_losses":  away_record.get("home_losses"),
            "away_wins":    away_record.get("away_wins"),
            "away_losses":  away_record.get("away_losses"),
        },

        "home_pitcher": _pitcher_ctx(hp, home_abbr),
        "away_pitcher": _pitcher_ctx(ap, away_abbr),

        "home_offense": home_offense,
        "away_offense": away_offense,

        "weather": weather_ctx,
        "park":    park_ctx,

        "team_form":    {"home": home_form, "away": away_form},
        "team_pitching": {"home": home_bullpen, "away": away_bullpen},

        "data_availability": data_availability,

        "edges": edges,
        "key_players": key_players,
    }


# ── Edge computation ──────────────────────────────────────────────────────────

def _compute_edges(
    hp: dict,
    ap: dict,
    home_off: dict,
    away_off: dict,
    weather_ctx: dict,
    park_ctx: dict,
    data_availability: dict,
) -> dict:
    """Compute edge labels from structured data."""
    pitch_edge, pitch_score, pitch_metrics = _pitching_edge(hp, ap)
    return {
        "starting_pitching_edge":      pitch_edge,
        "pitching_edge_score":         pitch_score,    # composite score (+ = home)
        "pitching_metrics_used":       pitch_metrics,  # count of metrics that voted
        "offensive_projection_edge":   _offensive_edge(home_off, away_off),
        "weather_park_edge":           _weather_park_edge(weather_ctx, park_ctx),
        "confidence_tier":             _confidence_tier(data_availability),
    }


def _pitching_edge(hp: dict, ap: dict) -> str:
    """Compute starting_pitching_edge via weighted composite of predictive metrics.

    Metrics and weights (positive score = home pitcher has edge):

      Tier 1 — Statcast expected stats (remove defense/luck entirely)
        xERA              weight 3.0   min_gap 0.20
        xwOBA allowed     weight 2.5   min_gap 0.010

      Tier 2 — Fielding-independent and contact-quality
        FIP               weight 2.0   min_gap 0.25
        K%                weight 1.5   min_gap 0.020  (higher = better)
        Hard-hit% allowed weight 1.5   min_gap 0.030  (lower = better)
        Barrel% allowed   weight 1.5   min_gap 0.010  (lower = better)

      Tier 3 — Control and swing-and-miss
        BB%               weight 1.0   min_gap 0.015  (lower = better)
        Whiff%            weight 1.0   min_gap 0.020  (higher = better)

      Tier 4 — Traditional (defense/BABIP-influenced, used as weak confirmation)
        WHIP              weight 0.5   min_gap 0.10

      ERA: intentionally excluded (too dependent on defense and BABIP luck).

    Each metric casts a binary directional vote (+weight or -weight) only when
    its gap between pitchers exceeds the noise threshold. Metrics below threshold
    contribute 0 — they are too close to call and should not move the needle.

    Score thresholds → edge label:
      score ≥  1.5  → "home"
      score ≤ -1.5  → "away"
      |score| < 1.5 → "neutral"
      zero metrics available for either pitcher → "mixed"
    """
    has_hp = bool(hp.get("name") and hp.get("name") != "TBD")
    has_ap = bool(ap.get("name") and ap.get("name") != "TBD")

    if not has_hp and not has_ap:
        return "neutral", 0.0, 0
    if not has_hp or not has_ap:
        return "mixed", 0.0, 0

    score = 0.0
    metrics_used   = 0   # metrics that exceeded the noise threshold (cast a vote)
    stats_seen     = 0   # metrics where both pitchers had non-None values

    def _vote(hp_val, ap_val, weight: float, lower_is_better: bool, min_gap: float) -> None:
        """Cast a weighted directional vote if the gap exceeds the noise floor."""
        nonlocal score, metrics_used, stats_seen
        if hp_val is None or ap_val is None:
            return
        stats_seen += 1  # both pitchers have this stat
        # diff > 0 means home pitcher is better for this metric
        diff = (ap_val - hp_val) if lower_is_better else (hp_val - ap_val)
        if abs(diff) < min_gap:
            return  # gap is within noise — no vote
        score += weight if diff > 0 else -weight
        metrics_used += 1

    # Tier 1 — Statcast expected (highest predictive value; no defense/luck)
    _vote(hp.get("xera"),          ap.get("xera"),          3.0, True,  0.20)
    _vote(hp.get("xwoba_allowed"), ap.get("xwoba_allowed"), 2.5, True,  0.010)

    # Tier 2 — Fielding-independent and contact quality
    _vote(hp.get("fip"),                  ap.get("fip"),                  2.0, True,  0.25)
    _vote(hp.get("k_pct"),                ap.get("k_pct"),                1.5, False, 0.020)
    _vote(hp.get("hard_hit_pct_allowed"), ap.get("hard_hit_pct_allowed"), 1.5, True,  0.030)
    _vote(hp.get("barrel_pct_allowed"),   ap.get("barrel_pct_allowed"),   1.5, True,  0.010)

    # Tier 3 — Control and swing-and-miss
    _vote(hp.get("bb_pct"),              ap.get("bb_pct"),              1.0, True,  0.015)
    _vote(hp.get("whiff_pct_generated"), ap.get("whiff_pct_generated"), 1.0, False, 0.020)

    # Tier 4 — Traditional (BABIP/defense-influenced; weakest signal)
    _vote(hp.get("whip"), ap.get("whip"), 0.5, True, 0.10)

    if stats_seen == 0:
        return "mixed", 0.0, 0  # no comparable stats for either pitcher

    # stats_seen > 0 but metrics_used == 0: both pitchers' stats are within
    # noise thresholds on every metric — they appear evenly matched.
    if score >= 1.5:
        return "home",    round(score, 2), metrics_used
    if score <= -1.5:
        return "away",    round(score, 2), metrics_used
    return "neutral",     round(score, 2), metrics_used


def _offensive_edge(home_off: dict, away_off: dict) -> str:
    """Compute offensive_projection_edge label."""
    home_avail = home_off.get("available", False)
    away_avail = away_off.get("available", False)

    if not home_avail and not away_avail:
        return "neutral"
    if not home_avail or not away_avail:
        return "mixed"

    home_avg = home_off.get("avg_hit_prob")
    away_avg = away_off.get("avg_hit_prob")
    if home_avg is None or away_avg is None:
        return "mixed"

    diff = home_avg - away_avg

    if diff >= 0.05:
        return "home"
    if diff <= -0.05:
        return "away"

    abs_diff = abs(diff)
    if 0.025 <= abs_diff < 0.05:
        # Check yes_count as tiebreaker
        home_yes = home_off.get("yes_count", 0)
        away_yes = away_off.get("yes_count", 0)
        yes_diff = home_yes - away_yes
        if yes_diff > 0 and diff > 0:
            return "home"
        if yes_diff < 0 and diff < 0:
            return "away"
        return "mixed"

    return "neutral"


def _weather_park_edge(weather_ctx: dict, park_ctx: dict) -> str:
    """Compute weather_park_edge label.

    Uses run_factor from static park data as the primary signal.
    Weather adjusts the edge for outdoor parks; dome/retractable parks
    are assessed on park factors only (weather impact is reduced/absent).
    """
    is_dome        = weather_ctx.get("is_dome", False)
    is_retractable = weather_ctx.get("is_retractable", False)

    # Park factors drive the primary assessment (now always available from static data)
    if park_ctx.get("available"):
        # Prefer run_factor (most holistic) then hit_factor as fallback
        factor = park_ctx.get("run_factor") or park_ctx.get("hit_factor")
        if factor is not None:
            if factor >= 108:
                return "hitter_friendly"
            if factor <= 93:
                return "pitcher_friendly"

            # Near-neutral park — check weather for outdoor parks only
            if not is_dome and not is_retractable:
                temp_f = weather_ctx.get("temp_f")
                if temp_f is not None:
                    if temp_f < 45:
                        return "pitcher_friendly"
                    wind_speed = weather_ctx.get("wind_speed_mph") or 0
                    if temp_f >= 88 and wind_speed >= 12:
                        return "hitter_friendly"

            return "neutral"

    # No park data at all — fall back to weather signals only
    if not is_dome and not is_retractable:
        temp_f = weather_ctx.get("temp_f")
        if temp_f is not None:
            if temp_f < 50:
                return "pitcher_friendly"
            wind_speed = weather_ctx.get("wind_speed_mph") or 0
            if temp_f >= 85 and wind_speed >= 10:
                return "hitter_friendly"
            return "neutral"

    return "unavailable"


def _confidence_tier(data_availability: dict) -> str:
    """Compute confidence_tier from data_availability scores."""
    score = 0
    if data_availability.get("pitchers"):     score += 2
    if data_availability.get("home_lineup"):  score += 1
    if data_availability.get("away_lineup"):  score += 1
    if data_availability.get("standings"):    score += 1
    if data_availability.get("park_factors"): score += 1
    if data_availability.get("weather"):      score += 1

    if score >= 6:
        return "strong"
    if score >= 4:
        return "moderate"
    if score >= 2:
        return "slight"
    return "low"


# ── Key players ───────────────────────────────────────────────────────────────

def _build_key_players(
    home_hit: list,
    away_hit: list,
    home_hr: list,
    away_hr: list,
    home_abbr: str,
    away_abbr: str,
) -> dict:
    """Build key player dicts for home and away teams."""
    return {
        "home": _team_key_players(home_hit, home_hr),
        "away": _team_key_players(away_hit, away_hr),
    }


def _team_key_players(hit_results: list, hr_results: list) -> dict:
    """Build key player summary for one team."""
    # Build HR prob lookup keyed by player_id
    hr_by_player: dict = {}
    for r in hr_results:
        try:
            pid = _get_player_id(r)
            prob = r.hr_probability if hasattr(r, "hr_probability") else (r.get("hr_probability", 0) if isinstance(r, dict) else 0)
            hr_by_player[pid] = prob
        except Exception:
            pass

    hit_entries: list[dict] = []
    hr_entries:  list[dict] = []
    yes_picks:   list[dict] = []

    for r in hit_results:
        try:
            p = r.player if hasattr(r, "player") else (r.get("player") or {})
            pd_dict = _to_dict(p)
            name       = pd_dict.get("name", "")
            hand       = pd_dict.get("hand", "")
            lineup_pos = pd_dict.get("lineup_position", 0)
            pid        = pd_dict.get("player_id", 0)

            hit_prob = r.hit_probability if hasattr(r, "hit_probability") else (r.get("hit_probability", 0) if isinstance(r, dict) else 0)
            verdict  = r.hit_verdict if hasattr(r, "hit_verdict") else (r.get("hit_verdict", "NO") if isinstance(r, dict) else "NO")

            hit_entries.append({
                "name":            name,
                "hit_prob":        round(hit_prob, 4),
                "verdict":         verdict,
                "hand":            hand,
                "lineup_position": lineup_pos,
                "_pid":            pid,
            })

            if verdict == "YES":
                yes_picks.append({
                    "name":     name,
                    "hit_prob": round(hit_prob, 4),
                })
        except Exception as exc:
            logger.debug("Skipping hit result in _team_key_players: %s", exc)
            continue

    # Build HR entries from hr_results directly
    for r in hr_results:
        try:
            p = r.player if hasattr(r, "player") else (r.get("player") or {})
            pd_dict = _to_dict(p)
            name    = pd_dict.get("name", "")
            hr_prob = r.hr_probability if hasattr(r, "hr_probability") else (r.get("hr_probability", 0) if isinstance(r, dict) else 0)

            if hr_prob > 0.03:
                hr_entries.append({
                    "name":    name,
                    "hr_prob": round(hr_prob, 4),
                })
        except Exception as exc:
            logger.debug("Skipping hr result in _team_key_players: %s", exc)
            continue

    # Sort and slice
    top_hit = sorted(hit_entries, key=lambda x: x["hit_prob"], reverse=True)[:5]
    top_hr  = sorted(hr_entries,  key=lambda x: x["hr_prob"],  reverse=True)[:3]

    # Strip internal _pid from top_hit output
    top_hit_out = [
        {k: v for k, v in p.items() if k != "_pid"}
        for p in top_hit
    ]

    return {
        "top_hit":  top_hit_out,
        "top_hr":   top_hr,
        "yes_picks": yes_picks,
    }


# ── Internal helpers ──────────────────────────────────────────────────────────

def _to_dict(obj: Any) -> dict:
    """Convert a dataclass or dict to a plain dict."""
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return obj
    if dataclasses.is_dataclass(obj):
        return dataclasses.asdict(obj)
    if hasattr(obj, "__dict__"):
        return obj.__dict__
    return {}


def _player_team(result: Any) -> str:
    """Extract the player's team abbreviation from a probability result."""
    try:
        if hasattr(result, "player"):
            return result.player.team or ""
        if isinstance(result, dict):
            return (result.get("player") or {}).get("team", "")
    except Exception:
        pass
    return ""


def _format_streak(streak_code: str) -> str:
    """Convert a raw MLB API streakCode ('W3', 'L1') to plain English.

    Rules:
      W1  → "Won last game"       (not "win streak" — only one game)
      W2+ → "2-game win streak"
      L1  → "Lost last game"
      L2+ → "2-game losing streak"
      ""  → ""
    """
    if not streak_code or len(streak_code) < 2:
        return ""
    direction = streak_code[0].upper()
    try:
        n = int(streak_code[1:])
    except ValueError:
        return streak_code  # pass through unrecognised codes unchanged
    if direction == "W":
        return "Won last game" if n == 1 else f"{n}-game win streak"
    if direction == "L":
        return "Lost last game" if n == 1 else f"{n}-game losing streak"
    return streak_code


def _parse_standings(raw: dict) -> dict:
    """Parse MLB API standings response into {team_id: record_dict}."""
    out: dict = {}
    for division_record in raw.get("records", []):
        for tr in division_record.get("teamRecords", []):
            team_id = tr.get("team", {}).get("id")
            if not team_id:
                continue
            streak_code = ""
            streak_info = tr.get("streak", {})
            if streak_info:
                streak_code = streak_info.get("streakCode", "")

            hr  = tr.get("homeRecord", {})
            ar  = tr.get("awayRecord", {})
            wins   = tr.get("wins", 0)
            losses = tr.get("losses", 0)

            # Last-ten games split (from splitRecords when standingsTypes=regularSeason)
            l10w: int | None = None
            l10l: int | None = None
            for sr in tr.get("records", {}).get("splitRecords", []):
                if sr.get("type") == "lastTen":
                    l10w = sr.get("wins")
                    l10l = sr.get("losses")
                    break

            out[team_id] = {
                "wins":              wins,
                "losses":            losses,
                "win_pct":           tr.get("winningPercentage"),
                "streak":            streak_code,               # raw: "W3", "L1", ""
                "streak_label":      _format_streak(streak_code),  # human: "3-game win streak"
                "home_wins":         hr.get("wins"),
                "home_losses":       hr.get("losses"),
                "away_wins":         ar.get("wins"),
                "away_losses":       ar.get("losses"),
                "last_ten_wins":     l10w,
                "last_ten_losses":   l10l,
            }
    return out


def _fetch_team_pitching(team_id: int, mlb_api: Any, season: int) -> dict:
    """Fetch team season pitching aggregate stats via mlb_api.get_team_stats()."""
    if not mlb_api or not team_id:
        return {}
    try:
        if hasattr(mlb_api, "get_team_stats"):
            return mlb_api.get_team_stats(int(team_id), int(season), "pitching") or {}
    except Exception as exc:
        logger.warning("_fetch_team_pitching failed (team=%s): %s", team_id, exc)
    return {}


def _team_form(record: dict) -> dict:
    """Build a structured team form dict from a parsed standings record."""
    if not record:
        return {"available": False}
    l10w = record.get("last_ten_wins")
    l10l = record.get("last_ten_losses")
    return {
        "available":       True,
        "wins":            record.get("wins"),
        "losses":          record.get("losses"),
        "win_pct":         record.get("win_pct"),
        "streak":          record.get("streak", ""),         # raw code, e.g. "W3"
        "streak_label":    record.get("streak_label", ""),   # human text, e.g. "3-game win streak"
        "last_ten_wins":   l10w,
        "last_ten_losses": l10l,
        "home_wins":       record.get("home_wins"),
        "home_losses":     record.get("home_losses"),
        "away_wins":       record.get("away_wins"),
        "away_losses":     record.get("away_losses"),
    }


def _bullpen_ctx(team_pitching: dict) -> dict:
    """Build a bullpen/pitching context dict from team season pitching stats.

    The MLB Stats API returns team-aggregate pitching numbers (not split by
    starter vs reliever), but saves, blownSaves, and holds are reliever-specific.
    ERA and WHIP are team-wide and used as a pitching-quality proxy.
    """
    if not team_pitching:
        return {"available": False}

    def _f(key: str):
        v = team_pitching.get(key)
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    def _i(key: str):
        v = team_pitching.get(key)
        try:
            return int(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    era        = _f("era")
    saves      = _i("saves")
    blown      = _i("blownSaves")
    holds      = _i("holds")
    strikeouts = _i("strikeOuts")
    whip       = _f("whip")

    save_pct: float | None = None
    if saves is not None and blown is not None:
        opps = (saves or 0) + (blown or 0)
        save_pct = round(saves / opps, 3) if opps > 0 else None

    available = era is not None or saves is not None
    return {
        "available":   available,
        "era":         round(era,  2) if era  is not None else None,
        "whip":        round(whip, 2) if whip is not None else None,
        "saves":       saves,
        "blown_saves": blown,
        "holds":       holds,
        "save_pct":    save_pct,
        "strikeouts":  strikeouts,
    }


def _pitcher_ctx(pitcher: dict, team_abbr: str) -> dict:
    """Build a pitcher summary dict from a ProbablePitcher dict."""
    return {
        "team":                  team_abbr,
        "name":                  pitcher.get("name", "TBD"),
        "hand":                  pitcher.get("hand", "R"),
        "era":                   pitcher.get("era"),
        "xera":                  pitcher.get("xera"),
        "fip":                   pitcher.get("fip"),
        "whip":                  pitcher.get("whip"),
        "k_pct":                 pitcher.get("k_pct"),
        "bb_pct":                pitcher.get("bb_pct"),
        "hr9":                   pitcher.get("hr9"),
        "hard_hit_pct_allowed":  pitcher.get("hard_hit_pct_allowed"),
        "barrel_pct_allowed":    pitcher.get("barrel_pct_allowed"),
        "xwoba_allowed":         pitcher.get("xwoba_allowed"),
        "whiff_pct_generated":   pitcher.get("whiff_pct_generated"),
        "avg_exit_velo_allowed": pitcher.get("avg_exit_velo_allowed"),
    }


def _aggregate_offense(hit_results: list, hr_results: list) -> dict:
    """Summarise team offensive projections from player-level results."""
    if not hit_results:
        return {"player_count": 0, "available": False}

    # Build HR prob lookup keyed by player_id
    hr_by_player: dict = {}
    for r in hr_results:
        try:
            pid = _get_player_id(r)
            prob = r.hr_probability if hasattr(r, "hr_probability") else (r.get("hr_probability", 0) if isinstance(r, dict) else 0)
            hr_by_player[pid] = prob
        except Exception:
            pass

    hit_probs  = []
    hr_probs   = []
    xba_vals   = []
    avg_vals   = []
    barrel_vals = []
    hard_hit_vals = []
    recent_vals   = []
    yes_count  = 0
    lean_count = 0
    no_count   = 0

    top_hit: list[dict] = []
    top_hr:  list[dict] = []

    for r in hit_results:
        try:
            p = r.player if hasattr(r, "player") else (r.get("player") or {})
            pd = _to_dict(p)
            pid = pd.get("player_id", 0)

            hit_prob = r.hit_probability if hasattr(r, "hit_probability") else (r.get("hit_probability", 0) if isinstance(r, dict) else 0)
            verdict  = r.hit_verdict if hasattr(r, "hit_verdict") else (r.get("hit_verdict", "NO") if isinstance(r, dict) else "NO")
            hr_prob  = hr_by_player.get(pid, 0)

            hit_probs.append(hit_prob)
            hr_probs.append(hr_prob)

            if pd.get("xba"):    xba_vals.append(pd["xba"])
            if pd.get("avg"):    avg_vals.append(pd["avg"])
            if pd.get("barrel_pct") is not None: barrel_vals.append(pd["barrel_pct"])
            if pd.get("hard_hit_pct") is not None: hard_hit_vals.append(pd["hard_hit_pct"])
            if pd.get("recent_avg", 0) > 0: recent_vals.append(pd["recent_avg"])

            if verdict == "YES":  yes_count  += 1
            elif verdict == "LEAN": lean_count += 1
            else: no_count += 1

            top_hit.append({"name": pd.get("name", ""), "hit_prob": hit_prob, "verdict": verdict})
            top_hr.append({"name": pd.get("name", ""), "hr_prob": hr_prob})

        except Exception as exc:
            logger.debug("Skipping result in _aggregate_offense: %s", exc)
            continue

    def _avg(lst): return round(sum(lst) / len(lst), 4) if lst else None

    # Top 5 by hit probability (expanded from 3)
    top_hit_sorted = sorted(top_hit, key=lambda x: x["hit_prob"], reverse=True)[:5]
    top_hr_sorted  = sorted(top_hr,  key=lambda x: x["hr_prob"],  reverse=True)[:3]

    return {
        "available":      True,
        "player_count":   len(hit_results),
        "avg_hit_prob":   _avg(hit_probs),
        "avg_hr_prob":    _avg(hr_probs),
        "avg_xba":        _avg(xba_vals),
        "avg_avg":        _avg(avg_vals),
        "avg_barrel_pct": _avg(barrel_vals),
        "avg_hard_hit_pct": _avg(hard_hit_vals),
        "avg_recent_avg": _avg(recent_vals),
        "yes_count":      yes_count,
        "lean_count":     lean_count,
        "no_count":       no_count,
        "top_hit_picks":  top_hit_sorted,
        "top_hr_picks":   top_hr_sorted,
    }


def _get_player_id(result: Any) -> int:
    """Extract player_id from a probability result."""
    try:
        if hasattr(result, "player"):
            return result.player.player_id or 0
        if isinstance(result, dict):
            return (result.get("player") or {}).get("player_id", 0)
    except Exception:
        pass
    return 0


def _get_park_ctx(venue_name: str, park_factors_df: Any, home_team_abbr: str = "") -> dict:
    """Look up park factors for a venue.

    Priority:
      1. Static dataset (always available, always tried first for game context)
      2. Live park_factors_df from pybaseball (merges run_factor if DF has it)
      3. Neutral fallback (100 for all) — logged at WARNING level

    Returns a rich dict including run_factor, hr_factor, hit_factor, handedness
    splits, dome/retractable flags, and tendency_notes for Gemini.
    """
    from data.static_park_factors import lookup_park_factors

    # ── Primary: static dataset ───────────────────────────────────────────────
    ctx = lookup_park_factors(venue_name, home_team_abbr)

    # ── Optional: upgrade run_factor from live DataFrame if available ─────────
    if park_factors_df is not None and not getattr(park_factors_df, "empty", True):
        try:
            vn = venue_name.lower()
            row = park_factors_df[
                park_factors_df["venue"].str.lower().str.contains(vn[:8], na=False)
            ]
            if not row.empty:
                r = row.iloc[0]
                live_hit = r.get("basic_5yr", r.get("hit_factor"))
                live_hr  = r.get("hr_factor", r.get("HR"))
                if live_hit is not None:
                    ctx["hit_factor"] = float(live_hit)
                    ctx["source"] = "live+static"
                if live_hr is not None:
                    ctx["hr_factor"] = float(live_hr)
        except Exception as exc:
            logger.debug("Live park factor merge failed for '%s': %s", venue_name, exc)

    return ctx
