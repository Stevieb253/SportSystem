# models/game.py — Game-related dataclasses. Pure data, no logic.

from dataclasses import dataclass, field
from .weather import Weather


@dataclass
class Venue:
    """MLB stadium details."""

    name: str
    city: str
    state: str
    lat: float
    lon: float
    is_dome: bool = False
    elevation_ft: int = 0


@dataclass
class Team:
    """MLB team info."""

    id: int
    name: str
    abbreviation: str
    league: str = ""
    division: str = ""


@dataclass
class ProbablePitcher:
    """Probable starting pitcher with Statcast and standard metrics."""

    id: int
    name: str
    hand: str               # R or L

    # Standard stats
    era: float = 0.0
    xera: float = 0.0       # From Baseball Savant expected stats
    k9: float = 0.0
    bb9: float = 0.0
    hr9: float = 0.0
    whip: float = 0.0
    fip: float = 0.0

    # Rate stats
    k_pct: float = 0.0
    bb_pct: float = 0.0

    # Baseball Savant allowed metrics
    hard_hit_pct_allowed: float = 0.0
    barrel_pct_allowed: float = 0.0
    avg_exit_velo_allowed: float = 0.0
    xwoba_allowed: float = 0.0
    whiff_pct_generated: float = 0.0


@dataclass
class Game:
    """A single MLB game with full context."""

    game_pk: int
    date: str
    status: str             # scheduled / live / final

    home_team: Team = field(default_factory=lambda: Team(0, "", ""))
    away_team: Team = field(default_factory=lambda: Team(0, "", ""))
    venue: Venue = field(default_factory=lambda: Venue("", "", "", 0.0, 0.0))

    home_pitcher: ProbablePitcher = field(
        default_factory=lambda: ProbablePitcher(0, "TBD", "R")
    )
    away_pitcher: ProbablePitcher = field(
        default_factory=lambda: ProbablePitcher(0, "TBD", "R")
    )

    home_score: int = 0
    away_score: int = 0
    inning: int = 0
    inning_half: str = ""   # top or bottom

    weather: Weather = field(
        default_factory=lambda: Weather("", 72.0, 5.0, 180.0, 0)
    )
    game_time_local: str = ""
