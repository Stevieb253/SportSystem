# services/hr_probability.py
# Calculates home run probability. No API calls, no Flask, no imports from api/.

import logging

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import config
from models.game import ProbablePitcher
from models.player import BatterMetrics
from models.weather import Weather

logger = logging.getLogger(__name__)


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


def calculate_wind_factor(weather: Weather) -> float:
    """Return wind adjustment multiplier for HR probability.

    Wind blowing out (90-270 deg) above threshold boosts HR.
    Wind blowing in (<45 or >315 deg) above threshold suppresses HR.

    Args:
        weather: Weather conditions.

    Returns:
        Multiplier float.
    """
    if weather.is_dome:
        return 1.0

    direction = weather.wind_direction_deg
    speed = weather.wind_speed_mph

    wind_out = 90 <= direction <= 270
    wind_in = direction < 45 or direction > 315

    if wind_out and speed > config.WIND_OUT_THRESHOLD_MPH:
        return config.WIND_OUT_BOOST
    if wind_in and speed > config.WIND_IN_THRESHOLD_MPH:
        return config.WIND_IN_SUPPRESS
    return 1.0


def calculate_component_scores(
    batter: BatterMetrics,
    pitcher: ProbablePitcher,
    park_hr_factor: float,
    weather: Weather,
) -> dict[str, float]:
    """Compute normalised 0-1 contribution score for each HR-prob component.

    Args:
        batter: BatterMetrics for the hitter.
        pitcher: Opposing ProbablePitcher.
        park_hr_factor: Park HR factor (100 = neutral).
        weather: Weather conditions.

    Returns:
        Dict mapping component name → normalised score.
    """
    barrel_score     = normalize_value(batter.barrel_pct, "barrel_pct")
    exit_velo_score  = normalize_value(batter.avg_exit_velo, "exit_velocity")
    ev50_score       = normalize_value(batter.ev50, "ev50")
    launch_score     = normalize_value(batter.ideal_la_pct, "ideal_la_pct")
    xwoba_score      = normalize_value(batter.xwoba, "xwoba")
    hr_fb_score      = normalize_value(batter.hr_fb_ratio, "hr_fb_ratio")
    park_score       = normalize_value(park_hr_factor, "park_hr_factor")
    # Pitcher HR/9 — NOT inverted: higher HR/9 allowed = better for batter
    pitcher_hr9_score = normalize_value(pitcher.hr9, "pitcher_hr9")
    platoon_score    = batter.platoon_advantage

    return {
        "barrel_pct":     barrel_score,
        "exit_velocity":  exit_velo_score,
        "ev50":           ev50_score,
        "launch_angle":   launch_score,
        "xwoba":          xwoba_score,
        "hr_fb_ratio":    hr_fb_score,
        "park_hr_factor": park_score,
        "pitcher_hr9":    pitcher_hr9_score,
        "platoon_adv":    platoon_score,
    }


def calculate_hr_probability(
    batter: BatterMetrics,
    pitcher: ProbablePitcher,
    park_hr_factor: float,
    weather: Weather,
) -> tuple[float, dict]:
    """Calculate home run probability for a batter in a given game.

    Formula:
        1. Weighted sum of normalised component scores
        2. Wind factor adjustment
        3. Temperature factor adjustment
        4. hr_prob = weighted_score * 0.28
        5. Clipped to [0.03, 0.35]

    Args:
        batter: BatterMetrics for the hitter.
        pitcher: Opposing ProbablePitcher.
        park_hr_factor: Park HR factor (100 = neutral).
        weather: Weather conditions.

    Returns:
        Tuple of (probability float, component_scores dict).
    """
    components = calculate_component_scores(batter, pitcher, park_hr_factor, weather)

    weighted_sum = sum(
        components[key] * config.HR_WEIGHTS.get(key, 0.0)
        for key in components
    )

    wind_factor = calculate_wind_factor(weather)
    temp_mult   = _temperature_hr_multiplier(weather)

    hr_prob = weighted_sum * 0.28 * wind_factor * temp_mult
    hr_prob = max(0.03, min(0.35, hr_prob))

    return round(hr_prob, 4), components


def get_verdict(probability: float) -> str:
    """Convert probability to YES / LEAN / NO verdict.

    Args:
        probability: HR probability float.

    Returns:
        'YES', 'LEAN', or 'NO'.
    """
    lean_cutoff, yes_cutoff = config.HR_VERDICT_THRESHOLDS
    if probability >= yes_cutoff:
        return "YES"
    if probability >= lean_cutoff:
        return "LEAN"
    return "NO"


# ── Internal helpers ──────────────────────────────────────────────────────────

def _temperature_hr_multiplier(weather: Weather) -> float:
    """Return HR probability multiplier based on temperature."""
    if weather.is_dome:
        return 1.0
    if weather.temp_f < config.WEATHER_COLD_THRESHOLD:
        return config.WEATHER_COLD_HR_MULT
    if weather.temp_f > config.WEATHER_HOT_THRESHOLD:
        return config.WEATHER_HOT_HR_MULT
    return 1.0
