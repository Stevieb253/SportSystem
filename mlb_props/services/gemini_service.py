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

import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_CACHE_TTL_HOURS = 2.0

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

    # Cache read
    if cache and cache_key:
        cached = cache.get(cache_key, ttl_hours=_CACHE_TTL_HOURS)
        if cached is not None:
            logger.debug("Gemini cache hit: %s", cache_key)
            return cached

    if not _init_gemini():
        return {**_empty, "model": _active_model_name or "unknown", "error": _init_error_msg or "Gemini not initialised"}

    from google.genai import types as _genai_types
    prompt = _build_prompt(ctx)

    # Build ordered list of models to attempt: primary first, then fallbacks (no dupes).
    models_to_try = [_active_model_name]
    for m in _FALLBACK_MODELS:
        if m not in models_to_try:
            models_to_try.append(m)

    last_exc: Exception | None = None
    for model_name in models_to_try:
        try:
            response = _vertex_model.models.generate_content(
                model=model_name,
                contents=prompt,
                config=_genai_types.GenerateContentConfig(
                    temperature=0.2,
                    max_output_tokens=800,
                ),
            )
            if model_name != _active_model_name:
                logger.info("Gemini fallback succeeded: %s → %s", _active_model_name, model_name)
            result = _parse_response(response.text.strip())
            result["available"] = True
            result["model"]     = model_name
            result["error"]     = None

            if cache and cache_key:
                cache.set(cache_key, result)

            return result

        except Exception as exc:
            last_exc = exc
            if _is_overload_error(exc):
                logger.warning("Gemini %s overloaded, trying next model: %s", model_name, exc)
                continue
            # Non-transient error (e.g. auth, bad request) — don't bother with fallbacks.
            logger.warning("Gemini explain failed (non-transient): %s", exc)
            return {**_empty, "error": str(exc)}

    logger.warning("All Gemini models unavailable. Last error: %s", last_exc)
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
        "You are a baseball analyst explaining a statistical model's probability for a player prop bet.",
        "",
        "CRITICAL RULES:",
        "1. Only cite statistics explicitly provided below. Do NOT invent, estimate, or assume numbers.",
        "2. When a section is marked unavailable, acknowledge it honestly.",
        "3. Do not recommend a bet. Explain what the data shows — the model made the assessment.",
        "4. Be concise: explanation 3-5 sentences, lists max 4 items each.",
        "5. Return ONLY valid JSON matching the exact schema shown at the bottom. No markdown.",
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
        # Gemini sometimes wraps in ```json ... ``` — strip it
        clean = text
        if clean.startswith("```"):
            clean = clean.split("```", 2)[-1] if clean.count("```") >= 2 else clean
            if clean.startswith("json"):
                clean = clean[4:]
            clean = clean.rstrip("`").strip()

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
        return {**fallback, "explanation": text[:600] if text else None}


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
