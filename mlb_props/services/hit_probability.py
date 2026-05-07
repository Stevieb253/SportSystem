# services/hit_probability.py
# Calculates hit probability. No API calls, no Flask, no imports from api/.

import logging
import math

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import config
from models.game import ProbablePitcher
from models.player import BatterMetrics
from models.weather import Weather

logger = logging.getLogger(__name__)

# Effective plate appearances per game (excludes walks, HBP)
_EFFECTIVE_PA = 3.7

# Composite score → per-PA hit rate mapping.
# The weighted_sum (0–1) is a talent/matchup score, NOT a batting average.
# Map it to the realistic MLB range: ~.200 (weakest matchup) → ~.355 (elite).
_HIT_RATE_FLOOR   = 0.200
_HIT_RATE_CEILING = 0.355


def normalize_value(value: float, stat_name: str) -> float:
    """Normalise a stat value to [0, 1] using config ranges.

    Args:
        value: Raw stat value.
        stat_name: Key in config.NORMALIZATION_RANGES.

    Returns:
        Float clipped to [0.0, 1.0].
    """
    low, high = config.NORMALIZATION_RANGES.get(stat_name, (0.0, 1.0))
    if high == low:
        return 0.5
    normalised = (value - low) / (high - low)
    return max(0.0, min(1.0, normalised))


def calculate_component_scores(
    batter: BatterMetrics,
    pitcher: ProbablePitcher,
    park_hit_factor: float,
    weather: Weather,
) -> dict[str, float]:
    """Compute normalised 0-1 contribution score for each hit-prob component.

    Args:
        batter: BatterMetrics for the hitter.
        pitcher: Opposing ProbablePitcher.
        park_hit_factor: Park hit factor (100 = neutral).
        weather: Weather conditions at the stadium.

    Returns:
        Dict mapping component name → normalised score.
    """
    # xBA — most predictive single metric
    xba_score = normalize_value(batter.xba, "xba")

    # Hard hit% — contact quality
    hard_hit_score = normalize_value(batter.hard_hit_pct, "hard_hit_pct")

    # Sweet spot% — optimal launch angle for hits
    sweet_spot_score = normalize_value(batter.sweet_spot_pct, "sweet_spot_pct")

    # Pitcher xERA — inverted: higher xERA is better for the batter
    pitcher_xera_score = normalize_value(pitcher.xera, "pitcher_xera")

    # Platoon advantage — direct 0-1
    platoon_score = batter.platoon_advantage

    # Park factor
    park_score = normalize_value(park_hit_factor, "park_factor")

    # Recent form — 14-day average
    recent_score = normalize_value(batter.recent_avg, "recent_avg")

    # Lineup position — inverted: position 1 gets highest score (more PAs)
    lineup_score = normalize_value(10 - batter.lineup_position, "lineup_pos")

    # Whiff% — inverted: lower whiff = better contact
    whiff_score = 1.0 - normalize_value(batter.whiff_pct, "whiff_pct")

    return {
        "xba":             xba_score,
        "hard_hit_pct":    hard_hit_score,
        "sweet_spot_pct":  sweet_spot_score,
        "pitcher_xera":    pitcher_xera_score,
        "platoon_adv":     platoon_score,
        "park_factor":     park_score,
        "recent_form":     recent_score,
        "lineup_position": lineup_score,
        "whiff_pct":       whiff_score,
    }


def calculate_hit_probability(
    batter: BatterMetrics,
    pitcher: ProbablePitcher,
    park_hit_factor: float,
    weather: Weather,
) -> tuple[float, dict]:
    """Calculate probability of at least one hit in a game.

    Formula:
        1. Weighted sum of normalised component scores
        2. Temperature adjustment from config
        3. Poisson conversion: P(≥1 hit) = 1 - e^(-expected_hits)
        4. Clipped to [0.25, 0.85]

    Args:
        batter: BatterMetrics for the hitter.
        pitcher: Opposing ProbablePitcher.
        park_hit_factor: Park hit factor (100 = neutral).
        weather: Weather conditions.

    Returns:
        Tuple of (probability float, component_scores dict).
    """
    components = calculate_component_scores(batter, pitcher, park_hit_factor, weather)

    weighted_sum = sum(
        components[key] * config.HIT_WEIGHTS.get(key, 0.0)
        for key in components
    )

    # Temperature multiplier applied to the hit rate, not the raw score
    temp_mult = _temperature_hit_multiplier(weather)

    # ── Calibrated Poisson conversion ──────────────────────────────────────────
    # weighted_sum is a 0–1 composite talent score, NOT a batting average.
    # Map it to a realistic per-PA hit rate so Poisson gives sensible output.
    #
    # Mapping: 0.0 → .200 (terrible matchup), 1.0 → .355 (elite)
    # Typical MLB starter scores 0.40–0.65, mapping to .269–.312 avg range.
    #
    # Poisson P(≥1 hit) examples:
    #   .240 avg × 3.7 PA = 0.888 exp hits → 59%
    #   .270 avg × 3.7 PA = 0.999 exp hits → 63%
    #   .305 avg × 3.7 PA = 1.129 exp hits → 68%
    #   .340 avg × 3.7 PA = 1.258 exp hits → 72%
    # ──────────────────────────────────────────────────────────────────────────
    per_pa_rate = _HIT_RATE_FLOOR + weighted_sum * (_HIT_RATE_CEILING - _HIT_RATE_FLOOR)
    per_pa_rate = max(_HIT_RATE_FLOOR, min(_HIT_RATE_CEILING, per_pa_rate * temp_mult))

    expected_hits = per_pa_rate * _EFFECTIVE_PA
    prob = 1.0 - math.exp(-expected_hits)

    # Realistic game-level bounds: even the weakest matchup gives ~42%,
    # the very best tops out around 78%.
    prob = max(0.42, min(0.78, prob))

    return round(prob, 4), components


def get_verdict(probability: float) -> str:
    """Convert probability to YES / LEAN / NO using absolute fallback thresholds.

    This is the per-player fallback used during game processing before the full
    day's distribution is known.  assign_percentile_verdicts() overrides these
    once all players have been scored.

    Args:
        probability: Hit probability float.

    Returns:
        'YES', 'LEAN', or 'NO'.
    """
    lean_cutoff, yes_cutoff = config.HIT_VERDICT_THRESHOLDS
    if probability >= yes_cutoff:
        return "YES"
    if probability >= lean_cutoff:
        return "LEAN"
    return "NO"


def assign_percentile_verdicts(results: list) -> None:
    """Reassign hit_verdict in-place using percentile rank within today's pool.

    Called once after all players for the day have been scored and sorted
    descending by hit_probability.  Overrides the per-player fallback verdicts
    set by get_verdict() during game processing.

    Splits (configurable via config):
        Top 15%   → YES   best contact profiles on today's slate
        Next 45%  → LEAN  above-average matchups worth tracking
        Bottom 40% → NO   below-average matchups

    Falls back to get_verdict() per-player if results is empty or an error
    occurs, leaving the existing verdicts intact.

    Args:
        results: List of HitProbabilityResult objects, sorted descending by
                 hit_probability (build_daily_model sorts before calling this).
    """
    if not results:
        return

    try:
        n        = len(results)
        yes_n    = max(1, round(n * config.HIT_PERCENTILE_YES))
        lean_end = max(yes_n + 1, round(n * (config.HIT_PERCENTILE_YES + config.HIT_PERCENTILE_LEAN)))

        for i, result in enumerate(results):
            if i < yes_n:
                result.hit_verdict = "YES"
            elif i < lean_end:
                result.hit_verdict = "LEAN"
            else:
                result.hit_verdict = "NO"

        logger.debug(
            "Percentile verdicts assigned: %d YES, %d LEAN, %d NO (n=%d)",
            yes_n, lean_end - yes_n, n - lean_end, n,
        )
    except Exception as exc:
        logger.warning(
            "assign_percentile_verdicts failed — keeping fallback verdicts: %s", exc
        )


# ── Internal helpers ──────────────────────────────────────────────────────────

def _temperature_hit_multiplier(weather: Weather) -> float:
    """Return hit probability multiplier based on temperature."""
    if weather.is_dome:
        return 1.0
    if weather.temp_f < config.WEATHER_COLD_THRESHOLD:
        return config.WEATHER_COLD_HIT_MULT
    if weather.temp_f > config.WEATHER_HOT_THRESHOLD:
        return config.WEATHER_HOT_HIT_MULT
    return 1.0
