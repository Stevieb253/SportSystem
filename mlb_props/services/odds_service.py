# services/odds_service.py
# Canonical location for all sportsbook odds math.
#
# RULES:
#   - Pure functions only — no API calls, no DB access, no Flask imports.
#   - This is the single source of truth for odds conversion and edge calculation.
#   - best_bets.py, routes.py, and any future ML code should all import from here.
#   - odds_api.py keeps its own copy of american_to_implied_prob so the API layer
#     stays self-contained (it predates this module and cannot import from services/).
#
# NOTE ON VIG:
#   We currently only receive OVER odds from the Odds API (one side).
#   True devigging requires both OVER and UNDER prices. With one side, raw implied
#   probability slightly overstates the book's true belief (by the vig percentage).
#   For typical MLB prop vig (~4-6%), raw implied prob is off by ~2-3pp.
#   When the Odds API data includes UNDER prices (Phase 4+), swap in devig_two_sided().

import logging

logger = logging.getLogger(__name__)


# ── Core conversions ───────────────────────────────────────────────────────────

def american_to_implied_prob(american_odds: int) -> float:
    """Convert American odds integer to raw implied probability (0–1).

    Includes the bookmaker vig — use devig_two_sided() when both sides are
    available for a vig-free estimate.

    Examples:
        +350  →  0.2222  (bet $100 to win $350)
        -135  →  0.5745  (bet $135 to win $100)
        +100  →  0.5000  (even money)
    """
    if american_odds >= 0:
        return 100.0 / (american_odds + 100.0)
    abs_odds = abs(american_odds)
    return abs_odds / (abs_odds + 100.0)


def devig_two_sided(over_odds: int, under_odds: int) -> tuple[float, float]:
    """Remove vig from two-sided market, returning (over_prob, under_prob).

    The raw probabilities sum to >1.0 (the vig). This normalises them to sum
    to exactly 1.0, giving the book's true implied belief.

    Use this in Phase 4+ when the Odds API returns both OVER and UNDER prices.

    Example:
        over=-135, under=+115  →  raw=(0.5745, 0.4651)  sum=1.0396
        devigged              →  (0.5526, 0.4474)  sum=1.000
    """
    raw_over  = american_to_implied_prob(over_odds)
    raw_under = american_to_implied_prob(under_odds)
    total     = raw_over + raw_under
    if total <= 0:
        return 0.5, 0.5
    return round(raw_over / total, 4), round(raw_under / total, 4)


def format_american(odds: int) -> str:
    """Format integer odds as American odds string.

    Examples:  350 → '+350',  -135 → '-135',  0 → '+0'
    """
    return f"+{odds}" if odds >= 0 else str(odds)


def parse_american(odds_str: str | int) -> int | None:
    """Parse '+350' or '-135' or integer to int. Returns None on bad input."""
    try:
        return int(odds_str)
    except (ValueError, TypeError):
        return None


# ── Edge calculation ───────────────────────────────────────────────────────────

def calculate_edge(model_prob: float, implied_prob: float) -> float:
    """Edge = model probability minus sportsbook implied probability.

    Positive → model sees more value than the book prices in.
    Negative → book prices this bet higher than model thinks it deserves.

    A +5pp edge on a bet priced at -135 is meaningful.
    A -3pp edge means the book has the better of it — skip.
    """
    return round(model_prob - implied_prob, 4)


def edge_label(edge: float) -> str:
    """Human-readable label for an edge value.

    Thresholds match best_bets.py EDGE_VALUE / EDGE_AVOID constants.
        ≥ +5pp  →  'VALUE'
        ≤ -3pp  →  'AVOID'
        else    →  'FAIR'
    """
    if edge >= 0.05:
        return "VALUE"
    if edge <= -0.03:
        return "AVOID"
    return "FAIR"


# ── Enrichment helper (used by best_bets.py and routes.py) ────────────────────

def enrich_with_edge(model_prob: float, odds_info: dict) -> dict:
    """Derive edge fields from a model probability and a raw odds_api dict.

    Accepts the format returned by odds_api.fetch_all_props_for_today():
        {"best_odds": "+350", "best_book": "DraftKings", "implied_prob": 0.2222, ...}

    Returns a canonical edge dict with consistent key names:
        {
            "sportsbook_odds":     "+350" | None,
            "sportsbook_line":     0.5    | None,   # always 0.5 for hit/HR anytime props
            "implied_probability": 0.2222 | None,
            "edge":                0.4378 | None,
            "edge_label":          "VALUE" | "FAIR" | "AVOID" | None,
            "best_book":           "DraftKings" | None,
        }

    All values are None when odds_info is empty or missing implied_prob —
    the caller should handle None gracefully (model-only mode).
    """
    if not odds_info:
        return _empty_edge()

    raw_implied = odds_info.get("implied_prob")
    if raw_implied is None:
        return {
            "sportsbook_odds":     odds_info.get("best_odds"),
            "sportsbook_line":     None,
            "implied_probability": None,
            "edge":                None,
            "edge_label":          None,
            "best_book":           odds_info.get("best_book"),
        }

    implied = round(float(raw_implied), 4)
    edge    = calculate_edge(model_prob, implied)

    return {
        "sportsbook_odds":     odds_info.get("best_odds"),
        "sportsbook_line":     0.5,       # anytime hit / anytime HR lines are always 0.5
        "implied_probability": implied,
        "edge":                edge,
        "edge_label":          edge_label(edge),
        "best_book":           odds_info.get("best_book"),
    }


def _empty_edge() -> dict:
    return {
        "sportsbook_odds":     None,
        "sportsbook_line":     None,
        "implied_probability": None,
        "edge":                None,
        "edge_label":          None,
        "best_book":           None,
    }


# ── Verification helper ────────────────────────────────────────────────────────

def verify_edge(model_prob: float, american_odds: str | int) -> dict:
    """Show all derived values for a given model probability and American odds.

    Designed for manual sanity-checking from the command line:

        python -c "
        import sys; sys.path.insert(0, '.')
        from services.odds_service import verify_edge
        import pprint; pprint.pprint(verify_edge(0.66, -135))
        "

    Args:
        model_prob:    Your model's probability (0–1).
        american_odds: Sportsbook odds as int or string ('+350', -135, 350).

    Returns:
        Dict showing every derived value so you can spot errors instantly.
    """
    odds_int = parse_american(american_odds)
    if odds_int is None:
        return {"error": f"Cannot parse odds: {american_odds!r}"}

    implied = american_to_implied_prob(odds_int)
    edge    = calculate_edge(model_prob, implied)

    return {
        "model_probability":    round(model_prob, 4),
        "american_odds":        format_american(odds_int),
        "implied_probability":  round(implied, 4),
        "edge":                 edge,
        "edge_pct":             f"{edge * 100:+.2f}pp",
        "edge_label":           edge_label(edge),
        "vig_note":             "raw implied (includes vig — use devig_two_sided when UNDER price available)",
    }
