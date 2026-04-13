# config.py — All tunable values. Zero hardcoded values anywhere else.

import os
from pathlib import Path

# Load .env if present (python-dotenv optional — works without it too)
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass

# ── API Endpoints ──────────────────────────────────────────────────────────────
MLB_API_BASE_URL        = "https://statsapi.mlb.com/api/v1"
MLB_API_LIVE_URL        = "https://statsapi.mlb.com/api/v1.1"
ESPN_API_BASE_URL       = "https://site.api.espn.com/apis/site/v2/sports/baseball/mlb"
WEATHER_API_URL         = "https://api.open-meteo.com/v1/forecast"
ODDS_API_URL            = "https://api.the-odds-api.com/v4"

# Baseball Savant scraping endpoints
SAVANT_STATCAST_LEADERBOARD_URL = "https://baseballsavant.mlb.com/leaderboard/statcast"
SAVANT_CUSTOM_LEADERBOARD_URL   = "https://baseballsavant.mlb.com/leaderboard/custom"
SAVANT_EXPECTED_STATS_URL       = "https://baseballsavant.mlb.com/leaderboard/expected_statistics"
SAVANT_PLAYER_URL               = "https://baseballsavant.mlb.com/savant-player"
SAVANT_SEARCH_URL               = "https://baseballsavant.mlb.com/statcast_search"
SAVANT_PERCENTILE_URL           = "https://baseballsavant.mlb.com/leaderboard/percentile-rankings"

# ── API Keys (user fills in) ───────────────────────────────────────────────────
ODDS_API_KEY            = os.getenv("ODDS_API_KEY", "")  # Free at the-odds-api.com — 500 requests/month

# ── Cache ──────────────────────────────────────────────────────────────────────
CACHE_DIR               = ".cache"
CACHE_TTL_HOURS         = 12
LIVE_POLL_SECONDS       = 30

# ── Hit Probability Formula Weights ───────────────────────────────────────────
# Must sum to 1.0
HIT_WEIGHTS = {
    "xba":             0.28,
    "hard_hit_pct":    0.16,
    "sweet_spot_pct":  0.08,
    "pitcher_xera":    0.16,
    "platoon_adv":     0.10,
    "park_factor":     0.08,
    "recent_form":     0.08,
    "lineup_position": 0.04,
    "whiff_pct":       0.02,
}

# ── HR Probability Formula Weights ────────────────────────────────────────────
# Must sum to 1.0
HR_WEIGHTS = {
    "barrel_pct":      0.26,
    "exit_velocity":   0.18,
    "ev50":            0.08,
    "launch_angle":    0.10,
    "xwoba":           0.08,
    "hr_fb_ratio":     0.12,
    "park_hr_factor":  0.10,
    "pitcher_hr9":     0.06,
    "platoon_adv":     0.02,
}

# ── Verdict Thresholds ────────────────────────────────────────────────────────
HIT_VERDICT_THRESHOLDS  = (0.48, 0.60)   # (LEAN cutoff, YES cutoff)
HR_VERDICT_THRESHOLDS   = (0.08, 0.15)   # (LEAN cutoff, YES cutoff)

# ── Normalization Ranges ──────────────────────────────────────────────────────
NORMALIZATION_RANGES = {
    "xba":             (0.150, 0.380),
    "hard_hit_pct":    (0.20,  0.65),
    "sweet_spot_pct":  (0.25,  0.55),
    "whiff_pct":       (0.12,  0.40),
    "pitcher_xera":    (1.5,   6.5),
    "park_factor":     (80,    115),
    "recent_avg":      (0.150, 0.450),
    "lineup_pos":      (1,     9),
    "barrel_pct":      (0.0,   0.25),
    "exit_velocity":   (82,    95),
    "ev50":            (88,    105),
    "ideal_la_pct":    (0.0,   0.60),
    "xwoba":           (0.250, 0.450),
    "hr_fb_ratio":     (0.03,  0.40),
    "park_hr_factor":  (70,    130),
    "pitcher_hr9":     (0.3,   2.5),
}

# ── Weather Adjustments ───────────────────────────────────────────────────────
WEATHER_COLD_THRESHOLD  = 50
WEATHER_HOT_THRESHOLD   = 80
WEATHER_COLD_HIT_MULT   = 0.93
WEATHER_HOT_HIT_MULT    = 1.04
WEATHER_COLD_HR_MULT    = 0.88
WEATHER_HOT_HR_MULT     = 1.06
WIND_OUT_BOOST          = 1.08
WIND_IN_SUPPRESS        = 0.92
WIND_OUT_THRESHOLD_MPH  = 8
WIND_IN_THRESHOLD_MPH   = 12

# ── Stadium Coordinates for Weather ──────────────────────────────────────────
STADIUM_COORDS = {
    "Dodger Stadium":           {"lat": 34.0739, "lon": -118.2400},
    "Fenway Park":              {"lat": 42.3467, "lon": -71.0972},
    "Yankee Stadium":           {"lat": 40.8296, "lon": -73.9262},
    "Wrigley Field":            {"lat": 41.9484, "lon": -87.6553},
    "Great American Ball Park": {"lat": 39.0979, "lon": -84.5082},
    "Petco Park":               {"lat": 32.7076, "lon": -117.1570},
    "Truist Park":              {"lat": 33.8907, "lon": -84.4678},
    "T-Mobile Park":            {"lat": 47.5914, "lon": -122.3325},
    "Globe Life Field":         {"lat": 32.7473, "lon": -97.0820},
    "Camden Yards":             {"lat": 39.2838, "lon": -76.6218},
    "Citizens Bank Park":       {"lat": 39.9061, "lon": -75.1665},
    "Citi Field":               {"lat": 40.7571, "lon": -73.8458},
    "Busch Stadium":            {"lat": 38.6226, "lon": -90.1928},
    "American Family Field":    {"lat": 43.0280, "lon": -87.9712},
    "Kauffman Stadium":         {"lat": 39.0517, "lon": -94.4803},
    "Comerica Park":            {"lat": 42.3390, "lon": -83.0485},
    "Tropicana Field":          {"lat": 27.7682, "lon": -82.6534},
    "Rogers Centre":            {"lat": 43.6414, "lon": -79.3894},
    "Target Field":             {"lat": 44.9817, "lon": -93.2783},
    "Guaranteed Rate Field":    {"lat": 41.8300, "lon": -87.6339},
    "Progressive Field":        {"lat": 41.4962, "lon": -81.6852},
    "Oracle Park":              {"lat": 37.7786, "lon": -122.3893},
    "Chase Field":              {"lat": 33.4455, "lon": -112.0667},
    "Coors Field":              {"lat": 39.7559, "lon": -104.9942},
    "loanDepot Park":           {"lat": 25.7781, "lon": -80.2197},
    "PNC Park":                 {"lat": 40.4469, "lon": -80.0057},
    "Minute Maid Park":         {"lat": 29.7573, "lon": -95.3555},
    "Angel Stadium":            {"lat": 33.8003, "lon": -117.8827},
    "Nationals Park":           {"lat": 38.8730, "lon": -77.0074},
    "Sutter Health Park":       {"lat": 38.5802, "lon": -121.4987},
}

# ── Dome Stadiums (weather irrelevant) ────────────────────────────────────────
DOME_STADIUMS = {
    "Tropicana Field",
    "Chase Field",
    "Rogers Centre",
    "American Family Field",
    "Minute Maid Park",
    "Globe Life Field",
}

# ── Baseball Savant Scraping Config ───────────────────────────────────────────
SAVANT_BATTER_STATCAST_PARAMS = {
    "type":    "batter",
    "min":     "q",
    "sort":    "barrels_per_pa",
    "sortDir": "desc",
}

SAVANT_BATTER_CUSTOM_PARAMS = {
    "type":       "batter",
    "min":        "q",
    "sort":       "xwoba",
    "sortDir":    "desc",
    "selections": (
        "pa,k_percent,bb_percent,woba,xwoba,sweet_spot_percent,"
        "barrel_batted_rate,hard_hit_percent,avg_best_speed,"
        "avg_hyper_speed,whiff_percent,swing_percent"
    ),
}

SAVANT_PITCHER_STATCAST_PARAMS = {
    "type":    "pitcher",
    "min":     "q",
    "sort":    "xera",
    "sortDir": "asc",
}

SAVANT_PITCHER_CUSTOM_PARAMS = {
    "type":       "pitcher",
    "min":        "q",
    "sort":       "xwoba",
    "sortDir":    "asc",
    "selections": (
        "pa,k_percent,bb_percent,woba,xwoba,barrel_batted_rate,"
        "hard_hit_percent,whiff_percent,xera,exit_velocity_avg"
    ),
}

# Baseball Savant request headers
SAVANT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept":          "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer":         "https://baseballsavant.mlb.com/",
    "Origin":          "https://baseballsavant.mlb.com",
}

SAVANT_REQUEST_DELAY_SECONDS = 0.3  # Only fires on cache miss; low enough to not rate-limit

# ── Historical Data ────────────────────────────────────────────────────────────
STATCAST_START_YEAR  = 2015
FANGRAPHS_START_YEAR = 2002
MIN_PA_QUALIFY       = 25
MIN_IP_QUALIFY       = 5
RECENT_FORM_DAYS     = 14
BVP_MIN_AB           = 10

# ── App ────────────────────────────────────────────────────────────────────────
DEBUG = False
PORT  = 5000
HOST  = "0.0.0.0"
