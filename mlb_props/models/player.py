# models/player.py — Player dataclasses. Pure data, no logic.

from dataclasses import dataclass, field


@dataclass
class BatterMetrics:
    """Full batter profile merging MLB API, Baseball Savant, and FanGraphs data."""

    player_id: int
    name: str
    team: str
    hand: str               # R, L, or S
    season: int

    games: int = 0
    pa: int = 0

    # Traditional stats
    avg: float = 0.0
    obp: float = 0.0
    slg: float = 0.0
    ops: float = 0.0
    woba: float = 0.0

    # Baseball Savant Statcast leaderboard
    avg_exit_velo: float = 0.0
    avg_launch_angle: float = 0.0
    barrel_count: int = 0
    barrel_pct: float = 0.0
    hard_hit_pct: float = 0.0
    sweet_spot_pct: float = 0.0
    ideal_la_pct: float = 0.0
    hr_fb_ratio: float = 0.0
    ev50: float = 0.0

    # Baseball Savant Custom leaderboard
    xba: float = 0.0
    xwoba: float = 0.0
    xslg: float = 0.0
    k_pct: float = 0.0
    bb_pct: float = 0.0
    whiff_pct: float = 0.0
    swing_pct: float = 0.0

    # Calculated / derived
    hr_count: int = 0
    hr_per_game: float = 0.0

    # Recent form (last 14 days)
    recent_avg: float = 0.0
    recent_hard_hit_pct: float = 0.0
    recent_barrel_pct: float = 0.0
    recent_exit_velo: float = 0.0

    # Matchup context
    platoon_advantage: float = 0.50   # 0.0 to 1.0
    lineup_position: int = 5          # 1-9


@dataclass
class PitcherMetrics:
    """Full pitcher profile merging Savant and FanGraphs data."""

    player_id: int
    name: str
    team: str
    hand: str
    season: int

    # Standard stats
    era: float = 0.0
    fip: float = 0.0
    whip: float = 0.0
    k9: float = 0.0
    bb9: float = 0.0
    hr9: float = 0.0

    # Baseball Savant Statcast (allowed)
    xera: float = 0.0
    avg_exit_velo_allowed: float = 0.0
    barrel_pct_allowed: float = 0.0
    hard_hit_pct_allowed: float = 0.0
    sweet_spot_pct_allowed: float = 0.0

    # Baseball Savant Custom (allowed)
    xwoba_allowed: float = 0.0
    k_pct: float = 0.0
    bb_pct: float = 0.0
    whiff_pct_generated: float = 0.0


@dataclass
class CareerSeason:
    """Single season row in a player's career history."""

    season: int
    team: str = ""
    games: int = 0
    pa: int = 0
    avg: float = 0.0
    hr: int = 0
    rbi: int = 0
    ops: float = 0.0
    woba: float = 0.0
    xba: float = 0.0
    xwoba: float = 0.0
    barrel_pct: float = 0.0
    hard_hit_pct: float = 0.0
    sweet_spot_pct: float = 0.0
    avg_exit_velo: float = 0.0
    ev50: float = 0.0
    whiff_pct: float = 0.0
    k_pct: float = 0.0
    bb_pct: float = 0.0
    war: float = 0.0
