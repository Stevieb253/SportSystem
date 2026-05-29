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

    # ── Split results by team ──────────────────────────────────────────────────
    home_hit = [r for r in hit_results if _player_team(r) == home_abbr]
    away_hit = [r for r in hit_results if _player_team(r) == away_abbr]
    home_hr  = [r for r in hr_results  if _player_team(r) == home_abbr]
    away_hr  = [r for r in hr_results  if _player_team(r) == away_abbr]

    # ── Standings ──────────────────────────────────────────────────────────────
    standings = {}
    if mlb_api and date_str:
        try:
            season = int(date_str[:4])
            raw = mlb_api.get_standings(season) or {}
            standings = _parse_standings(raw)
        except Exception as exc:
            logger.warning("Standings fetch failed in game_context_service: %s", exc)

    home_record = standings.get(home_id) or standings.get(str(home_id)) or {}
    away_record = standings.get(away_id) or standings.get(str(away_id)) or {}

    # ── Park factors ───────────────────────────────────────────────────────────
    park_ctx = _get_park_ctx(v.get("name", ""), park_factors_df, home_abbr)

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
    return {
        "starting_pitching_edge":    _pitching_edge(hp, ap),
        "offensive_projection_edge": _offensive_edge(home_off, away_off),
        "weather_park_edge":         _weather_park_edge(weather_ctx, park_ctx),
        "confidence_tier":           _confidence_tier(data_availability),
    }


def _pitching_edge(hp: dict, ap: dict) -> str:
    """Compute starting_pitching_edge label."""
    hp_name = hp.get("name", "TBD")
    ap_name = ap.get("name", "TBD")

    # Neither has any pitcher data
    has_hp = bool(hp_name and hp_name != "TBD")
    has_ap = bool(ap_name and ap_name != "TBD")
    if not has_hp and not has_ap:
        return "neutral"

    # Either is TBD or missing xERA
    hp_xera = hp.get("xera")
    ap_xera = ap.get("xera")
    if not has_hp or not has_ap or hp_xera is None or ap_xera is None:
        return "mixed"

    # Both have xERA — compute diff (positive = home pitcher better)
    diff = ap_xera - hp_xera

    if diff >= 0.45:
        return "home"
    if diff <= -0.45:
        return "away"

    abs_diff = abs(diff)
    if 0.20 <= abs_diff < 0.45:
        # Use WHIP as tiebreaker
        hp_whip = hp.get("whip")
        ap_whip = ap.get("whip")
        if hp_whip is not None and ap_whip is not None:
            whip_diff = ap_whip - hp_whip  # positive = home pitcher better
            if abs(whip_diff) >= 0.10 and (whip_diff > 0) == (diff > 0):
                return "home" if diff > 0 else "away"
        return "mixed"

    # |diff| < 0.20
    return "neutral"


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
            out[team_id] = {
                "wins":        wins,
                "losses":      losses,
                "win_pct":     tr.get("winningPercentage"),
                "streak":      streak_code,
                "home_wins":   hr.get("wins"),
                "home_losses": hr.get("losses"),
                "away_wins":   ar.get("wins"),
                "away_losses": ar.get("losses"),
            }
    return out


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
