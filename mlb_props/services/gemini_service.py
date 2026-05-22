# services/gemini_service.py
# Gemini explanation layer — translates structured prop context into plain English.
#
# DESIGN RULES:
#   - Only called on explicit /explain requests. Never called automatically.
#   - Reads only the context dict produced by prop_context_service. Never invents stats.
#   - If Gemini is unavailable (missing creds, import error, API error), returns a safe
#     error dict — never raises, never breaks the app.
#   - Responses are cached 2h per (player_id, prop_type, date) to avoid redundant calls.
#
# SETUP (pick one):
#   Option A — API key (simplest, recommended):
#     pip install google-generativeai
#     Get a free key at aistudio.google.com → Get API Key
#     Add GEMINI_API_KEY=<key> to .env
#
#   Option B — Service account (no API key needed):
#     pip install google-generativeai google-auth
#     Place service account JSON at mlb_props/secrets/gemini-service-account.json
#     OR set GOOGLE_APPLICATION_CREDENTIALS in .env to the full path.
#     The service account needs roles/aiplatform.user on the project.

import datetime
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_CACHE_TTL_HOURS = 2.0

# ── Safety rules ───────────────────────────────────────────────────────────────
# Patterns scanned across every text field returned by Gemini.
# Two tiers:
#   _PROHIBITED_ALWAYS — banned in both prop and game responses
#   _PROHIBITED_GAME   — banned only in game responses (where we never supply odds)
#
# Each entry: (human_label, compiled_regex)
# Violations are logged at WARNING and surfaced in the result's safety_warnings list.
# Betting-language violations additionally set safety_flags["betting_language"] = True
# so the frontend can render a stronger disclaimer.

_PROHIBITED_ALWAYS: list[tuple[str, re.Pattern]] = [
    # ── Gambling / betting recommendations ─────────────────────────────────────
    # "bet" is common in baseball idiom ("bet on yourself"), so require it to be
    # clearly a recommendation context — adjacent to "the", numbers, or "over/under".
    ("betting language",        re.compile(r"\bbet(?:ting)?\s+(?:the|on\s+(?:over|under|home|away))\b", re.I)),
    ("betting recommendation",  re.compile(r"\bstrong\s+(?:play|pick|bet|value|side)\b", re.I)),
    ("betting recommendation",  re.compile(r"\bbest\s+bet\b", re.I)),
    ("betting recommendation",  re.compile(r"\btake\s+the\s+(?:over|under|home|away)\b", re.I)),
    ("betting recommendation",  re.compile(r"\bfade\s+(?:the|them|him|her)\b", re.I)),
    ("betting recommendation",  re.compile(r"\blay\s+(?:the|off)\b", re.I)),
    ("moneyline format",        re.compile(r"[+-]\d{3}\b")),          # e.g. +145 or -110
    # ── Categories never supplied — flag as potential hallucination ────────────
    ("injury data",             re.compile(r"\binjur(?:y|ies|ed|ing)\b", re.I)),
    ("injury data",             re.compile(r"\billness\b|\binjured\s+list\b", re.I)),
    ("bullpen data",            re.compile(r"\bbullpen\b", re.I)),
    ("reliever data",           re.compile(r"\breliev(?:er|ers|ing)\b", re.I)),
    ("closer data",             re.compile(r"\bcloser\s+(?:ERA|saves?|pitched)\b", re.I)),
]

_PROHIBITED_GAME: list[tuple[str, re.Pattern]] = [
    # Odds/lines are not provided in game context — never in our game context dict
    ("odds (not provided)",     re.compile(r"\bodds\b(?!\s+ratio|\s+and\s+ends)", re.I)),
    ("moneyline (not provided)",re.compile(r"\bmoneyline\b", re.I)),
    ("spread (not provided)",   re.compile(r"\bspread\b(?!\s+(?:the\s+)?ball|\s+out|\s+thin)", re.I)),
    ("total/over-under",        re.compile(r"\bover[/\-]under\b|\bO\/U\b", re.I)),
]

_BETTING_LABELS = {"betting language", "betting recommendation", "moneyline format"}

# Fallback chain tried in order when the primary model returns a 503/overload error.
# The primary model (from config) is always tried first; these kick in only on failure.
_FALLBACK_MODELS = [
    "gemini-2.5-flash-lite",
    "gemini-2.5-flash",
    "gemini-flash-latest",
]

# Default credentials path — resolved relative to this file's directory
_CREDS_DEFAULT = str(
    Path(__file__).parent.parent / "secrets" / "gemini-service-account.json"
)

# Lazy-initialised on first successful call; None until then.
# Storing the resolved model name alongside so logs always report what was used.
_vertex_model: Any = None
_active_model_name: str = ""

# Permanent error message set when init fails for a non-transient reason
# (missing packages, missing credentials file). Cleared on success.
_init_error_msg: str | None = None


# ── Public API ─────────────────────────────────────────────────────────────────

def explain_prop(
    ctx: dict,
    cache: Any = None,
    cache_key: str = "",
) -> dict:
    """Generate a plain-English Gemini explanation for a player prop.

    Args:
        ctx:       Full context dict from prop_context_service.build_prop_context().
        cache:     Cache instance — used to store/retrieve explanations (optional).
        cache_key: Key to store result under (e.g. "gemini_explain_12345_hit_2026-05-05").

    Returns:
        Dict with keys: available, explanation, confidence_summary,
        key_factors, risk_factors, sample_size_warnings, model, error.
        available=False + error set on any failure.
    """
    _empty = _make_empty()

    # ── Cache read ────────────────────────────────────────────────────────────
    if cache and cache_key:
        cached = cache.get(cache_key, ttl_hours=_CACHE_TTL_HOURS)
        if cached is not None:
            logger.info("Gemini prop cache HIT: %s", cache_key)
            return cached
        logger.debug("Gemini prop cache MISS: %s", cache_key)

    if not _init_gemini():
        return {**_empty, "model": _active_model_name or "unknown", "error": _init_error_msg or "Gemini not initialised"}

    from google.genai import types as _genai_types
    prompt = _build_prompt(ctx)

    # Debug dump — only writes files when GEMINI_DEBUG=true in .env
    player_id = (ctx.get("player") or {}).get("player_id", "unknown")
    prop_type = (ctx.get("prop") or {}).get("type", "unknown")
    _debug_dump(f"prop_{player_id}_{prop_type}", prompt, ctx)

    # Build ordered list of models to attempt: primary first, then fallbacks (no dupes).
    models_to_try = [_active_model_name]
    for m in _FALLBACK_MODELS:
        if m not in models_to_try:
            models_to_try.append(m)

    last_exc: Exception | None = None
    for model_name in models_to_try:
        try:
            t0 = time.perf_counter()
            response = _vertex_model.models.generate_content(
                model=model_name,
                contents=prompt,
                config=_genai_types.GenerateContentConfig(
                    temperature=0.2,
                    max_output_tokens=800,
                ),
            )
            latency = time.perf_counter() - t0

            if model_name != _active_model_name:
                logger.info(
                    "Gemini prop fallback succeeded — primary=%s  used=%s",
                    _active_model_name, model_name,
                )
            logger.info(
                "Gemini prop response — model=%s  latency=%.2fs  key=%s",
                model_name, latency, cache_key or "(no-cache)",
            )
            _log_token_usage(response, model_name)

            result = _parse_response(response.text.strip())
            result["available"] = True
            result["model"]     = model_name
            result["error"]     = None

            # Safety scan — logs violations, flags betting language
            avail = ctx.get("data_availability") or {}
            result = _check_response_safety(result, "prop", avail)

            if cache and cache_key:
                cache.set(cache_key, result)

            return result

        except Exception as exc:
            last_exc = exc
            if _is_overload_error(exc):
                logger.warning(
                    "Gemini prop model overloaded — model=%s  error=%s  trying next",
                    model_name, exc,
                )
                continue
            # Non-transient error (e.g. auth, bad request) — don't bother with fallbacks.
            logger.warning("Gemini prop explain failed (non-transient) — model=%s  error=%s", model_name, exc)
            return {**_empty, "error": str(exc)}

    logger.warning("All Gemini prop models unavailable. Last error: %s", last_exc)
    return {**_empty, "error": f"All models unavailable: {last_exc}"}


def explain_game(
    ctx: dict,
    cache: Any = None,
    cache_key: str = "",
    force: bool = False,
) -> dict:
    """Generate a plain-English Gemini breakdown for an individual game.

    Args:
        ctx:       Full context dict from game_context_service.build_game_context().
        cache:     Cache instance — optional, 2-hour TTL.
        cache_key: Cache key, e.g. "gemini_game_823865_2026-05-19".
        force:     If True, bypass and invalidate any cached entry, then regenerate.

    Returns:
        Dict with keys: available, summary, pitching_edge, offensive_edge,
        weather_park_impact, reasons_away_could_win, reasons_home_could_win,
        data_caveats, model, error, used_fallback_summary.
        available=False + error set on any failure.
    """
    _empty = _make_game_empty()

    # ── Cache read ────────────────────────────────────────────────────────────
    if cache and cache_key:
        if force:
            # Caller requested a fresh generation — nuke whatever is on disk.
            cache.invalidate(cache_key)
            logger.info("Gemini game cache BYPASSED (force=1): %s", cache_key)
        else:
            cached = cache.get(cache_key, ttl_hours=_CACHE_TTL_HOURS)
            if cached is not None:
                if _is_valid_game_response(cached):
                    logger.info("Gemini game cache HIT valid: %s", cache_key)
                    return cached
                else:
                    missing = [f for f in _REQUIRED_GAME_PROSE if not cached.get(f)]
                    logger.warning(
                        "Gemini game cache HIT invalidated — missing prose fields=%s  key=%s",
                        missing, cache_key,
                    )
                    cache.invalidate(cache_key)
            else:
                logger.debug("Gemini game cache MISS: %s", cache_key)

    if not _init_gemini():
        return {**_empty, "model": _active_model_name or "unknown", "error": _init_error_msg or "Gemini not initialised"}

    from google.genai import types as _genai_types
    prompt = _build_game_prompt(ctx)

    # Debug dump — only writes files when GEMINI_DEBUG=true in .env
    game_pk = ctx.get("game_pk", "unknown")
    _debug_dump(f"game_{game_pk}", prompt, ctx)

    models_to_try = [_active_model_name]
    for m in _FALLBACK_MODELS:
        if m not in models_to_try:
            models_to_try.append(m)

    last_exc: Exception | None = None
    for model_name in models_to_try:
        try:
            t0 = time.perf_counter()
            response = _vertex_model.models.generate_content(
                model=model_name,
                contents=prompt,
                config=_genai_types.GenerateContentConfig(
                    temperature=0.25,
                    max_output_tokens=1500,            # increased: avoid truncation
                    response_mime_type="application/json",  # force clean JSON
                ),
            )
            latency = time.perf_counter() - t0

            if model_name != _active_model_name:
                logger.info(
                    "Gemini game fallback succeeded — primary=%s  used=%s",
                    _active_model_name, model_name,
                )
            logger.info(
                "Gemini game response — model=%s  latency=%.2fs  game_pk=%s  key=%s",
                model_name, latency, game_pk, cache_key or "(no-cache)",
            )
            _log_token_usage(response, model_name)

            result = _parse_game_response(response.text.strip(), model_name)

            if result.get("_parse_failed"):
                # Multi-stage parser exhausted — build a deterministic local summary
                parse_error = result.get("_parse_error", "unknown")
                logger.warning(
                    "Gemini game JSON parse failed — activating local fallback  "
                    "model=%s  error=%s",
                    model_name, parse_error,
                )
                result = _build_local_fallback(ctx, model_name, parse_error)
            else:
                result["available"]            = True
                result["model"]                = model_name
                result["error"]                = None
                result.setdefault("used_fallback_summary", False)

            # Safety scan — logs violations, flags betting language
            avail = ctx.get("data_availability") or {}
            result = _check_response_safety(result, "game", avail)

            # Only cache responses that have all required prose fields filled in.
            # Malformed or fallback-only responses are not cached so the next
            # request gets a fresh Gemini attempt.
            if cache and cache_key:
                if _is_valid_game_response(result):
                    cache.set(cache_key, result)
                else:
                    missing = [f for f in _REQUIRED_GAME_PROSE if not result.get(f)]
                    logger.warning(
                        "Gemini game response NOT cached — missing fields=%s  key=%s",
                        missing, cache_key,
                    )

            return result

        except Exception as exc:
            last_exc = exc
            if _is_overload_error(exc):
                logger.warning(
                    "Gemini game model overloaded — model=%s  error=%s  trying next",
                    model_name, exc,
                )
                continue
            logger.warning("Gemini game explain failed (non-transient) — model=%s  error=%s", model_name, exc)
            return {**_empty, "error": str(exc)}

    logger.warning("All Gemini game models unavailable. Last error: %s", last_exc)
    return {**_empty, "error": f"All models unavailable: {last_exc}"}


def reset_for_testing() -> None:
    """Reset module-level singletons (for tests only)."""
    global _vertex_model, _active_model_name, _init_error_msg
    _vertex_model      = None
    _active_model_name = ""
    _init_error_msg    = None


# ── Helpers ────────────────────────────────────────────────────────────────────

def _is_overload_error(exc: Exception) -> bool:
    """Return True if the exception looks like a transient server-overload/rate-limit."""
    msg = str(exc).lower()
    return any(k in msg for k in (
        "503", "overloaded", "overload", "rate limit", "rate_limit",
        "quota", "resource exhausted", "429", "unavailable", "try again",
    ))


# ── Initialisation ─────────────────────────────────────────────────────────────

def _init_gemini() -> bool:
    """Lazy-init the Gemini model using google-generativeai SDK.

    Auth priority:
      1. GEMINI_API_KEY in config/env  → genai.configure(api_key=...)
      2. Service account JSON          → genai.configure(credentials=...)

    Uses google-generativeai (not vertexai SDK) to avoid Vertex AI publisher
    model endpoint requirements that need extra project-level activation.

    Returns True if the model is ready. Sets _init_error_msg on permanent
    failures (missing packages, missing creds). Transient errors don't set it
    so the next request retries.
    """
    global _vertex_model, _active_model_name, _init_error_msg

    if _vertex_model is not None:
        return True

    # Don't retry after a permanent failure (missing packages or missing creds file)
    if _init_error_msg and ("not found" in _init_error_msg or "not installed" in _init_error_msg):
        return False

    try:
        import sys
        sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
        import config

        model_name = config.GEMINI_MODEL or "gemini-1.5-flash"
        api_key    = config.GEMINI_API_KEY or ""
        creds_path = (
            config.GOOGLE_APPLICATION_CREDENTIALS
            or os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "")
            or _CREDS_DEFAULT
        )

        from google import genai

        if api_key:
            # ── API key path (Option A — recommended) ─────────────────────
            client = genai.Client(api_key=api_key)
            logger.info("Gemini init: auth=api_key  model=%s", model_name)
        else:
            # ── Service account path (Option B) ───────────────────────────
            if not os.path.exists(creds_path):
                _init_error_msg = (
                    f"No GEMINI_API_KEY set and service account file not found: {creds_path}. "
                    "Either add GEMINI_API_KEY to .env (get one free at aistudio.google.com) "
                    "or place the service account JSON at mlb_props/secrets/gemini-service-account.json"
                )
                logger.warning("Gemini unavailable: %s", _init_error_msg)
                return False

            from google.oauth2 import service_account
            credentials = service_account.Credentials.from_service_account_file(
                creds_path,
                scopes=["https://www.googleapis.com/auth/cloud-platform"],
            )
            client = genai.Client(credentials=credentials)
            logger.info(
                "Gemini init: auth=service_account  creds=%s  model=%s",
                creds_path, model_name,
            )

        _vertex_model      = client
        _active_model_name = model_name
        _init_error_msg    = None
        logger.info("Gemini service ready — model=%s", model_name)
        return True

    except ImportError as exc:
        _init_error_msg = (
            f"Required package not installed ({exc}). "
            "Run: pip install google-genai"
        )
        logger.warning("Gemini unavailable: %s", _init_error_msg)
        return False

    except Exception as exc:
        # Transient — don't permanently block retries
        logger.warning("Gemini init failed (will retry next request): %s", exc)
        return False


# ── Prompt builder ─────────────────────────────────────────────────────────────

def _build_prompt(ctx: dict) -> str:
    """Construct the Gemini prompt from a prop context dict.

    Only includes data sections that are present and non-null.
    Every absent section is explicitly labelled as unavailable.
    """
    player  = ctx.get("player") or {}
    prop    = ctx.get("prop") or {}
    batter  = ctx.get("batter_season") or {}
    pitcher = ctx.get("pitcher") or {}
    bvp     = ctx.get("bvp") or {}
    arsenal = ctx.get("pitch_arsenal") or {}
    avail   = ctx.get("data_availability") or {}
    context = ctx.get("context") or {}
    odds    = prop.get("odds") or {}
    notes   = ctx.get("pitch_matchup_notes") or []

    name       = player.get("name", "Unknown")
    team       = player.get("team", "Unknown")
    hand       = player.get("hand", "?")
    lineup_pos = player.get("lineup_position")
    prop_type  = prop.get("type", "hit")
    prob       = prop.get("model_probability")
    verdict    = prop.get("verdict", "UNKNOWN")
    lineup_st  = prop.get("lineup_status", "unknown")

    prob_str = f"{prob:.1%}" if prob is not None else "N/A"

    lines: list[str] = [
        "You are a baseball analyst explaining a statistical model's probability for a player prop.",
        "",
        "CRITICAL RULES — violations will be flagged and logged:",
        "1. Only cite statistics explicitly provided in this prompt. Do NOT invent, estimate, or assume any number.",
        "2. When a section is marked unavailable, write 'unavailable' — do NOT infer or substitute.",
        "3. NEVER recommend, suggest, or imply a bet, wager, or gambling action of any kind.",
        "4. NEVER mention injuries, illness, the injured list, or player health — this data is not provided.",
        "5. NEVER mention bullpen ERA, reliever stats, closer performance, or setup-man data — not provided.",
        "6. NEVER reference odds, moneylines, spreads, over/unders, or sportsbook lines — not provided.",
        "7. Be concise: explanation 3-5 sentences, lists max 4 items each.",
        "8. Return ONLY valid JSON matching the exact schema shown at the bottom. No markdown fences.",
        "",
        "=" * 60,
        f"PLAYER:  {name} ({team}, bats {hand}, lineup position #{lineup_pos or '?'})",
        f"PROP:    {prop_type.upper()} — Model probability: {prob_str}  |  Verdict: {verdict}",
        f"LINEUP:  {lineup_st}",
    ]

    # ── Batter season stats ───────────────────────────────────────────────────
    lines.append("")
    if avail.get("batter_savant"):
        lines.append("BATTER SEASON STATS (Baseball Savant):")
        _stat(lines, "Batting avg",           batter.get("avg"),                fmt=".3f")
        _stat(lines, "xBA (expected BA)",     batter.get("xba"),                fmt=".3f")
        _stat(lines, "OBP",                   batter.get("obp"),                fmt=".3f")
        _stat(lines, "wOBA",                  batter.get("woba"),               fmt=".3f")
        _stat(lines, "xwOBA",                 batter.get("xwoba"),              fmt=".3f")
        _stat(lines, "Hard-hit%",             batter.get("hard_hit_pct"),       fmt="pct")
        _stat(lines, "Barrel%",               batter.get("barrel_pct"),         fmt="pct")
        _stat(lines, "Sweet-spot%",           batter.get("sweet_spot_pct"),     fmt="pct")
        _stat(lines, "Avg exit velocity",     batter.get("exit_velocity"),      fmt=".1f", unit=" mph")
        _stat(lines, "EV50",                  batter.get("ev50"),               fmt=".1f", unit=" mph")
        _stat(lines, "Whiff%",                batter.get("whiff_pct"),          fmt="pct")
        _stat(lines, "K%",                    batter.get("k_pct"),              fmt="pct")
        _stat(lines, "BB%",                   batter.get("bb_pct"),             fmt="pct")
        _stat(lines, "HR/FB ratio",           batter.get("hr_fb_ratio"),        fmt="pct")
        _stat(lines, "Recent avg (14 days)",  batter.get("recent_avg"),         fmt=".3f")
        _stat(lines, "Recent hard-hit (14d)", batter.get("recent_hard_hit_pct"),fmt="pct")
    else:
        lines.append("BATTER SEASON STATS: unavailable — no Statcast data loaded")

    # ── Pitcher stats ─────────────────────────────────────────────────────────
    lines.append("")
    p_name = pitcher.get("name", "Unknown")
    p_hand = pitcher.get("hand", "?")
    if avail.get("pitcher_savant"):
        lines.append(f"OPPOSING PITCHER: {p_name} ({p_hand}HP)")
        _stat(lines, "ERA",                   pitcher.get("era"),                   fmt=".2f")
        _stat(lines, "xERA",                  pitcher.get("xera"),                  fmt=".2f")
        _stat(lines, "FIP",                   pitcher.get("fip"),                   fmt=".2f")
        _stat(lines, "WHIP",                  pitcher.get("whip"),                  fmt=".2f")
        _stat(lines, "K%",                    pitcher.get("k_pct"),                 fmt="pct")
        _stat(lines, "BB%",                   pitcher.get("bb_pct"),                fmt="pct")
        _stat(lines, "Hard-hit% allowed",     pitcher.get("hard_hit_pct_allowed"),  fmt="pct")
        _stat(lines, "Barrel% allowed",       pitcher.get("barrel_pct_allowed"),    fmt="pct")
        _stat(lines, "Avg exit velo allowed", pitcher.get("avg_exit_velo_allowed"), fmt=".1f", unit=" mph")
        _stat(lines, "xwOBA allowed",         pitcher.get("xwoba_allowed"),         fmt=".3f")
        _stat(lines, "Whiff% generated",      pitcher.get("whiff_pct_generated"),   fmt="pct")
        _stat(lines, "HR/9",                  pitcher.get("hr9"),                   fmt=".2f")
    else:
        lines.append(f"OPPOSING PITCHER: {p_name} ({p_hand}HP) — season stats unavailable")

    # ── BvP history ───────────────────────────────────────────────────────────
    lines.append("")
    if avail.get("bvp_history") and bvp.get("available"):
        ab   = bvp.get("ab", 0)
        hits = bvp.get("hits", 0)
        hr   = bvp.get("hr", 0)
        bvp_avg = bvp.get("avg")
        warn = bvp.get("warning")
        lines.append(
            f"BATTER vs THIS PITCHER (career): {ab} AB, {hits} H, {hr} HR, "
            f"avg {bvp_avg or 'N/A'}"
        )
        if warn:
            lines.append(f"  [Sample warning: {warn}]")
    else:
        lines.append("BATTER vs THIS PITCHER: no career matchup data available")

    # ── Pitch arsenal / matchup notes ─────────────────────────────────────────
    lines.append("")
    if notes:
        lines.append("PITCH MATCHUP NOTES:")
        for note in notes:
            lines.append(f"  - {note}")
    elif avail.get("pitch_arsenal"):
        primary = arsenal.get("primary_pitch")
        lines.append(f"PITCH ARSENAL: data available (primary pitch: {primary})")
    else:
        lines.append("PITCH ARSENAL / MATCHUP DATA: unavailable")

    # ── Game context ──────────────────────────────────────────────────────────
    lines.append("")
    lines.append("GAME CONTEXT:")
    venue   = context.get("venue") or "Unknown"
    park    = context.get("park") or {}
    weather = context.get("weather") or {}

    lines.append(f"  Venue: {venue}")
    if park.get("available"):
        lines.append(
            f"  Park factors — hit: {park.get('hit_factor')}, HR: {park.get('hr_factor')} "
            "(100 = league average)"
        )
    else:
        lines.append("  Park factors: unavailable")

    if weather.get("temp_f") is not None:
        wind_lbl = weather.get("wind_direction_label") or ""
        lines.append(
            f"  Weather: {weather['temp_f']}°F, "
            f"wind {weather.get('wind_speed_mph', '?')} mph {wind_lbl}".rstrip()
        )
        if weather.get("is_dome"):
            lines.append("  (dome stadium — weather does not affect play)")
    else:
        lines.append("  Weather: unavailable")

    # ── Odds ──────────────────────────────────────────────────────────────────
    lines.append("")
    if avail.get("odds") and odds.get("available"):
        implied = odds.get("implied_probability")
        edge    = odds.get("edge")
        sbook   = odds.get("sportsbook_odds") or "N/A"
        book    = odds.get("best_book") or "unknown book"
        if implied is not None and edge is not None:
            lines.append(
                f"SPORTSBOOK ODDS: {sbook} ({book}), implied prob {implied:.1%}, "
                f"model edge {edge:+.1%}"
            )
        else:
            lines.append(f"SPORTSBOOK ODDS: {sbook} ({book})")
    else:
        lines.append("SPORTSBOOK ODDS: not available for this prop")

    # ── Output schema ─────────────────────────────────────────────────────────
    lines += [
        "",
        "=" * 60,
        "Respond with exactly this JSON structure (no markdown, no extra keys):",
        "{",
        '  "explanation": "3 to 5 sentences explaining the key drivers of the model probability. '
        'Reference specific stats from the data above.",',
        '  "confidence_summary": "One sentence on overall data quality and confidence level.",',
        '  "key_factors": ["most important factor", "second factor", "third factor"],',
        '  "risk_factors": ["main concern", "second concern"],',
        '  "sample_size_warnings": ["any BvP or recent-form sample too small to be reliable"]',
        "}",
    ]

    return "\n".join(lines)


def _stat(lines: list, label: str, val: Any, fmt: str = "", unit: str = "") -> None:
    """Append a formatted stat line only when value is non-None."""
    if val is None:
        return
    try:
        if fmt == "pct":
            formatted = f"{float(val):.1%}"
        elif fmt:
            formatted = format(float(val), fmt)
        else:
            formatted = str(val)
        lines.append(f"  {label}: {formatted}{unit}")
    except (TypeError, ValueError):
        lines.append(f"  {label}: {val}{unit}")


# ── Response parser ────────────────────────────────────────────────────────────

def _parse_response(text: str) -> dict:
    """Parse Gemini JSON response. Falls back gracefully on malformed output."""
    fallback = {
        "explanation":          None,
        "confidence_summary":   None,
        "key_factors":          [],
        "risk_factors":         [],
        "sample_size_warnings": [],
    }
    try:
        clean = text.strip()

        # Strip markdown fences: ```json\n...\n``` or ```\n...\n```
        # (The old split("```",2)[-1] approach returned the empty tail — this fixes it.)
        if clean.startswith("```"):
            clean = re.sub(r'^```[a-zA-Z]*\s*', '', clean)   # remove opening fence + lang tag
            clean = re.sub(r'\s*```\s*$', '', clean).strip()  # remove closing fence

        # If there's preamble text before the JSON object, skip ahead to it
        if not clean.startswith('{'):
            m = re.search(r'\{[\s\S]*\}', clean)
            if m:
                clean = m.group(0)

        data = json.loads(clean)
        return {
            "explanation":          data.get("explanation"),
            "confidence_summary":   data.get("confidence_summary"),
            "key_factors":          list(data.get("key_factors") or []),
            "risk_factors":         list(data.get("risk_factors") or []),
            "sample_size_warnings": list(data.get("sample_size_warnings") or []),
        }
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning("Gemini response parse failed: %s | raw: %.200s", exc, text)
        # Regex last-resort: extract the explanation string field directly
        m = re.search(r'"explanation"\s*:\s*"((?:[^"\\]|\\.)*)"', text or "")
        expl = m.group(1).replace('\\n', '\n').replace('\\"', '"').strip() if m else None
        return {**fallback, "explanation": expl}


def _debug_dump(tag: str, prompt: str, ctx: dict) -> None:
    """Write prompt text + structured context JSON to disk when GEMINI_DEBUG=true.

    Files are written to config.GEMINI_DEBUG_DIR with a timestamp prefix so each
    request gets its own files and nothing is overwritten.  Production should keep
    GEMINI_DEBUG=false (default) — dumps can contain player/game data.

    Args:
        tag:    Short identifier, e.g. "prop_12345_hit" or "game_823865".
        prompt: The full prompt string sent to the API.
        ctx:    The structured context dict (from prop/game context services).
    """
    try:
        # _init_gemini already wired sys.path; config should be importable.
        import config as _cfg
        if not _cfg.GEMINI_DEBUG:
            return
        os.makedirs(_cfg.GEMINI_DEBUG_DIR, exist_ok=True)
        ts   = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:21]
        base = os.path.join(_cfg.GEMINI_DEBUG_DIR, f"{tag}_{ts}")
        with open(base + "_prompt.txt", "w", encoding="utf-8") as fh:
            fh.write(prompt)
        with open(base + "_context.json", "w", encoding="utf-8") as fh:
            json.dump(ctx, fh, indent=2, default=str)
        logger.debug("Gemini debug dump written: %s_{prompt,context}.*", base)
    except Exception as exc:
        logger.debug("Gemini debug dump failed (non-critical): %s", exc)


def _check_response_safety(result: dict, ctx_type: str, avail: dict) -> dict:
    """Scan all text fields in a Gemini result for prohibited content.

    Violations are logged at WARNING. Betting-language violations are surfaced to
    the caller via ``safety_flags["betting_language"] = True`` so the frontend can
    render a stronger disclaimer.  All violations accumulate in ``safety_warnings``
    (developer-facing, not shown in UI) and notable ones are appended to
    ``data_caveats`` (user-facing footnote in the breakdown card).

    Args:
        result:   Parsed Gemini response dict — modified **in-place** and returned.
        ctx_type: ``'prop'`` or ``'game'`` — selects which rule set applies.
        avail:    data_availability dict from the context service.

    Returns:
        The (mutated) result dict with ``safety_warnings`` and ``safety_flags`` added.
    """
    rules = list(_PROHIBITED_ALWAYS)
    if ctx_type == "game":
        rules.extend(_PROHIBITED_GAME)

    # Conditionally add win-loss hallucination check when standings were not supplied
    if not avail.get("standings", True):
        rules.append((
            "invented win-loss record",
            re.compile(r"\b\d{1,3}-\d{1,3}\b"),   # e.g. "22-18"
        ))

    # Collect prose text fields for scanning.
    # Exclude metadata keys (available, model, error) — only scan AI-generated content.
    _META_KEYS = {"available", "model", "error", "safety_warnings", "safety_flags"}
    text_fields: dict[str, str] = {}
    for key, val in result.items():
        if key in _META_KEYS:
            continue
        if isinstance(val, str) and val:
            text_fields[key] = val
        elif isinstance(val, list):
            for i, item in enumerate(val):
                if isinstance(item, str) and item:
                    text_fields[f"{key}[{i}]"] = item

    warnings: list[str] = []
    betting_hit = False

    for label, pattern in rules:
        for field_name, text in text_fields.items():
            m = pattern.search(text)
            if m:
                snippet = text[max(0, m.start() - 25): m.end() + 25].replace("\n", " ")
                msg = f"[{label}] in '{field_name}': …{snippet}…"
                logger.warning("Gemini safety violation — %s", msg)
                warnings.append(msg)
                if label in _BETTING_LABELS:
                    betting_hit = True

    result["safety_warnings"] = warnings
    result["safety_flags"]    = {"betting_language": betting_hit}

    # Append a user-visible caveat when betting language slipped through
    if betting_hit:
        caveats = result.get("data_caveats") or result.get("sample_size_warnings") or []
        note = "AI response flagged for possible betting language — treat as informational only."
        if note not in caveats:
            caveats_key = "data_caveats" if "data_caveats" in result else "sample_size_warnings"
            result[caveats_key] = list(result.get(caveats_key) or []) + [note]

    if warnings:
        logger.warning(
            "Gemini safety check: %d violation(s) in %s response (betting_language=%s)",
            len(warnings), ctx_type, betting_hit,
        )
    else:
        logger.debug("Gemini safety check: clean (%s)", ctx_type)

    return result


def _log_token_usage(response: Any, model_name: str) -> None:
    """Log token counts from a Gemini response object if the SDK exposes them."""
    try:
        usage = getattr(response, "usage_metadata", None)
        if usage is None:
            return
        prompt_tok    = getattr(usage, "prompt_token_count",     "?")
        candidate_tok = getattr(usage, "candidates_token_count", "?")
        total_tok     = getattr(usage, "total_token_count",      "?")
        logger.info(
            "Gemini tokens — model=%s  prompt=%s  candidates=%s  total=%s",
            model_name, prompt_tok, candidate_tok, total_tok,
        )
    except Exception:
        pass  # token logging is best-effort, never break the request


def _make_empty() -> dict:
    return {
        "available":            False,
        "explanation":          None,
        "confidence_summary":   None,
        "key_factors":          [],
        "risk_factors":         [],
        "sample_size_warnings": [],
        "model":                _active_model_name or "not initialised",
        "error":                None,
    }


def _make_game_empty() -> dict:
    return {
        "available":               False,
        "summary":                 None,
        "pitching_edge":           None,
        "offensive_edge":          None,
        "weather_park_impact":     None,
        "reasons_away_could_win":  [],
        "reasons_home_could_win":  [],
        "data_caveats":            [],
        "model":                   _active_model_name or "not initialised",
        "error":                   None,
        "used_fallback_summary":   False,
    }


# Prose fields that must all be non-empty for a game response to be considered valid.
# Used to gate both caching (never write bad entries) and cache reads (invalidate stale/empty ones).
_REQUIRED_GAME_PROSE = ("summary", "pitching_edge", "offensive_edge", "weather_park_impact")


def _is_valid_game_response(resp: dict) -> bool:
    """Return True if a game breakdown response is cacheable / usable.

    Three conditions must all hold:
      1. available=True  — Gemini call succeeded (no auth/API error)
      2. used_fallback_summary is falsy  — real AI prose, not local synthetic fallback
      3. All four core prose fields are non-empty strings

    Responses that fail this check are not cached so the next request gets a fresh
    Gemini attempt. This covers:
      - Auth/overload failures (available=False)
      - Truncated/malformed JSON where we fell back to local data (used_fallback_summary=True)
      - Partial parses where some fields are None/empty
    """
    if not (isinstance(resp, dict) and resp.get("available")):
        return False
    if resp.get("used_fallback_summary"):
        return False
    return all(isinstance(resp.get(f), str) and resp.get(f) for f in _REQUIRED_GAME_PROSE)


def _build_local_fallback(ctx: dict, model_name: str, parse_error: str = "unknown") -> dict:
    """Build a deterministic game summary from structured context data.

    Called when Gemini returns malformed JSON that survives all parse stages.
    Uses ctx.edges, pitcher names/xERA, offense aggregates, and weather/park
    data — the same structured data that backs the edge cards — so the result
    is always factually grounded even without usable Gemini prose.

    Returns a full response dict (available=True, used_fallback_summary=True).
    """
    home      = ctx.get("home_team")     or {}
    away      = ctx.get("away_team")     or {}
    hp        = ctx.get("home_pitcher")  or {}
    ap        = ctx.get("away_pitcher")  or {}
    home_off  = ctx.get("home_offense")  or {}
    away_off  = ctx.get("away_offense")  or {}
    weather   = ctx.get("weather")       or {}
    park      = ctx.get("park")          or {}
    edges     = ctx.get("edges")         or {}
    avail     = ctx.get("data_availability") or {}

    home_abbr = home.get("abbreviation", "Home")
    away_abbr = away.get("abbreviation", "Away")
    home_name = home.get("name", "Home Team")
    away_name = away.get("name", "Away Team")
    hp_name   = hp.get("name", "TBD")
    ap_name   = ap.get("name", "TBD")

    pitch_edge = edges.get("starting_pitching_edge", "neutral")
    off_edge   = edges.get("offensive_projection_edge", "neutral")
    wp_edge    = edges.get("weather_park_edge", "neutral")

    # ── Summary ───────────────────────────────────────────────────────────────
    parts = [f"{away_abbr} visits {home_abbr}."]
    if hp_name != "TBD" and ap_name != "TBD":
        parts.append(f"The model projects {ap_name} ({away_abbr}) against {hp_name} ({home_abbr}).")
    elif hp_name != "TBD":
        parts.append(f"{home_abbr} starts {hp_name}.")
    elif ap_name != "TBD":
        parts.append(f"{away_abbr} sends {ap_name} to the mound.")

    if pitch_edge in ("home", "away"):
        edge_team = home_name if pitch_edge == "home" else away_name
        parts.append(f"The pitching model gives a slight edge to {edge_team}.")
    elif off_edge in ("home", "away"):
        edge_team = home_name if off_edge == "home" else away_name
        parts.append(f"The offensive projection model slightly favors {edge_team}.")
    else:
        parts.append("The model projects this as a relatively even matchup.")
    summary = " ".join(parts)

    # ── Pitching edge ─────────────────────────────────────────────────────────
    hp_xera = hp.get("xera")
    ap_xera = ap.get("xera")
    if avail.get("pitchers") and hp_xera is not None and ap_xera is not None:
        if pitch_edge == "home":
            pitching_edge = (
                f"The model gives {home_abbr} the pitching edge. "
                f"{hp_name} (xERA {hp_xera:.2f}) projects better than "
                f"{ap_name} (xERA {ap_xera:.2f}) by predictive metrics."
            )
        elif pitch_edge == "away":
            pitching_edge = (
                f"The model gives {away_abbr} the pitching edge. "
                f"{ap_name} (xERA {ap_xera:.2f}) projects better than "
                f"{hp_name} (xERA {hp_xera:.2f}) by predictive metrics."
            )
        else:
            pitching_edge = (
                f"The model projects a neutral or mixed pitching matchup — "
                f"{ap_name} (xERA {ap_xera:.2f}) and {hp_name} (xERA {hp_xera:.2f}) "
                "are close in predictive metrics."
            )
    elif avail.get("pitchers"):
        pitching_edge = f"Pitching edge: {pitch_edge}. Detailed xERA values unavailable."
    else:
        pitching_edge = "Pitcher data unavailable — pitching comparison could not be computed."

    # ── Offensive edge ────────────────────────────────────────────────────────
    home_yes = home_off.get("yes_count") or 0
    away_yes = away_off.get("yes_count") or 0
    home_avg = home_off.get("avg_hit_prob")
    away_avg = away_off.get("avg_hit_prob")

    home_avail = home_off.get("available")
    away_avail = away_off.get("available")

    if home_avail and away_avail:
        ha_str = f"{home_avg:.0%}" if home_avg is not None else "N/A"
        aa_str = f"{away_avg:.0%}" if away_avg is not None else "N/A"
        if off_edge == "home":
            offensive_edge = (
                f"{home_abbr} holds the offensive edge with {home_yes} YES-verdict hitters "
                f"(avg hit prob {ha_str}) vs {away_yes} for {away_abbr} ({aa_str})."
            )
        elif off_edge == "away":
            offensive_edge = (
                f"{away_abbr} holds the offensive edge with {away_yes} YES-verdict hitters "
                f"(avg hit prob {aa_str}) vs {home_yes} for {home_abbr} ({ha_str})."
            )
        else:
            offensive_edge = (
                f"Offenses project closely — {home_abbr} has {home_yes} YES picks, "
                f"{away_abbr} has {away_yes}. No clear offensive edge from the model."
            )
    else:
        offensive_edge = "Offensive projection data unavailable."

    # ── Weather / park impact ─────────────────────────────────────────────────
    if weather.get("is_dome"):
        weather_park_impact = "Indoor stadium — weather has little to no impact on scoring."
    elif weather.get("temp_f") is not None:
        temp = weather["temp_f"]
        wind = weather.get("wind_speed_mph") or 0
        if wp_edge == "hitter_friendly":
            weather_park_impact = (
                f"Conditions ({temp:.0f}°F, {wind:.0f} mph wind) and park factors "
                "project as hitter-friendly."
            )
        elif wp_edge == "pitcher_friendly":
            weather_park_impact = (
                f"Conditions ({temp:.0f}°F, {wind:.0f} mph wind) and park factors "
                "project as pitcher-friendly."
            )
        else:
            weather_park_impact = (
                f"Weather ({temp:.0f}°F, {wind:.0f} mph wind) projects as "
                "neutral for scoring."
            )
    elif park.get("available"):
        hf  = park.get("hit_factor") or 100
        hrf = park.get("hr_factor")  or 100
        weather_park_impact = (
            f"Weather unavailable. Park factors: hit {hf:.0f}, HR {hrf:.0f} "
            "(100 = league average)."
        )
    else:
        weather_park_impact = "Weather and park factor data unavailable."

    # ── Win scenarios ─────────────────────────────────────────────────────────
    reasons_away: list[str] = []
    reasons_home: list[str] = []

    if away_avail and away_yes > home_yes:
        reasons_away.append(
            f"{away_abbr} has more YES-verdict hitters ({away_yes} vs {home_yes})."
        )
    if pitch_edge == "away" and ap_name != "TBD":
        reasons_away.append(f"{ap_name} projects as the stronger starter by xERA.")
    if off_edge == "away":
        reasons_away.append(f"The offensive projection model favors {away_abbr}.")
    if not reasons_away:
        reasons_away.append(
            f"The model does not give {away_abbr} a clear statistical edge today."
        )

    if home_avail and home_yes > away_yes:
        reasons_home.append(
            f"{home_abbr} has more YES-verdict hitters ({home_yes} vs {away_yes})."
        )
    if pitch_edge == "home" and hp_name != "TBD":
        reasons_home.append(f"{hp_name} projects as the stronger starter by xERA.")
    if off_edge == "home":
        reasons_home.append(f"The offensive projection model favors {home_abbr}.")
    if not reasons_home:
        reasons_home.append(
            f"The model does not give {home_abbr} a clear statistical edge today."
        )

    # ── Data caveats ──────────────────────────────────────────────────────────
    caveats = [
        "Gemini response was malformed; displaying structured fallback summary."
    ]
    if not avail.get("standings"):
        caveats.append("Team records and streaks were unavailable.")
    if not avail.get("pitchers"):
        caveats.append("Starting pitcher stats were unavailable.")
    if not avail.get("home_lineup") or not avail.get("away_lineup"):
        caveats.append("Some lineup projection data was unavailable.")

    logger.warning(
        "Gemini game local fallback built — model=%s  parse_error=%s",
        model_name, parse_error,
    )

    return {
        "available":               True,
        "summary":                 summary,
        "pitching_edge":           pitching_edge,
        "offensive_edge":          offensive_edge,
        "weather_park_impact":     weather_park_impact,
        "reasons_away_could_win":  reasons_away,
        "reasons_home_could_win":  reasons_home,
        "data_caveats":            caveats,
        "model":                   model_name,
        "error":                   None,
        "used_fallback_summary":   True,
    }


# ── Game prompt builder ────────────────────────────────────────────────────────

def _build_game_prompt(ctx: dict) -> str:
    """Build the Gemini prompt for an AI Game Breakdown."""
    home = ctx.get("home_team") or {}
    away = ctx.get("away_team") or {}
    hp   = ctx.get("home_pitcher") or {}
    ap   = ctx.get("away_pitcher") or {}
    home_off = ctx.get("home_offense") or {}
    away_off = ctx.get("away_offense") or {}
    weather  = ctx.get("weather") or {}
    park     = ctx.get("park") or {}
    avail    = ctx.get("data_availability") or {}
    edges    = ctx.get("edges") or {}
    kp       = ctx.get("key_players") or {}

    home_name  = home.get("name", "Home Team")
    away_name  = away.get("name", "Away Team")
    home_abbr  = home.get("abbreviation", "HM")
    away_abbr  = away.get("abbreviation", "AW")
    venue      = ctx.get("venue", "Unknown Venue")

    lines: list[str] = [
        "You are a baseball analyst providing a pre-game breakdown based strictly on structured model data.",
        "",
        "CRITICAL RULES — violations will be flagged and logged:",
        "0. Do NOT list or restate the raw statistics. Instead, explain what they mean for this specific matchup.",
        "0b. Synthesize — e.g. instead of 'Pitcher A has a 3.21 xERA', write 'Pitcher A generates elite whiff rates and keeps barrels low, suggesting strong swing-and-miss stuff'.",
        "0c. Reference players by name from the KEY PLAYERS section if helpful, but do NOT invent stats for them.",
        "1. Only cite statistics explicitly provided in this prompt. Do NOT invent, estimate, or assume any number.",
        "2. When a section is marked unavailable, write 'unavailable' — do NOT infer, guess, or use prior knowledge.",
        "3. NEVER recommend, suggest, or imply a bet, wager, gambling action, or side to back.",
        "4. NEVER mention injuries, illness, the injured list, or player availability — not provided.",
        "5. NEVER mention bullpen ERAs, reliever stats, closer performance, or setup arms — not provided.",
        "6. NEVER reference odds, moneylines, spreads, totals, over/unders, or sportsbook lines — not provided.",
        "7. NEVER cite team OPS, runs per game, or other team-aggregate stats not supplied below.",
        "8. If standings are marked unavailable, do NOT state or estimate any win-loss records.",
        "9. Be concise and analytical. Each JSON field: 2-3 sentences max.",
        "10. Explain both teams fairly — do not simply favour one side.",
        "11. Return ONLY valid JSON matching the exact schema shown at the bottom. No markdown fences.",
        "",
        "=" * 60,
        f"MATCHUP:  {away_name} ({away_abbr}) @ {home_name} ({home_abbr})",
        f"VENUE:    {venue}",
        f"DATE:     {ctx.get('date', '')}",
        f"STATUS:   {ctx.get('status', 'scheduled')}",
    ]

    # ── Pre-computed edge labels ──────────────────────────────────────────────
    lines += [
        "",
        "PRE-COMPUTED EDGE LABELS (model-computed from structured data — use as guidance, not absolute):",
        f"  Starting pitching edge: {edges.get('starting_pitching_edge', 'unknown')}",
        f"  Offensive projection edge: {edges.get('offensive_projection_edge', 'unknown')}",
        f"  Venue/weather edge: {edges.get('weather_park_edge', 'unknown')}",
        f"  Data confidence: {edges.get('confidence_tier', 'unknown')}",
    ]

    # ── Team Records ──────────────────────────────────────────────────────────
    lines.append("")
    if avail.get("standings"):
        lines.append("TEAM RECORDS (season standings):")

        def _record_line(team: dict, label: str) -> str:
            w, l = team.get("wins"), team.get("losses")
            pct  = team.get("win_pct", "")
            stk  = team.get("streak", "")
            hw, hl = team.get("home_wins"), team.get("home_losses")
            aw, al = team.get("away_wins"), team.get("away_losses")
            parts = [f"{label}: {w}-{l} ({pct})"]
            if stk:
                parts.append(f"streak {stk}")
            if hw is not None:
                parts.append(f"home {hw}-{hl}")
            if aw is not None:
                parts.append(f"away {aw}-{al}")
            return "  " + ", ".join(parts)

        lines.append(_record_line(away, away_abbr))
        lines.append(_record_line(home, home_abbr))
    else:
        lines.append("TEAM RECORDS: unavailable")

    # ── Pitching Matchup (condensed to 4 most diagnostic stats) ───────────────
    lines.append("")
    if avail.get("pitchers"):
        lines.append("PITCHING MATCHUP:")
        for label, p in [(away_abbr, ap), (home_abbr, hp)]:
            pname = p.get("name", "TBD")
            phand = p.get("hand", "R")
            lines.append(f"  {label} — {pname} ({phand}HP):")
            _stat(lines, "xERA (predictive ERA)",          p.get("xera"),                fmt=".2f")
            _stat(lines, "WHIP (baserunners per inning)",  p.get("whip"),                fmt=".2f")
            _stat(lines, "Whiff% generated",               p.get("whiff_pct_generated"), fmt="pct")
            _stat(lines, "Hard-hit% allowed",              p.get("hard_hit_pct_allowed"), fmt="pct")
    else:
        lines.append("PITCHING MATCHUP: pitcher data unavailable")

    # ── Offensive Projections (condensed) ─────────────────────────────────────
    lines.append("")
    for label, abbr, off in [(away_name, away_abbr, away_off), (home_name, home_abbr, home_off)]:
        if off.get("available"):
            n    = off.get("player_count", 0)
            yes  = off.get("yes_count", 0)
            lean = off.get("lean_count", 0)
            no   = off.get("no_count", 0)
            lines.append(f"OFFENSE — {abbr} ({n} projected batters):")
            _stat(lines, "Avg hit probability", off.get("avg_hit_prob"),      fmt=".1%")
            _stat(lines, "Avg xBA",             off.get("avg_xba"),           fmt=".3f")
            _stat(lines, "Avg barrel%",         off.get("avg_barrel_pct"),    fmt="pct")
            lines.append(f"  Model verdicts: {yes} YES / {yes + lean + no} total")

            top_hits = off.get("top_hit_picks") or []
            if top_hits:
                picks = ", ".join(
                    f"{p['name']} ({p['hit_prob']:.0%} / {p['verdict']})"
                    for p in top_hits
                )
                lines.append(f"  Top hit picks: {picks}")
        else:
            lines.append(f"OFFENSE — {abbr}: lineup data unavailable")
        lines.append("")

    # ── Key Players section ───────────────────────────────────────────────────
    for abbr, team_kp in [(away_abbr, kp.get("away") or {}), (home_abbr, kp.get("home") or {})]:
        top_hit = team_kp.get("top_hit") or []
        top_hr  = team_kp.get("top_hr") or []
        hit_line = ", ".join(
            f"{p['name']} ({p['hit_prob']:.0%}/{p['verdict']})"
            for p in top_hit
        ) if top_hit else "none"
        hr_line = ", ".join(
            f"{p['name']} ({p['hr_prob']:.1%})"
            for p in top_hr
        ) if top_hr else "none"
        lines.append(f"KEY PLAYERS — {abbr}:")
        lines.append(f"  Hit targets: {hit_line}")
        lines.append(f"  Power targets: {hr_line}")
        lines.append("")

    # ── Weather & Park ────────────────────────────────────────────────────────
    if weather.get("is_dome"):
        lines.append("VENUE: Indoor stadium — weather has little to no impact on play.")
    elif weather.get("temp_f") is not None:
        lines.append("WEATHER:")
        _stat(lines, "Temperature",  weather.get("temp_f"),           fmt=".0f", unit="°F")
        _stat(lines, "Wind",         weather.get("wind_speed_mph"),   fmt=".0f", unit=" mph")
        _stat(lines, "Wind dir",     weather.get("wind_direction_deg"), fmt=".0f", unit="°")
        _stat(lines, "Conditions",   weather.get("condition_text"))
        _stat(lines, "Cloud cover",  weather.get("cloud_cover_pct"),  fmt=".0f", unit="%")
        if weather.get("precipitation_mm", 0) > 0:
            _stat(lines, "Precipitation", weather.get("precipitation_mm"), fmt=".1f", unit=" mm")

        lines.append("")
        if park.get("available"):
            hf  = park.get("hit_factor")
            hrf = park.get("hr_factor")
            lines.append(
                f"PARK FACTORS — {venue}: hit factor {hf:.0f}, HR factor {hrf:.0f} "
                "(100 = league average; >100 = hitter-friendly)"
            )
        else:
            lines.append("PARK FACTORS: unavailable")
    else:
        lines.append("WEATHER: unavailable")
        lines.append("")
        if park.get("available"):
            hf  = park.get("hit_factor")
            hrf = park.get("hr_factor")
            lines.append(
                f"PARK FACTORS — {venue}: hit factor {hf:.0f}, HR factor {hrf:.0f} "
                "(100 = league average; >100 = hitter-friendly)"
            )
        else:
            lines.append("PARK FACTORS: unavailable")

    # ── Data availability summary ─────────────────────────────────────────────
    lines.append("")
    lines.append("DATA AVAILABILITY SUMMARY (base your response strictly on what is 'yes' below):")
    for key, label in [
        ("standings",    "Team records/streaks"),
        ("park_factors", "Park hit/HR factors"),
        ("weather",      "Weather conditions"),
        ("home_lineup",  "Home team lineup projections"),
        ("away_lineup",  "Away team lineup projections"),
        ("pitchers",     "Starting pitcher stats"),
    ]:
        status = "yes" if avail.get(key) else "UNAVAILABLE"
        lines.append(f"  {label}: {status}")

    lines.append("")
    lines.append("NOT PROVIDED (never mention or infer these):")
    lines.append("  Bullpen/reliever ERAs or stats")
    lines.append("  Injuries, IL status, player availability")
    lines.append("  Sportsbook odds, moneylines, spreads, or totals")
    lines.append("  Team OPS, runs per game, wRC+, or other aggregate team stats")

    # ── Output schema ─────────────────────────────────────────────────────────
    lines += [
        "",
        "=" * 60,
        "Respond with exactly this JSON structure (no markdown, no extra keys):",
        "{",
        '  "summary": "2-3 sentences: the core narrative of this matchup. Mention both pitchers and which offense looks stronger per the model.",',
        '  "pitching_edge": "1-2 sentences. Explain WHY the model edge exists — describe the pitcher\'s profile (not raw numbers). If edge is neutral/mixed, say so honestly.",',
        '  "offensive_edge": "1-2 sentences. Compare the offenses qualitatively. Reference YES pick counts and top names if helpful.",',
        '  "weather_park_impact": "1 sentence. For domes: say weather has little to no impact. For unavailable park factors: explicitly say park data is unavailable. Otherwise describe the environment\'s likely effect on scoring.",',
        '  "reasons_away_could_win": ["up to 3 specific reasons grounded in the data — each reason one sentence"],',
        '  "reasons_home_could_win": ["up to 3 specific reasons grounded in the data — each reason one sentence"],',
        '  "data_caveats": ["one item per data section that was unavailable — skip this list if all key data was available"]',
        "}",
    ]

    return "\n".join(lines)


def _repair_json(text: str) -> str:
    """Attempt to close open strings, brackets, and braces in truncated JSON.

    Walks the string character-by-character tracking parser state. Appends
    whatever closing tokens are needed to produce well-formed JSON.  Not a
    general-purpose JSON fixer — designed specifically for Gemini truncation.
    """
    s = text.rstrip()
    in_string   = False
    escape_next = False
    depth_brace   = 0
    depth_bracket = 0

    for ch in s:
        if escape_next:
            escape_next = False
            continue
        if ch == "\\" and in_string:
            escape_next = True
            continue
        if ch == '"':
            in_string = not in_string
        elif not in_string:
            if   ch == "{": depth_brace   += 1
            elif ch == "}": depth_brace    = max(0, depth_brace - 1)
            elif ch == "[": depth_bracket += 1
            elif ch == "]": depth_bracket  = max(0, depth_bracket - 1)

    suffix = ""
    if in_string:
        suffix += '"'
    suffix += "]" * depth_bracket + "}" * depth_brace
    return s + suffix


def _extract_game_fields_regex(text: str) -> dict | None:
    """Last-resort per-field regex extraction from partial/malformed JSON.

    Returns a partial dict (at minimum containing 'summary') or None if
    even the summary field cannot be found.
    """
    fields: dict = {}

    # String fields: "key": "value with possible \\" escapes"
    _str_pat = r'"{key}"\s*:\s*"((?:[^"\\]|\\.)*)"'
    for field in ("summary", "pitching_edge", "offensive_edge", "weather_park_impact"):
        m = re.search(_str_pat.format(key=re.escape(field)), text)
        if m:
            fields[field] = (
                m.group(1)
                .replace("\\n", "\n")
                .replace('\\"', '"')
                .strip()
            )

    # List fields: "key": [ ... ] — find bracketed content then extract strings
    for field in ("reasons_away_could_win", "reasons_home_could_win", "data_caveats"):
        m = re.search(rf'"{re.escape(field)}"\s*:\s*\[', text)
        if m:
            start = m.end()
            depth = 1
            end   = start
            while end < len(text) and depth > 0:
                if   text[end] == "[": depth += 1
                elif text[end] == "]": depth -= 1
                end += 1
            arr_content = text[start : end - 1] if depth == 0 else text[start:]
            items = re.findall(r'"((?:[^"\\]|\\.)*)"', arr_content)
            fields[field] = [
                item.replace("\\n", "\n").replace('\\"', '"').strip()
                for item in items
                if item.strip()
            ]

    return fields if fields.get("summary") else None


def _parse_game_response(text: str, model_name: str = "") -> dict:
    """Multi-stage parser for Gemini game breakdown JSON.

    Stages (tried in order, stops at first success):
      1. Strip markdown fences
      2. Find largest JSON object in text
      3. json.loads — clean path
      4. _repair_json + json.loads — truncation repair
      5. _extract_game_fields_regex — per-field regex
      6. Return {"_parse_failed": True} — caller builds local deterministic fallback

    Returns a validated dict on success, or {"_parse_failed": True, "_parse_error": ...}
    to signal the caller to use the local fallback.
    """
    _SCHEMA = {
        "summary":                (str,  None),
        "pitching_edge":          (str,  None),
        "offensive_edge":         (str,  None),
        "weather_park_impact":    (str,  None),
        "reasons_away_could_win": (list, []),
        "reasons_home_could_win": (list, []),
        "data_caveats":           (list, []),
    }

    def _validate(data: dict) -> dict:
        """Type-check each field; fill missing with defaults; log schema issues."""
        warn: list[str] = []
        out: dict = {}
        for field, (expected_type, default) in _SCHEMA.items():
            val = data.get(field, default)
            if val is None:
                out[field] = default
                warn.append(f"'{field}' missing")
            elif not isinstance(val, expected_type):
                out[field] = default
                warn.append(f"'{field}' wrong type ({type(val).__name__})")
            elif expected_type == str and not val.strip():
                out[field] = default
                warn.append(f"'{field}' empty")
            elif expected_type == list:
                out[field] = [str(item) for item in val if item]
            else:
                out[field] = val
        if warn:
            logger.warning(
                "Gemini game schema issues — model=%s  fields=%s",
                model_name, warn,
            )
            existing = out.get("data_caveats") or []
            out["data_caveats"] = existing + ["Some AI response fields were incomplete — analysis may be partial."]
        return out

    raw_for_log = (text or "")[:300]

    # ── Stage 1: strip fences ─────────────────────────────────────────────────
    clean = (text or "").strip()
    if clean.startswith("```"):
        clean = re.sub(r'^```[a-zA-Z]*\s*', '', clean)
        clean = re.sub(r'\s*```\s*$', '', clean).strip()

    # ── Stage 2: find JSON object ─────────────────────────────────────────────
    if not clean.startswith('{'):
        m = re.search(r'\{[\s\S]*\}', clean)
        if m:
            clean = m.group(0)
        else:
            idx = clean.find('{')
            if idx >= 0:
                clean = clean[idx:]

    # ── Stage 3: clean parse ──────────────────────────────────────────────────
    if clean.startswith('{'):
        try:
            return _validate(json.loads(clean))
        except json.JSONDecodeError as exc:
            logger.debug("Gemini game Stage 3 (clean parse) failed: %s", exc)

    # ── Stage 4: repair truncated JSON + parse ────────────────────────────────
    if clean.startswith('{'):
        try:
            repaired = _repair_json(clean)
            data = json.loads(repaired)
            logger.info(
                "Gemini game Stage 4 (repair) succeeded — model=%s  raw[:100]=%s",
                model_name, raw_for_log[:100],
            )
            return _validate(data)
        except Exception as exc:
            logger.debug("Gemini game Stage 4 (repair) failed: %s", exc)

    # ── Stage 5: per-field regex extraction ───────────────────────────────────
    fields = _extract_game_fields_regex(text or "")
    if fields:
        logger.info(
            "Gemini game Stage 5 (regex) succeeded — model=%s  found=%s",
            model_name, sorted(fields.keys()),
        )
        out: dict = {}
        for field, (expected_type, default) in _SCHEMA.items():
            val = fields.get(field)
            if val is None:
                out[field] = default
            elif expected_type == list and not isinstance(val, list):
                out[field] = default
            else:
                out[field] = val
        return out

    # ── Stage 6: signal complete parse failure ────────────────────────────────
    logger.warning(
        "Gemini game parse failed all stages — model=%s  raw[:300]=%s",
        model_name, raw_for_log,
    )
    return {
        "_parse_failed": True,
        "_parse_error":  f"All parse stages failed. Raw prefix: {raw_for_log[:120]}",
    }
