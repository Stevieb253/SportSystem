# data/normalizer.py
# Converts raw API dicts into typed model dataclasses.
# This is the ONLY place where raw data → dataclass conversion happens.

import logging
from datetime import datetime, timezone, timedelta
from typing import Any

import pandas as pd

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from models.game import Game, Team, Venue, ProbablePitcher
from models.player import BatterMetrics, CareerSeason
from models.weather import Weather

logger = logging.getLogger(__name__)


def normalize_game(raw_game: dict, weather: Weather) -> Game:
    """Convert a raw MLB API game dict into a Game dataclass.

    Args:
        raw_game: Raw game dict from mlb_api.get_schedule.
        weather: Weather instance for the stadium.

    Returns:
        Populated Game dataclass.
    """
    home_team_raw = raw_game.get("teams", {}).get("home", {}).get("team", {})
    away_team_raw = raw_game.get("teams", {}).get("away", {}).get("team", {})
    venue_raw = raw_game.get("venue", {})

    home_team = Team(
        id=home_team_raw.get("id", 0),
        name=home_team_raw.get("name", ""),
        abbreviation=home_team_raw.get("abbreviation", ""),
        league=home_team_raw.get("league", {}).get("name", ""),
        division=home_team_raw.get("division", {}).get("name", ""),
    )
    away_team = Team(
        id=away_team_raw.get("id", 0),
        name=away_team_raw.get("name", ""),
        abbreviation=away_team_raw.get("abbreviation", ""),
        league=away_team_raw.get("league", {}).get("name", ""),
        division=away_team_raw.get("division", {}).get("name", ""),
    )

    venue_location = venue_raw.get("location", {})
    venue = Venue(
        name=venue_raw.get("name", ""),
        city=venue_location.get("city", ""),
        state=venue_location.get("stateAbbrev", ""),
        lat=float(venue_location.get("defaultCoordinates", {}).get("latitude", 0.0)),
        lon=float(venue_location.get("defaultCoordinates", {}).get("longitude", 0.0)),
        is_dome=weather.is_dome,
        elevation_ft=0,
    )

    status_code = raw_game.get("status", {}).get("abstractGameState", "Preview")
    status_map = {"Preview": "scheduled", "Live": "live", "Final": "final"}
    status = status_map.get(status_code, "scheduled")

    linescore = raw_game.get("linescore", {})
    home_score = linescore.get("teams", {}).get("home", {}).get("runs", 0)
    away_score = linescore.get("teams", {}).get("away", {}).get("runs", 0)
    inning = linescore.get("currentInning", 0)
    inning_half = linescore.get("inningHalf", "")

    # Probable pitchers
    home_pp_raw = raw_game.get("teams", {}).get("home", {}).get("probablePitcher", {})
    away_pp_raw = raw_game.get("teams", {}).get("away", {}).get("probablePitcher", {})

    home_pitcher = _minimal_pitcher(home_pp_raw)
    away_pitcher = _minimal_pitcher(away_pp_raw)

    game_datetime_utc = raw_game.get("gameDate", "")
    tz_info           = venue_raw.get("timeZone", {})
    game_time_display = _format_game_time(game_datetime_utc, tz_info)

    return Game(
        game_pk=raw_game.get("gamePk", 0),
        date=game_datetime_utc[:10] if game_datetime_utc else "",
        status=status,
        home_team=home_team,
        away_team=away_team,
        venue=venue,
        home_pitcher=home_pitcher,
        away_pitcher=away_pitcher,
        home_score=home_score,
        away_score=away_score,
        inning=inning,
        inning_half=inning_half.lower(),
        weather=weather,
        game_time_local=game_time_display,
    )


def normalize_probable_pitcher(
    raw_pitcher: dict,
    savant_data: dict | None,
    fangraphs_row: "pd.Series | None",
) -> ProbablePitcher:
    """Build a ProbablePitcher from MLB API data merged with Savant/FanGraphs.

    Args:
        raw_pitcher: Pitcher dict from MLB API schedule hydrate.
        savant_data: Merged Savant dict for this pitcher_id, or None.
        fangraphs_row: FanGraphs pitching DataFrame row, or None.

    Returns:
        Populated ProbablePitcher dataclass.
    """
    pid = raw_pitcher.get("id", 0)
    name = raw_pitcher.get("fullName", raw_pitcher.get("name", "TBD"))
    hand = (
        raw_pitcher.get("pitchHand", {}).get("code", "R")
        if isinstance(raw_pitcher.get("pitchHand"), dict)
        else raw_pitcher.get("pitchHand", "R")
    )

    sv = savant_data or {}
    fg = fangraphs_row if fangraphs_row is not None else {}

    def _fg(col: str, default: float = 0.0) -> float:
        try:
            val = fg[col] if hasattr(fg, "__getitem__") else fg.get(col, default)
            return float(val) if val is not None else default
        except (KeyError, TypeError, ValueError):
            return default

    def _svp(*keys: str) -> float:
        """Read Savant percentage field — auto-convert 0-100 → 0-1 if value > 1."""
        for k in keys:
            v = sv.get(k)
            if v is not None:
                f = _safe_float(v)
                if f:
                    return round(f / 100, 6) if f > 1.0 else f
        return 0.0

    return ProbablePitcher(
        id=pid,
        name=name,
        hand=hand,
        era=_safe_float(sv.get("p_era") or sv.get("era")) or _fg("ERA"),
        xera=_safe_float(sv.get("xera") or sv.get("p_xera")) or _fg("xERA"),
        k9=_safe_float(sv.get("k9")) or _fg("K/9"),
        bb9=_safe_float(sv.get("bb9")) or _fg("BB/9"),
        hr9=_safe_float(sv.get("hr9")) or _fg("HR/9"),
        whip=_safe_float(sv.get("whip")) or _fg("WHIP"),
        fip=_fg("FIP"),
        k_pct=_svp("k_percent"),
        bb_pct=_svp("bb_percent"),
        hard_hit_pct_allowed=_svp("hard_hit_percent", "ev95percent"),
        barrel_pct_allowed=_svp("barrel_batted_rate", "brl_percent"),
        avg_exit_velo_allowed=_safe_float(sv.get("exit_velocity_avg") or sv.get("avg_hit_speed")),
        xwoba_allowed=_safe_float(sv.get("xwoba")),
        whiff_pct_generated=_svp("whiff_percent"),
    )


def normalize_batter(
    player_id: int,
    player_name: str,
    lineup_pos: int,
    team: str,
    hand: str,
    savant_data: dict | None,
    fangraphs_row: "pd.Series | None",
    recent_savant_records: list[dict],
    vs_pitcher: ProbablePitcher,
    season: int,
) -> BatterMetrics:
    """Build a BatterMetrics from all available data sources.

    Args:
        player_id: MLBAM player ID.
        player_name: Display name.
        lineup_pos: Lineup position 1-9.
        team: Team abbreviation.
        hand: Batting hand R/L/S.
        savant_data: Merged Savant dict for this player, or None.
        fangraphs_row: FanGraphs batting DataFrame row, or None.
        recent_savant_records: Raw recent Statcast records (last 14 days).
        vs_pitcher: Opposing pitcher for platoon calc.
        season: Current season year.

    Returns:
        Populated BatterMetrics dataclass.
    """
    from api.baseball_savant_api import calculate_recent_metrics_from_statcast

    sv = savant_data or {}
    fg = fangraphs_row if fangraphs_row is not None else {}

    def _fg(col: str, default: float = 0.0) -> float:
        try:
            val = fg[col] if hasattr(fg, "__getitem__") else fg.get(col, default)
            return float(val) if val is not None else default
        except (KeyError, TypeError, ValueError):
            return default

    def _sv(*keys: str, pct: bool = False) -> float:
        """Look up first non-zero value from sv dict.

        Args:
            *keys: Savant field names to try in order.
            pct: If True and value > 1, divide by 100.
                 Savant CSV returns percentages as 0-100 (e.g. k_percent=28.5),
                 but the model stores 0-1 decimals (0.285) for display as 28.5%.
        """
        for k in keys:
            v = sv.get(k)
            if v is not None:
                f = _safe_float(v)
                if f:
                    if pct and f > 1.0:
                        return round(f / 100, 6)
                    return f
        return 0.0

    recent = calculate_recent_metrics_from_statcast(recent_savant_records)
    platoon = calculate_platoon_advantage(hand, vs_pitcher.hand)

    avg = _safe_float(sv.get("ba") or sv.get("batting_avg") or sv.get("avg")) or _fg("AVG")

    return BatterMetrics(
        player_id=player_id,
        name=player_name,
        team=team,
        hand=hand,
        season=season,
        games=int(_fg("G")),  # games played from FanGraphs (player_age is not games)
        pa=int(_safe_float(sv.get("pa") or sv.get("plateAppearances"))) or int(_fg("PA")),
        avg=avg,
        obp=_safe_float(sv.get("obp")) or _fg("OBP"),
        slg=_safe_float(sv.get("slg")) or _fg("SLG"),
        ops=_safe_float(sv.get("ops")) or _fg("OPS"),
        woba=_safe_float(sv.get("woba")) or _fg("wOBA"),
        # Statcast leaderboard fields — CSV columns aliased by _normalize_savant_row()
        # Non-percentage fields: exit velo (mph), launch angle (degrees), EV50 (mph)
        avg_exit_velo=_sv("exit_velocity_avg", "avg_hit_speed"),
        avg_launch_angle=_sv("launch_angle_avg", "avg_hit_angle"),
        barrel_count=int(_safe_float(sv.get("barrels"))),
        ev50=_sv("ev50", "avg_best_speed"),
        # Percentage fields (Savant CSV returns 0-100, pct=True converts to 0-1)
        barrel_pct=_sv("brl_pa", "brl_percent", "barrel_batted_rate", pct=True),
        hard_hit_pct=_sv("hard_hit_percent", "ev95percent", pct=True),
        sweet_spot_pct=_sv("sweet_spot_percent", "anglesweetspotpercent", pct=True),
        ideal_la_pct=_sv("ideal_la_percent", pct=True),
        hr_fb_ratio=_safe_float(sv.get("hr_fb_pct")) or _fg("HR/FB"),
        # Custom leaderboard fields (xBA, xwOBA, xSLG are decimals; k%/bb%/whiff% are 0-100)
        xba=_safe_float(sv.get("xba") or sv.get("est_ba")),
        xwoba=_safe_float(sv.get("xwoba") or sv.get("est_woba")),
        xslg=_safe_float(sv.get("xslg") or sv.get("est_slg")),
        k_pct=_sv("k_percent", pct=True),
        bb_pct=_sv("bb_percent", pct=True),
        whiff_pct=_sv("whiff_percent", pct=True),
        swing_pct=_sv("swing_percent", pct=True),
        # Derived
        hr_count=int(_fg("HR")),
        hr_per_game=round(_fg("HR") / max(_fg("G"), 1), 4),
        # Recent form
        recent_avg=recent.get("recent_avg", 0.0),
        recent_hard_hit_pct=recent.get("recent_hard_hit_pct", 0.0),
        recent_barrel_pct=recent.get("recent_barrel_pct", 0.0),
        recent_exit_velo=recent.get("recent_exit_velo", 0.0),
        platoon_advantage=platoon,
        lineup_position=lineup_pos,
    )


def normalize_weather(raw: dict, stadium_name: str) -> Weather:
    """Convert raw Open-Meteo dict to Weather dataclass.

    Args:
        raw: Raw dict from weather_api.get_weather.
        stadium_name: Stadium display name.

    Returns:
        Weather dataclass.
    """
    current = raw.get("current", {})
    return Weather(
        stadium=stadium_name,
        temp_f=float(current.get("temperature_2m", 72.0)),
        wind_speed_mph=float(current.get("wind_speed_10m", 5.0)),
        wind_direction_deg=float(current.get("wind_direction_10m", 180.0)),
        condition_code=int(current.get("weather_code", 0)),
        fetched_at=datetime.utcnow(),
        is_dome=stadium_name in __import__("config").DOME_STADIUMS,
    )


def calculate_platoon_advantage(batter_hand: str, pitcher_hand: str) -> float:
    """Calculate platoon advantage score.

    Opposite-hand matchup favours the batter; same-hand does not.

    Args:
        batter_hand: Batter handedness R, L, or S.
        pitcher_hand: Pitcher handedness R or L.

    Returns:
        0.62 opposite hand, 0.38 same hand, 0.50 if either unknown.
    """
    if not batter_hand or not pitcher_hand:
        return 0.50
    b = batter_hand.upper()[0]
    p = pitcher_hand.upper()[0]
    if b == "S":
        return 0.55   # Switch hitter gets mild advantage vs either hand
    if b != p:
        return 0.62   # Platoon advantage
    return 0.38       # Platoon disadvantage


# ── Helpers ───────────────────────────────────────────────────────────────────

def _safe_float(val: Any, default: float = 0.0) -> float:
    """Safely cast to float."""
    try:
        return float(val) if val is not None else default
    except (TypeError, ValueError):
        return default


def _minimal_pitcher(raw: dict) -> ProbablePitcher:
    """Build minimal ProbablePitcher from schedule hydrate stub."""
    if not raw:
        return ProbablePitcher(id=0, name="TBD", hand="R")
    hand_raw = raw.get("pitchHand", {})
    hand = hand_raw.get("code", "R") if isinstance(hand_raw, dict) else str(hand_raw)
    return ProbablePitcher(
        id=raw.get("id", 0),
        name=raw.get("fullName", raw.get("name", "TBD")),
        hand=hand,
    )


def _format_game_time(utc_str: str, tz_info: dict) -> str:
    """Convert UTC game datetime string to Pacific time.

    Always displays in Pacific time (PDT Apr-Oct, PST Nov-Mar) regardless
    of the venue's local timezone — consistent single timezone for all games.

    Args:
        utc_str: UTC datetime string e.g. '2026-04-12T17:35:00Z'.
        tz_info: Unused (kept for signature compatibility).

    Returns:
        Formatted string e.g. '10:35 AM PDT'.
    """
    if not utc_str:
        return "TBD"

    # Determine Pacific offset: PDT (UTC-7) Mar-Nov, PST (UTC-8) Nov-Mar
    # Simple month-based rule: months 3-11 = PDT, else PST
    try:
        clean  = utc_str.replace("Z", "+00:00")
        utc_dt = datetime.fromisoformat(clean)
        month  = utc_dt.month
        if 3 <= month <= 11:
            pt_offset = -7
            tz_abbr   = "PDT"
        else:
            pt_offset = -8
            tz_abbr   = "PST"

        pt_tz    = timezone(timedelta(hours=pt_offset))
        local_dt = utc_dt.astimezone(pt_tz)

        # Windows-compatible: strip leading zero from hour manually
        t = local_dt.strftime("%I:%M %p").lstrip("0")
        return f"{t} {tz_abbr}"
    except Exception:
        return utc_str[:16].replace("T", " ") + " UTC"
