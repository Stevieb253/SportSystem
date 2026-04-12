# models/probability.py — Probability result dataclasses. Pure data, no logic.

from dataclasses import dataclass, field
from .player import BatterMetrics
from .game import Game, ProbablePitcher


@dataclass
class HitProbabilityResult:
    """Output of the hit probability model for a single batter in a game."""

    player: BatterMetrics
    game: Game
    vs_pitcher: ProbablePitcher
    hit_probability: float
    hit_verdict: str              # YES / LEAN / NO
    component_scores: dict = field(default_factory=dict)
    weather_adjustment: float = 0.0


@dataclass
class HRProbabilityResult:
    """Output of the HR probability model for a single batter in a game."""

    player: BatterMetrics
    game: Game
    vs_pitcher: ProbablePitcher
    hr_probability: float
    hr_verdict: str               # YES / LEAN / NO
    component_scores: dict = field(default_factory=dict)
    weather_adjustment: float = 0.0
    odds: str = ""                # e.g. +350
    implied_prob: float = 0.0
    value_edge: float = 0.0
