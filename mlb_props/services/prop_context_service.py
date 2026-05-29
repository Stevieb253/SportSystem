# services/prop_context_service.py
# Assembles structured context JSON for a single player prop.
#
# PURPOSE:
#   One function — build_prop_context() — gathers every piece of real data
#   available for a batter/pitcher matchup and returns it as a plain dict.
#   Missing data is always represented as null + available=False, never guessed.
#   This JSON is the data layer for Phase 2 (Gemini explanations).
#
# DESIGN RULES:
#   - Never invents or interpolates stats.
#   - Every section has an "available" boolean so callers know what's real.
#   - Odds are enriched inline from the cached odds dict — not fetched fresh.
#   - Park factors marked available=False when pybaseball is broken (expected).
#   - Safe to call per-request; BvP is cached 24h, all other data is in-memory.

import logging
import sys
import os
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

logger = logging.getLogger(__name__)


def build_prop_context(
    player_id: int,
    prop_type: str,
    model: dict,
    bvp_api: Any,
    odds_by_player: dict | None = None,
    park_factors_df: Any = None,
    pitch_arsenal_api: Any = None,
) -> dict | None:
    """Assemble full structured context for one player's prop.

    Args:
        player_id:         MLBAM batter ID.
        prop_type:         "hit" or "hr".
        model:             Serialised model dict from ModelBuilder (plain dicts).
        bvp_api:           Injected bvp_api module (keeps this service testable).
        odds_by_player:    Dict keyed by player name from the odds cache (may be None).
        park_factors_df:   DataFrame from DataPipeline.load_park_factors() (may be None).
        pitch_arsenal_api: Injected pitch_arsenal_api module (may be None).

    Returns:
        Context dict, or None if the player isn't found in this model.
    """
    prob_key = "hit_probabilities" if prop_type == "hit" else "hr_probabilities"

    row = _find_player_row(model.get(prob_key, []), player_id)
    if row is None:
        return None

    player  = row.get("player") or {}
    pitcher = row.get("vs_pitcher") or {}
    game    = row.get("game") or {}

    # ── Player ────────────────────────────────────────────────────────────────
    player_ctx = {
        "name":            player.get("name"),
        "team":            player.get("team"),
        "hand":            player.get("hand"),
        "lineup_position": player.get("lineup_position"),
    }

    # ── Prop ──────────────────────────────────────────────────────────────────
    prob_field   = "hit_probability"  if prop_type == "hit" else "hr_probability"
    verdict_field = "hit_verdict"     if prop_type == "hit" else "hr_verdict"
    model_prob   = row.get(prob_field)

    odds_ctx = _build_odds_ctx(
        player_name   = player.get("name", ""),
        prop_type     = prop_type,
        model_prob    = model_prob,
        odds_by_player = odds_by_player or {},
    )

    prop_ctx = {
        "type":                prop_type,
        "model_probability":   model_prob,
        "verdict":             row.get(verdict_field),
        "lineup_status":       row.get("lineup_status"),
        "odds":                odds_ctx,
    }

    # ── Batter season stats ───────────────────────────────────────────────────
    batter_available = _any_real(player, [
        "xba", "hard_hit_pct", "avg", "barrel_pct", "ev50",
    ])
    batter_season = {
        "available":           batter_available,
        # Contact / on-base
        "avg":                 _f(player, "avg"),
        "obp":                 _f(player, "obp"),
        "slg":                 _f(player, "slg"),
        "ops":                 _f(player, "ops"),
        "woba":                _f(player, "woba"),
        "xba":                 _f(player, "xba"),
        "xwoba":               _f(player, "xwoba"),
        # Quality of contact
        "hard_hit_pct":        _f(player, "hard_hit_pct"),
        "barrel_pct":          _f(player, "barrel_pct"),
        "sweet_spot_pct":      _f(player, "sweet_spot_pct"),
        "exit_velocity":       _f(player, "avg_exit_velo"),   # BatterMetrics field name
        "ev50":                _f(player, "ev50"),
        "hr_fb_ratio":         _f(player, "hr_fb_ratio"),
        # Plate discipline
        "whiff_pct":           _f(player, "whiff_pct"),
        "k_pct":               _f(player, "k_pct"),
        "bb_pct":              _f(player, "bb_pct"),
        # Recent form (14 days)
        "recent_avg":          _f(player, "recent_avg"),
        "recent_hard_hit_pct": _f(player, "recent_hard_hit_pct"),
        "recent_barrel_pct":   _f(player, "recent_barrel_pct"),
        "recent_exit_velo":    _f(player, "recent_exit_velo"),
        "source":              "Baseball Savant",
    }

    # ── Pitcher ───────────────────────────────────────────────────────────────
    # ProbablePitcher serialises with key "id", not "player_id"
    pitcher_id = pitcher.get("id") or pitcher.get("player_id")
    pitcher_available = _any_real(pitcher, [
        "era", "xera", "hard_hit_pct_allowed", "k_pct",
    ])
    pitcher_ctx = {
        "available":              pitcher_available,
        "player_id":              pitcher_id,
        "name":                   pitcher.get("name"),
        "hand":                   pitcher.get("hand"),
        # Standard rates
        "era":                    _f(pitcher, "era"),
        "xera":                   _f(pitcher, "xera"),
        "fip":                    _f(pitcher, "fip"),
        "whip":                   _f(pitcher, "whip"),
        "k9":                     _f(pitcher, "k9"),
        "bb9":                    _f(pitcher, "bb9"),
        "hr9":                    _f(pitcher, "hr9"),
        "k_pct":                  _f(pitcher, "k_pct"),
        "bb_pct":                 _f(pitcher, "bb_pct"),
        # Statcast allowed
        "hard_hit_pct_allowed":   _f(pitcher, "hard_hit_pct_allowed"),
        "barrel_pct_allowed":     _f(pitcher, "barrel_pct_allowed"),
        "avg_exit_velo_allowed":  _f(pitcher, "avg_exit_velo_allowed"),
        "xwoba_allowed":          _f(pitcher, "xwoba_allowed"),
        "whiff_pct_generated":    _f(pitcher, "whiff_pct_generated"),
        "source":                 "Baseball Savant",
    }

    # ── BvP history ───────────────────────────────────────────────────────────
    batter_id_val = player.get("player_id") or player_id
    bvp_ctx = _fetch_bvp_safe(bvp_api, int(batter_id_val), pitcher_id)

    # ── Game context ──────────────────────────────────────────────────────────
    weather    = game.get("weather") or {}
    venue_dict = game.get("venue") or {}
    venue_name = venue_dict.get("name", "")

    # Extract home team abbreviation for park factor fallback lookup
    teams_data = game.get("teams") or {}
    home_team_abbr = (
        (teams_data.get("home") or {})
        .get("team", {})
        .get("abbreviation", "")
    )

    # Build park context first — weather_ctx uses is_dome / is_retractable from it
    park_ctx = _get_park_context(venue_name, park_factors_df, home_team_abbr)

    weather_ctx = {
        "temp_f":               _f(weather, "temp_f"),
        "wind_speed_mph":       _f(weather, "wind_speed_mph"),
        "wind_direction_deg":   _f(weather, "wind_direction_deg"),
        "wind_direction_label": _wind_label(_f(weather, "wind_direction_deg")),
        "is_dome":              park_ctx.get("is_dome", weather.get("is_dome", False)),
        "is_retractable":       park_ctx.get("is_retractable", False),
        "condition":            weather.get("condition_text"),
        "precipitation_mm":     _f(weather, "precipitation_mm"),
        "cloud_cover_pct":      weather.get("cloud_cover_pct"),
    }

    context_ctx = {
        "game_pk": game.get("game_pk"),
        "venue":   venue_name,
        "park":    park_ctx,
        "weather": weather_ctx,
    }

    # ── Pitch arsenal + batter vs pitch type ─────────────────────────────────
    from datetime import date as _date
    year = _date.today().year

    arsenal_ctx = {"available": False, "pitches": [], "primary_pitch": None, "source": "Baseball Savant"}
    batter_vs_pitch_ctx = {"available": False, "splits": [], "source": "Baseball Savant"}
    pitch_matchup_notes: list[str] = []

    if pitch_arsenal_api and pitcher_id:
        try:
            arsenal_ctx = pitch_arsenal_api.get_pitcher_arsenal(int(pitcher_id), year)
        except Exception as exc:
            logger.warning("Pitch arsenal fetch failed (pitcher=%s): %s", pitcher_id, exc)

    if pitch_arsenal_api:
        batter_id_val = player.get("player_id") or player_id
        try:
            batter_vs_pitch_ctx = pitch_arsenal_api.get_batter_vs_pitch_type(int(batter_id_val), year)
        except Exception as exc:
            logger.warning("Batter-vs-pitch fetch failed (batter=%s): %s", batter_id_val, exc)

    if arsenal_ctx["available"] or batter_vs_pitch_ctx["available"]:
        pitch_matchup_notes = _generate_matchup_notes(
            arsenal_ctx,
            batter_vs_pitch_ctx,
        )

    # ── Data availability map ─────────────────────────────────────────────────
    # Every key is an explicit boolean — Gemini reads this to know what's real.
    availability = {
        "batter_savant":   batter_season["available"],
        "pitcher_savant":  pitcher_ctx["available"],
        "bvp_history":     bvp_ctx["available"],
        "odds":            odds_ctx["available"],
        "pitch_arsenal":   arsenal_ctx["available"],
        "batter_vs_pitch": batter_vs_pitch_ctx["available"],
        "park_factor":     park_ctx["available"],
        "weather":         weather_ctx["temp_f"] is not None,
    }

    return {
        "player":              player_ctx,
        "prop":                prop_ctx,
        "batter_season":       batter_season,
        "pitcher":             pitcher_ctx,
        "bvp":                 bvp_ctx,
        "pitch_arsenal":       arsenal_ctx,
        "batter_vs_pitch":     batter_vs_pitch_ctx,
        "pitch_matchup_notes": pitch_matchup_notes,
        "context":             context_ctx,
        "data_availability":   availability,
    }


# ── Internal helpers ───────────────────────────────────────────────────────────

def _find_player_row(results: list, player_id: int) -> dict | None:
    """Find the first result row matching player_id."""
    for r in results:
        if not isinstance(r, dict):
            continue
        if (r.get("player") or {}).get("player_id") == player_id:
            return r
    return None


def _build_odds_ctx(
    player_name: str,
    prop_type: str,
    model_prob: float | None,
    odds_by_player: dict,
) -> dict:
    """Look up odds from the cached odds dict and compute edge."""
    empty = {
        "available":          False,
        "sportsbook_odds":    None,
        "sportsbook_line":    None,
        "implied_probability": None,
        "edge":               None,
        "best_book":          None,
    }

    if not odds_by_player or not player_name or model_prob is None:
        return empty

    # HR props may be stored under "PlayerName_hr" key
    odds_key = (player_name + "_hr") if prop_type == "hr" else player_name
    odds_info = odds_by_player.get(odds_key) or odds_by_player.get(player_name)
    if not odds_info:
        return empty

    try:
        from services import odds_service
        edge_data = odds_service.enrich_with_edge(float(model_prob), odds_info)
        if edge_data.get("implied_probability") is None:
            return empty
        return {
            "available":           True,
            "sportsbook_odds":     edge_data.get("sportsbook_odds"),
            "sportsbook_line":     edge_data.get("sportsbook_line"),
            "implied_probability": edge_data.get("implied_probability"),
            "edge":                edge_data.get("edge"),
            "best_book":           edge_data.get("best_book"),
        }
    except Exception as exc:
        logger.warning("Odds enrichment failed for %s: %s", player_name, exc)
        return empty


def _fetch_bvp_safe(bvp_api: Any, batter_id: int, pitcher_id: Any) -> dict:
    """Call bvp_api.get_bvp_stats() safely; return empty result on any failure."""
    empty = {
        "available":   False,
        "ab":          0, "hits": 0, "hr": 0, "doubles": 0, "triples": 0,
        "k":           0, "bb":   0,
        "avg":         None, "obp": None, "slg": None, "ops": None,
        "sample_size": "none",
        "warning":     None,
        "source":      "MLB Stats API",
    }

    if not pitcher_id or not batter_id:
        return empty

    try:
        return bvp_api.get_bvp_stats(int(batter_id), int(pitcher_id))
    except Exception as exc:
        logger.warning(
            "BvP lookup failed (batter=%s pitcher=%s): %s", batter_id, pitcher_id, exc
        )
        return empty


def _get_park_context(
    venue_name: str,
    park_df: Any,
    home_team_abbr: str = "",
) -> dict:
    """Return rich park factor context for a venue.

    Uses the static park factor dataset as the primary source (always available),
    with the live pybaseball DataFrame as an optional upgrade layer for run/hit/HR.
    """
    from data.static_park_factors import lookup_park_factors

    ctx = lookup_park_factors(venue_name, home_team_abbr)

    # Optional upgrade: merge live DataFrame factors on top of static baseline
    if park_df is not None and not getattr(park_df, "empty", True):
        try:
            name_col  = "Team" if "Team" in park_df.columns else park_df.columns[0]
            first_word = venue_name.lower().split()[0] if venue_name else ""
            if first_word:
                row = park_df[
                    park_df[name_col].str.lower().str.contains(first_word, na=False)
                ]
                if not row.empty:
                    r = row.iloc[0]
                    hit_live = float(r.get("1B", r.get("H", 0)) or 0) or None
                    hr_live  = float(r.get("HR", 0) or 0) or None
                    if hit_live:
                        ctx["hit_factor"] = hit_live
                    if hr_live:
                        ctx["hr_factor"] = hr_live
                    ctx["source"] = "live+static"
        except Exception:
            pass  # static data already in ctx — no harm done

    # Derive a human-readable park profile from run + HR factors
    run_f = ctx.get("run_factor") or 100
    hr_f  = ctx.get("hr_factor")  or 100
    if run_f >= 105 or hr_f >= 108:
        profile = "hitter-friendly"
    elif run_f <= 97 or hr_f <= 96:
        profile = "pitcher-friendly"
    else:
        profile = "neutral"

    ctx["park_profile"] = profile
    ctx["name"]         = venue_name or ctx.get("venue", "")

    # Provide a graceful fallback note when exact data is unavailable
    if ctx.get("source") == "neutral_fallback":
        ctx["fallback_note"] = (
            f"Detailed park factors unavailable. This stadium is generally considered "
            f"{profile} based on configured park tendencies."
        )
    else:
        ctx["fallback_note"] = None

    logger.debug(
        "prop park context: venue=%r  home_team=%r  source=%s  "
        "run=%s  hr=%s  hit=%s  lhb_hr=%s  rhb_hr=%s  "
        "dome=%s  retractable=%s  profile=%s",
        venue_name, home_team_abbr, ctx.get("source"),
        ctx.get("run_factor"), ctx.get("hr_factor"), ctx.get("hit_factor"),
        ctx.get("lhb_hr_factor"), ctx.get("rhb_hr_factor"),
        ctx.get("is_dome"), ctx.get("is_retractable"), profile,
    )
    return ctx


def _wind_label(deg: float | None) -> str | None:
    """Convert wind direction degrees to a human-readable label."""
    if deg is None:
        return None
    deg = deg % 360
    if deg < 22.5 or deg >= 337.5:
        return "in from center"
    if deg < 67.5:
        return "in from right-center"
    if deg < 112.5:
        return "cross from right"
    if deg < 157.5:
        return "out to right-center"
    if deg < 202.5:
        return "out to center"
    if deg < 247.5:
        return "out to left-center"
    if deg < 292.5:
        return "cross from left"
    return "in from left-center"


def _any_real(d: dict, keys: list[str]) -> bool:
    """Return True if any key has a non-null, non-zero value."""
    for k in keys:
        v = d.get(k)
        if v is not None and v != 0 and v != 0.0:
            return True
    return False


def _f(d: dict, key: str) -> float | None:
    """Safe float extraction — returns None on missing/zero/invalid."""
    v = d.get(key)
    if v is None:
        return None
    try:
        fv = float(v)
        return fv if fv != 0.0 else None
    except (TypeError, ValueError):
        return None


def _generate_matchup_notes(arsenal: dict, batter_splits: dict) -> list[str]:
    """Generate structured pitch-matchup notes for the context JSON.

    Notes are factual — only generated when supporting data exists.
    Each note is one plain English sentence suitable for display or Gemini input.
    """
    notes: list[str] = []
    pitches = arsenal.get("pitches") or []
    splits  = batter_splits.get("splits") or []

    # ── Pitcher arsenal summary ────────────────────────────────────────────────
    top = [p for p in pitches if p.get("usage_pct", 0) >= 10][:4]
    if top:
        summary = ", ".join(
            f"{p['label']} ({p['usage_pct']}%)" for p in top
        )
        notes.append(f"Pitch mix: {summary}")

    # ── Primary pitch detail ───────────────────────────────────────────────────
    if pitches:
        p = pitches[0]
        parts = [f"{p['usage_pct']}% usage"]
        if p.get("avg_velocity"):
            parts.append(f"{p['avg_velocity']} mph avg")
        if p.get("whiff_pct"):
            parts.append(f"{p['whiff_pct']}% whiff rate")
        notes.append(f"Pitcher's primary: {p['label']} — {', '.join(parts)}")

    if not splits:
        return notes

    split_map = {s["pitch_type"]: s for s in splits}

    # ── Cross-reference pitcher primary vs batter weakness / strength ──────────
    if pitches:
        primary_pt = pitches[0]["pitch_type"]
        s = split_map.get(primary_pt)
        if s and s.get("pa", 0) >= 10:
            note_parts: list[str] = []
            if s.get("whiff_pct") and s["whiff_pct"] >= 25:
                note_parts.append(f"{s['whiff_pct']}% whiff rate")
            if s.get("ba") is not None and s["ba"] <= 0.200:
                note_parts.append(f".{int(s['ba'] * 1000):03d} BA")
            if note_parts:
                notes.append(
                    f"Batter struggles vs {pitches[0]['label']}: "
                    f"{', '.join(note_parts)} ({s['pa']} PA) — pitcher's primary pitch"
                )
            else:
                pos_parts: list[str] = []
                if s.get("ba") is not None and s["ba"] >= 0.300:
                    pos_parts.append(f".{int(s['ba'] * 1000):03d} BA")
                if s.get("hard_hit_pct") and s["hard_hit_pct"] >= 40:
                    pos_parts.append(f"{s['hard_hit_pct']}% hard-hit rate")
                if pos_parts:
                    notes.append(
                        f"Batter handles {pitches[0]['label']} well: "
                        f"{', '.join(pos_parts)} ({s['pa']} PA) — pitcher's primary pitch"
                    )

    # ── Batter's weakest pitch type overall (whiff) ───────────────────────────
    weak_candidates = [
        s for s in splits
        if s.get("whiff_pct") is not None and s.get("pa", 0) >= 10
    ]
    if weak_candidates:
        worst = max(weak_candidates, key=lambda x: x["whiff_pct"])
        if worst["whiff_pct"] >= 30:
            notes.append(
                f"Batter weakness: {worst['whiff_pct']}% whiff rate vs "
                f"{worst['label']} ({worst['pa']} PA)"
            )

    # ── Batter's strongest pitch type overall (BA) ────────────────────────────
    strong_candidates = [
        s for s in splits
        if s.get("ba") is not None and s.get("pa", 0) >= 10
    ]
    if strong_candidates:
        best = max(strong_candidates, key=lambda x: x["ba"])
        if best["ba"] >= 0.280:
            notes.append(
                f"Batter strength: .{int(best['ba'] * 1000):03d} BA vs "
                f"{best['label']} ({best['pa']} PA)"
            )

    return notes
