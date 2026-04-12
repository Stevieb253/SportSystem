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

# Expected plate appearances per game used in Poisson conversion
_PA_PER_GAME = 4.2


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

    # Temperature multiplier
    temp_mult = _temperature_hit_multiplier(weather)
    weighted_sum *= temp_mult

    # Poisson: P(at least one hit in ~4.2 PA)
    expected_hits = weighted_sum * _PA_PER_GAME
    prob = 1.0 - math.exp(-expected_hits)

    prob = max(0.25, min(0.85, prob))

    return round(prob, 4), components


def get_verdict(probability: float) -> str:
    """Convert probability to YES / LEAN / NO verdict.

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
