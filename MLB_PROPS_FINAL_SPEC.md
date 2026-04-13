# MLB PROPS MODEL — COMPLETE TECHNICAL SPECIFICATION
# =====================================================
# Send this entire document to Claude Code.
# Claude Code should read every section and build the
# complete application exactly as described.
# Do not skip any section. Do not combine any files.
# Build in this order: config → models → api → data → services → web

---

## WHAT THIS APP IS

A full-stack MLB player props model web application built in Python.

The app pulls real baseball data from multiple free sources — including
direct scraping of Baseball Savant (baseballsavant.mlb.com) which is
the official Statcast home and provides the most precise and complete
Statcast metrics available anywhere — runs a weighted statistical
probability model, and displays the results in a clean dark-themed
web interface accessible by the user and their friends.

The application must be built with professional software engineering
practices from day one so it can be scaled up later into a larger
product without needing to be rewritten.

---

## CORE ENGINEERING PRINCIPLES — ENFORCE THROUGHOUT EVERY FILE

### Low Coupling
Every module is independent. No module imports from another unless
there is a clearly defined interface. Modules do not know about each
other's internals. If module A needs data from module B, it receives
that data as a function argument — it never calls module B directly.

### High Cohesion
Every file does exactly ONE thing.
- A file called hit_probability.py only calculates hit probability
- A file called mlb_api.py only talks to the MLB Stats API
- A file called cache.py only handles caching
- No exceptions

### Separation of Concerns
Three completely separate layers that never mix:
1. Data layer — fetches and caches raw data from external sources
2. Service layer — pure business logic and math, no API calls, no HTML
3. Web layer — Flask routes and templates only, no business logic

### Dependency Injection
Services receive their dependencies as constructor arguments or function
parameters. They never instantiate their own dependencies. This makes
testing easy and swapping implementations trivial.

### Configuration Over Hardcoding
Every tunable value lives in config.py. Weights, thresholds, URLs,
API keys, stadium coordinates — all in config.py. Zero hardcoded
values in any other file.

### Graceful Degradation
If any single data source fails (network error, bad response, API down),
the app continues running using the other sources. Never crash on a
missing data source. Always return a sensible default.

### Caching
Every external API call is cached to disk. Same data is never fetched
twice on the same day. Cache files invalidate at midnight ET.
Respect free tier rate limits by always checking cache before fetching.

### Code Quality
- Full Python type hints on every function signature
- Docstring on every class and every function
- No function longer than 50 lines — split if needed
- No class with more than 8 methods — split if needed
- Use .get() with defaults on all dict access — never raw key access
- Wrap all network calls in try/except — never let a network error crash the app
- Check DataFrame column existence before accessing
- Use Python logging module — not print statements in production code
- No magic numbers anywhere — everything named and in config

---

## COMPLETE FOLDER STRUCTURE

Build exactly this. Do not add or remove any files.

```
mlb_props/
├── api/
│   ├── __init__.py
│   ├── mlb_api.py
│   ├── baseball_savant_api.py      ← NEW: scrapes Baseball Savant directly
│   ├── statcast_api.py             ← pybaseball fallback and FanGraphs data
│   ├── espn_api.py
│   ├── odds_api.py
│   └── weather_api.py
│
├── models/
│   ├── __init__.py
│   ├── game.py
│   ├── player.py
│   ├── probability.py
│   └── weather.py
│
├── services/
│   ├── __init__.py
│   ├── hit_probability.py
│   ├── hr_probability.py
│   ├── historical_service.py
│   ├── live_tracker.py
│   └── model_builder.py
│
├── data/
│   ├── __init__.py
│   ├── cache.py
│   ├── pipeline.py
│   └── normalizer.py
│
├── web/
│   ├── __init__.py
│   ├── app.py
│   ├── routes.py
│   ├── templates/
│   │   ├── base.html
│   │   ├── index.html
│   │   ├── game.html
│   │   ├── player.html
│   │   └── historical.html
│   └── static/
│       ├── css/
│       │   └── style.css
│       └── js/
│           ├── main.js
│           ├── strike_zone.js
│           └── live.js
│
├── config.py
├── main.py
├── requirements.txt
└── README.md
```

---

## CONFIG.PY

Everything tunable lives here. Build this file first.

```python
# ── API Endpoints ──────────────────────────────────────────────────────────────
MLB_API_BASE_URL        = "https://statsapi.mlb.com/api/v1"
MLB_API_LIVE_URL        = "https://statsapi.mlb.com/api/v1.1"
ESPN_API_BASE_URL       = "https://site.api.espn.com/apis/site/v2/sports/baseball/mlb"
WEATHER_API_URL         = "https://api.open-meteo.com/v1/forecast"
ODDS_API_URL            = "https://api.the-odds-api.com/v4"

# Baseball Savant scraping endpoints
# These return JSON when called with the right parameters
SAVANT_STATCAST_LEADERBOARD_URL = "https://baseballsavant.mlb.com/leaderboard/statcast"
SAVANT_CUSTOM_LEADERBOARD_URL   = "https://baseballsavant.mlb.com/leaderboard/custom"
SAVANT_EXPECTED_STATS_URL       = "https://baseballsavant.mlb.com/leaderboard/expected_statistics"
SAVANT_PLAYER_URL               = "https://baseballsavant.mlb.com/savant-player"
SAVANT_SEARCH_URL               = "https://baseballsavant.mlb.com/statcast_search"
SAVANT_PERCENTILE_URL           = "https://baseballsavant.mlb.com/leaderboard/percentile-rankings"

# ── API Keys (user fills in) ───────────────────────────────────────────────────
ODDS_API_KEY            = "f63570eee680606f78c2899d96148dce"   # Free at the-odds-api.com — 500 requests/month

# ── Cache ──────────────────────────────────────────────────────────────────────
CACHE_DIR               = ".cache"
CACHE_TTL_HOURS         = 12
LIVE_POLL_SECONDS       = 30

# ── Hit Probability Formula Weights ───────────────────────────────────────────
# Must sum to 1.0
# These are starting values — tune based on accuracy tracking over time
HIT_WEIGHTS = {
    "xba":                  0.28,   # xBA from Baseball Savant — most predictive
    "hard_hit_pct":         0.16,   # Hard hit % (exit velo >= 95 mph) from Savant
    "sweet_spot_pct":       0.08,   # Sweet spot % (LA 8-32 deg) from Savant — NEW
    "pitcher_xera":         0.16,   # Pitcher xERA — matchup quality
    "platoon_adv":          0.10,   # Handedness advantage
    "park_factor":          0.08,   # Park hit factor
    "recent_form":          0.08,   # Last 14 days avg
    "lineup_position":      0.04,   # Lineup spot — more PA = more chances
    "whiff_pct":            0.02,   # Inverse: lower whiff = better contact — NEW
}

# ── HR Probability Formula Weights ────────────────────────────────────────────
# Must sum to 1.0
HR_WEIGHTS = {
    "barrel_pct":           0.26,   # Barrel % from Baseball Savant — best HR predictor
    "exit_velocity":        0.18,   # Avg exit velo from Savant
    "ev50":                 0.08,   # Top 50% hardest hit balls avg — NEW from Savant
    "launch_angle":         0.10,   # Ideal LA % (25-35 deg) from Savant
    "xwoba":                0.08,   # xwOBA from Savant custom leaderboard — NEW
    "hr_fb_ratio":          0.12,   # HR/FB ratio
    "park_hr_factor":       0.10,   # Park HR environment
    "pitcher_hr9":          0.06,   # Pitcher HR allowed per 9
    "platoon_adv":          0.02,   # Handedness advantage for power
}

# ── Verdict Thresholds ────────────────────────────────────────────────────────
HIT_VERDICT_THRESHOLDS  = (0.48, 0.60)   # (LEAN cutoff, YES cutoff)
HR_VERDICT_THRESHOLDS   = (0.08, 0.15)   # (LEAN cutoff, YES cutoff)

# ── Normalization Ranges ──────────────────────────────────────────────────────
NORMALIZATION_RANGES = {
    "xba":              (0.150, 0.380),
    "hard_hit_pct":     (0.20,  0.65),
    "sweet_spot_pct":   (0.25,  0.55),   # Sweet spot range from Savant
    "whiff_pct":        (0.12,  0.40),   # Inverted in formula — lower is better
    "pitcher_xera":     (1.5,   6.5),
    "park_factor":      (80,    115),
    "recent_avg":       (0.150, 0.450),
    "lineup_pos":       (1,     9),
    "barrel_pct":       (0.0,   0.25),
    "exit_velocity":    (82,    95),
    "ev50":             (88,    105),    # EV50 range from Savant
    "ideal_la_pct":     (0.0,   0.60),
    "xwoba":            (0.250, 0.450),  # xwOBA range
    "hr_fb_ratio":      (0.03,  0.40),
    "park_hr_factor":   (70,    130),
    "pitcher_hr9":      (0.3,   2.5),
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
}

# ── Baseball Savant Scraping Config ───────────────────────────────────────────
# These are the exact parameter sets that Baseball Savant accepts
# to return JSON data from their leaderboards

SAVANT_BATTER_STATCAST_PARAMS = {
    "type":     "batter",
    "min":      "q",          # Qualified batters only
    "sort":     "barrels_per_pa",
    "sortDir":  "desc",
}

SAVANT_BATTER_CUSTOM_PARAMS = {
    "type":     "batter",
    "min":      "q",
    "sort":     "xwoba",
    "sortDir":  "desc",
    "selections": "pa,k_percent,bb_percent,woba,xwoba,sweet_spot_percent,"
                  "barrel_batted_rate,hard_hit_percent,avg_best_speed,"
                  "avg_hyper_speed,whiff_percent,swing_percent",
}

SAVANT_PITCHER_STATCAST_PARAMS = {
    "type":     "pitcher",
    "min":      "q",
    "sort":     "xera",
    "sortDir":  "asc",
}

SAVANT_PITCHER_CUSTOM_PARAMS = {
    "type":     "pitcher",
    "min":      "q",
    "sort":     "xwoba",
    "sortDir":  "asc",
    "selections": "pa,k_percent,bb_percent,woba,xwoba,barrel_batted_rate,"
                  "hard_hit_percent,whiff_percent,xera,exit_velocity_avg",
}

# Baseball Savant request headers — needed to avoid being blocked
SAVANT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://baseballsavant.mlb.com/",
    "Origin": "https://baseballsavant.mlb.com",
}

SAVANT_REQUEST_DELAY_SECONDS = 1.5   # Delay between Savant requests to be respectful

# ── Historical Data ────────────────────────────────────────────────────────────
STATCAST_START_YEAR     = 2015
FANGRAPHS_START_YEAR    = 2002
MIN_PA_QUALIFY          = 25
MIN_IP_QUALIFY          = 5
RECENT_FORM_DAYS        = 14
BVP_MIN_AB              = 10

# ── App ────────────────────────────────────────────────────────────────────────
DEBUG                   = False
PORT                    = 5000
HOST                    = "0.0.0.0"
```

---

## MODELS LAYER (models/)

Pure data containers only. No logic. No API calls. Just dataclasses.

### models/weather.py
```
Dataclass: Weather
Fields:
  stadium: str
  temp_f: float
  wind_speed_mph: float
  wind_direction_deg: float
  condition_code: int
  fetched_at: datetime
  is_dome: bool            True if indoor stadium (no weather impact)
```

### models/game.py
```
Dataclass: Venue
Fields:
  name: str
  city: str
  state: str
  lat: float
  lon: float
  is_dome: bool
  elevation_ft: int

Dataclass: Team
Fields:
  id: int
  name: str
  abbreviation: str
  league: str
  division: str

Dataclass: ProbablePitcher
Fields:
  id: int
  name: str
  hand: str               R or L
  era: float
  xera: float             From Baseball Savant expected stats
  k9: float
  bb9: float
  hr9: float
  whip: float
  fip: float
  k_pct: float            Strikeout percentage
  bb_pct: float           Walk percentage
  hard_hit_pct_allowed: float    From Baseball Savant
  barrel_pct_allowed: float      From Baseball Savant
  avg_exit_velo_allowed: float   From Baseball Savant
  xwoba_allowed: float           From Baseball Savant custom leaderboard
  whiff_pct_generated: float     From Baseball Savant custom leaderboard

Dataclass: Game
Fields:
  game_pk: int
  date: str
  status: str             scheduled / live / final
  home_team: Team
  away_team: Team
  venue: Venue
  home_pitcher: ProbablePitcher
  away_pitcher: ProbablePitcher
  home_score: int
  away_score: int
  inning: int
  inning_half: str        top or bottom
  weather: Weather
  game_time_local: str
```

### models/player.py
```
Dataclass: BatterMetrics
Fields:
  player_id: int
  name: str
  team: str
  hand: str               R, L, or S
  season: int
  games: int
  pa: int

  # Traditional stats
  avg: float
  obp: float
  slg: float
  ops: float
  woba: float

  # Baseball Savant Statcast leaderboard fields
  # (from baseballsavant.mlb.com/leaderboard/statcast)
  avg_exit_velo: float          Exit Velocity (EV)
  avg_launch_angle: float       Launch Angle (LA)
  barrel_count: int             Total barrels
  barrel_pct: float             Barrels per PA
  hard_hit_pct: float           Hard Hit % (exit velo >= 95 mph)
  sweet_spot_pct: float         Sweet Spot % (LA 8-32 degrees)
  ideal_la_pct: float           % batted balls at 25-35 degrees (HR zone)
  hr_fb_ratio: float            HR to fly ball ratio
  ev50: float                   Avg of hardest 50% of batted balls (EV50)

  # Baseball Savant Custom Leaderboard fields
  # (from baseballsavant.mlb.com/leaderboard/custom with the provided selections)
  xba: float                    Expected batting average
  xwoba: float                  Expected weighted on-base average
  xslg: float                   Expected slugging
  k_pct: float                  Strikeout percentage
  bb_pct: float                 Walk percentage
  whiff_pct: float              Whiff percentage (swing and miss rate)
  swing_pct: float              Swing percentage

  # Calculated/derived
  hr_count: int
  hr_per_game: float

  # Recent form (last 14 days from Statcast)
  recent_avg: float
  recent_hard_hit_pct: float
  recent_barrel_pct: float
  recent_exit_velo: float

  # Matchup context
  platoon_advantage: float      0.0 to 1.0
  lineup_position: int          1-9

Dataclass: PitcherMetrics
Fields:
  player_id: int
  name: str
  team: str
  hand: str
  season: int

  # Standard stats
  era: float
  fip: float
  whip: float
  k9: float
  bb9: float
  hr9: float

  # Baseball Savant Statcast fields (pitcher allowed)
  xera: float
  avg_exit_velo_allowed: float
  barrel_pct_allowed: float
  hard_hit_pct_allowed: float
  sweet_spot_pct_allowed: float

  # Baseball Savant Custom fields (pitcher)
  xwoba_allowed: float
  k_pct: float
  bb_pct: float
  whiff_pct_generated: float    How often pitcher generates whiffs

Dataclass: CareerSeason
Fields:
  season: int
  team: str
  games: int
  pa: int
  avg: float
  hr: int
  rbi: int
  ops: float
  woba: float
  xba: float
  xwoba: float
  barrel_pct: float
  hard_hit_pct: float
  sweet_spot_pct: float
  avg_exit_velo: float
  ev50: float
  whiff_pct: float
  k_pct: float
  bb_pct: float
  war: float
```

### models/probability.py
```
Dataclass: HitProbabilityResult
Fields:
  player: BatterMetrics
  game: Game
  vs_pitcher: ProbablePitcher
  hit_probability: float
  hit_verdict: str              YES / LEAN / NO
  component_scores: dict        Each weight's contribution breakdown
  weather_adjustment: float     How much weather moved the number

Dataclass: HRProbabilityResult
Fields:
  player: BatterMetrics
  game: Game
  vs_pitcher: ProbablePitcher
  hr_probability: float
  hr_verdict: str
  component_scores: dict
  weather_adjustment: float
  odds: str                     Sportsbook odds if available e.g. +350
  implied_prob: float           Sportsbook implied probability
  value_edge: float             Model prob minus implied prob
```

---

## API LAYER (api/)

One file per data source. No business logic here. Just fetch and return.
All functions catch exceptions and return empty defaults rather than crashing.

### api/baseball_savant_api.py

This is the most important new file. Baseball Savant is the official
Statcast home run by MLB and provides more precise and complete Statcast
data than any other free source. The site returns JSON from its leaderboard
endpoints when called with the right parameters and headers.

```
Purpose: Scrape Baseball Savant leaderboards for batter and pitcher
         Statcast metrics. This is the primary Statcast data source.
         pybaseball is the fallback if Savant is unavailable.

Use the headers defined in config.SAVANT_HEADERS on every request.
Add config.SAVANT_REQUEST_DELAY_SECONDS delay between requests.
Cache all responses aggressively — Savant data only changes daily.

Functions to implement:

get_statcast_leaderboard(year: int, player_type: str) -> list[dict]
  Fetches the Exit Velocity and Barrels leaderboard
  URL: config.SAVANT_STATCAST_LEADERBOARD_URL
  Params: type={player_type}, year={year}, min=q, sort=barrels_per_pa, sortDir=desc
  Try fetching with .json() extension first: URL + "?type=...&results=all"
  If that fails, try requests with Accept: application/json header
  Returns list of player dicts with these fields:
    player_id, player_name, team_name, pa, bip (batted ball events),
    ba, slg, woba, xba, xslg, xwoba,
    exit_velocity_avg, launch_angle_avg,
    barrels, brl_pa (barrels per PA), brl_percent,
    hard_hit_percent, sweet_spot_percent,
    ev50, avg_hyper_speed (top exit velos)
  Cache key: "savant_statcast_{player_type}_{year}"

get_custom_leaderboard(year: int, player_type: str, selections: str) -> list[dict]
  Fetches the custom leaderboard with user-specified stat columns
  URL: config.SAVANT_CUSTOM_LEADERBOARD_URL
  Use the selections from config.SAVANT_BATTER_CUSTOM_PARAMS or
  config.SAVANT_PITCHER_CUSTOM_PARAMS depending on player_type
  Returns list of player dicts with:
    player_id, player_name, team_name, pa,
    k_percent, bb_percent, woba, xwoba,
    sweet_spot_percent, barrel_batted_rate, hard_hit_percent,
    avg_best_speed (EV50), avg_hyper_speed,
    whiff_percent, swing_percent
  Cache key: "savant_custom_{player_type}_{year}"

get_expected_stats(year: int, player_type: str) -> list[dict]
  Fetches expected statistics leaderboard
  URL: config.SAVANT_EXPECTED_STATS_URL
  Returns xBA, xSLG, xwOBA, xERA for all qualified players
  Cache key: "savant_expected_{player_type}_{year}"

get_percentile_rankings(year: int, player_type: str) -> list[dict]
  Fetches Statcast percentile rankings
  URL: config.SAVANT_PERCENTILE_URL
  Returns percentile rank for each Statcast metric per player
  This tells us how a player ranks vs the league in each stat
  Cache key: "savant_percentiles_{player_type}_{year}"

get_player_page_stats(player_id: int, year: int) -> dict
  Fetches an individual player's Statcast page
  URL: https://baseballsavant.mlb.com/savant-player/{player_id}
  Extracts: career stats, year splits, pitch type breakdown,
            spray chart data, rolling averages
  Cache key: "savant_player_{player_id}_{year}"

get_recent_statcast(player_id: int, days: int, player_type: str) -> list[dict]
  Fetches Statcast search results for a player over recent N days
  URL: config.SAVANT_SEARCH_URL
  Params: player_id_type=mlbam, player_id={player_id},
          game_date_gt={start_date}, game_date_lt={today},
          type={player_type}
  Returns list of individual pitch/batted ball records
  Used to calculate recent form metrics
  Cache key: "savant_recent_{player_id}_{days}days"

merge_savant_data(statcast_df: list[dict], custom_df: list[dict]) -> dict
  Merges the statcast leaderboard and custom leaderboard by player_id
  Returns dict keyed by player_id with all Savant metrics combined
  This is the main data structure the pipeline uses for Savant data

calculate_recent_metrics_from_statcast(records: list[dict]) -> dict
  Takes raw Statcast search records for a player
  Calculates: recent_avg, recent_hard_hit_pct, recent_barrel_pct,
              recent_exit_velo from the last N days of data
  Returns dict of recent metrics
```

### api/mlb_api.py
```
Purpose: MLB Stats API — official, free, no key required

Functions:

get_schedule(date_str: str) -> list[dict]
  URL: {MLB_API_BASE_URL}/schedule
  Params: date={date_str}, sportId=1,
          hydrate=probablePitcher,lineups,team,venue,weather
  Returns list of raw game dicts

get_live_feed(game_pk: int) -> dict
  URL: {MLB_API_LIVE_URL}/game/{game_pk}/feed/live
  Returns full live game data including current at bat and pitch log

get_boxscore(game_pk: int) -> dict
  URL: {MLB_API_BASE_URL}/game/{game_pk}/boxscore
  Returns full box score

get_player_info(player_id: int) -> dict
  URL: {MLB_API_BASE_URL}/people/{player_id}
  Params: hydrate=stats(group=hitting,type=season)
  Returns player profile

get_standings(season: int) -> dict
  URL: {MLB_API_BASE_URL}/standings
  Params: leagueId=103,104, season={season}

parse_live_pitches(live_feed: dict) -> list[dict]
  Extracts pitch list from live feed response
  Each pitch dict contains:
    pitch_number, pitch_type, speed, zone (1-13 grid),
    description, balls, strikes, outs, result, event
  Returns list of pitch dicts sorted by pitch_number
```

### api/statcast_api.py
```
Purpose: pybaseball library — FanGraphs data and fallback Statcast
This is the FALLBACK for when Baseball Savant scraping fails.
Baseball Savant API is primary. pybaseball is secondary.

Functions:

get_season_batting_fangraphs(year: int, min_pa: int) -> pd.DataFrame
  Uses pybaseball.batting_stats(year, qual=min_pa)
  Key columns: Name, AVG, OBP, SLG, OPS, wOBA, WAR, HR, G, PA,
               Hard%, Barrel%, EV, HR/FB, xBA, xwOBA
  Used as fallback or to supplement Savant data

get_season_pitching_fangraphs(year: int, min_ip: int) -> pd.DataFrame
  Uses pybaseball.pitching_stats(year, qual=min_ip)
  Key columns: Name, ERA, xERA, FIP, K/9, BB/9, HR/9, WHIP,
               Hard%, Barrel%, EV

get_park_factors(year: int) -> pd.DataFrame
  Uses pybaseball.park_factors(year)
  Returns park factor DataFrame

get_player_id(first_name: str, last_name: str) -> int | None
  Uses pybaseball.playerid_lookup(last_name, first_name)
  Returns MLBAM player ID or None

get_statcast_batter_fallback(player_id: int, start: str, end: str) -> pd.DataFrame
  Uses pybaseball.statcast_batter(start, end, player_id=player_id)
  Fallback when Savant scraping fails
  Returns raw Statcast DataFrame

get_historical_batting(start_year: int, end_year: int, min_pa: int) -> pd.DataFrame
  Loops years start_year through end_year
  Calls get_season_batting_fangraphs each year
  Adds Season column
  Sleeps 0.3 seconds between calls
  Returns combined multi-year DataFrame
```

### api/espn_api.py
```
Purpose: ESPN unofficial endpoints — free, no key

Functions:

get_scoreboard(date_str: str | None) -> dict
  URL: {ESPN_API_BASE_URL}/scoreboard
  Optional date param for specific date
  Returns raw scoreboard including live scores

get_game_summary(espn_game_id: str) -> dict
  URL: {ESPN_API_BASE_URL}/summary?event={espn_game_id}
  Returns detailed game data

get_news() -> list[dict]
  URL: {ESPN_API_BASE_URL}/news
  Returns latest MLB news
```

### api/odds_api.py
```
Purpose: The Odds API — free tier 500 requests/month

Functions:

get_events() -> list[dict]
  URL: {ODDS_API_URL}/sports/baseball_mlb/events
  Returns upcoming MLB events with IDs

get_player_props(event_id: str, market: str) -> dict
  URL: {ODDS_API_URL}/sports/baseball_mlb/events/{event_id}/odds
  Params: apiKey, regions=us, markets={market},
          bookmakers=draftkings,fanduel,betmgm
  Market options: batter_hits, batter_home_runs, batter_total_bases,
                  pitcher_strikeouts, batter_rbis

american_to_implied_prob(american_odds: int) -> float
  Positive odds: 100 / (odds + 100)
  Negative odds: abs(odds) / (abs(odds) + 100)

get_best_book_odds(props_dict: dict, player_name: str) -> dict | None
  Finds best available odds for a player across all books
  Returns: best_odds, best_book, implied_prob or None
```

### api/weather_api.py
```
Purpose: Open-Meteo weather — free, no key

Functions:

get_weather(lat: float, lon: float) -> dict
  URL: {WEATHER_API_URL}
  Params: latitude, longitude, current=temperature_2m+wind_speed_10m+
          wind_direction_10m, temperature_unit=fahrenheit,
          wind_speed_unit=mph, forecast_days=1
  Returns raw Open-Meteo response

get_stadium_weather(stadium_name: str) -> Weather
  Looks up coords from config.STADIUM_COORDS
  Calls get_weather with those coords
  Converts to Weather dataclass
  Returns default (72F, 5mph) if stadium not found or API fails
  Sets is_dome=True for Tropicana Field, Chase Field (roof closed),
       Rogers Centre, American Family Field, Minute Maid Park
  Dome stadiums get default weather values — weather irrelevant
```

---

## DATA LAYER (data/)

### data/cache.py
```
Purpose: Disk-based JSON cache with TTL

Class: Cache

__init__(cache_dir: str, ttl_hours: int)

get(key: str) -> dict | list | None
  Returns cached data or None if missing/expired

set(key: str, data: dict | list) -> None
  Writes data + timestamp to JSON file

invalidate(key: str) -> None
  Deletes one cache entry

clear_all() -> None
  Clears all cache files

is_expired(key: str) -> bool
  True if older than ttl_hours
```

### data/normalizer.py
```
Purpose: Convert raw API dicts into typed model dataclasses
Only place where raw data → dataclass conversion happens

Functions:

normalize_game(raw_game: dict, weather: Weather) -> Game

normalize_probable_pitcher(
    raw_pitcher: dict,
    savant_data: dict | None,
    fangraphs_row: pd.Series | None
) -> ProbablePitcher
  Merges MLB API pitcher info with:
    - Baseball Savant xERA, barrel% allowed, exit velo allowed,
      xwOBA allowed, whiff% generated
    - FanGraphs ERA, FIP, K/9, BB/9, HR/9 as fallback

normalize_batter(
    player_id: int,
    player_name: str,
    lineup_pos: int,
    team: str,
    hand: str,
    savant_data: dict | None,
    fangraphs_row: pd.Series | None,
    recent_savant_records: list[dict],
    vs_pitcher: ProbablePitcher
) -> BatterMetrics
  Merges:
    - Baseball Savant statcast leaderboard: barrel%, hard hit%,
      exit velo, sweet spot%, EV50, launch angle, barrels
    - Baseball Savant custom leaderboard: xBA, xwOBA, k%, bb%,
      whiff%, swing%
    - FanGraphs as fallback for any missing fields
    - Recent Savant records for 14-day form metrics
    - Calculates platoon_advantage from batter/pitcher handedness

normalize_weather(raw: dict, stadium_name: str) -> Weather

calculate_platoon_advantage(batter_hand: str, pitcher_hand: str) -> float
  Returns:
    0.62 if opposite hand (platoon advantage — LHB vs RHP or RHB vs LHP)
    0.38 if same hand (platoon disadvantage)
    0.50 if either hand unknown
```

### data/pipeline.py
```
Purpose: Orchestrates fetching and combining all data sources
Only file that calls multiple API modules together
All services get their data from pipeline — never from APIs directly

Class: DataPipeline

__init__(cache: Cache)

load_season_savant_data(year: int) -> dict
  PRIMARY data load — Baseball Savant
  Calls baseball_savant_api.get_statcast_leaderboard (batters)
  Calls baseball_savant_api.get_custom_leaderboard (batters)
  Calls baseball_savant_api.get_statcast_leaderboard (pitchers)
  Calls baseball_savant_api.get_custom_leaderboard (pitchers)
  Calls baseball_savant_api.get_expected_stats (pitchers)
  Merges all results by player_id
  Returns dict: {player_id: merged_savant_metrics}
  Cache key: "savant_season_{year}"

load_season_fangraphs_data(year: int) -> dict
  FALLBACK data load — pybaseball/FanGraphs
  Calls statcast_api.get_season_batting_fangraphs
  Calls statcast_api.get_season_pitching_fangraphs
  Returns dict: {"batting_df": df, "pitching_df": df}
  Cache key: "fangraphs_season_{year}"

load_park_factors(year: int) -> pd.DataFrame
  Calls statcast_api.get_park_factors
  Returns park factor DataFrame

load_games_for_date(date_str: str) -> list[Game]
  Calls mlb_api.get_schedule
  For each game fetches weather via weather_api
  Normalizes each game via normalizer
  Returns list[Game]

load_batter_data(
    player_id: int,
    player_name: str,
    lineup_pos: int,
    team: str,
    hand: str,
    vs_pitcher: ProbablePitcher,
    savant_season_data: dict,
    fangraphs_data: dict
) -> BatterMetrics
  Gets player's Savant data from savant_season_data dict
  Gets recent 14-day Statcast from Baseball Savant recent endpoint
  Falls back to FanGraphs if Savant data missing
  Normalizes into BatterMetrics via normalizer
  Cache key: "batter_{player_id}_{date}"

load_pitcher_data(
    pitcher_id: int,
    pitcher_name: str,
    hand: str,
    savant_season_data: dict,
    fangraphs_data: dict
) -> ProbablePitcher
  Same pattern as load_batter_data but for pitchers

load_historical_player(
    player_name: str,
    start_year: int,
    end_year: int
) -> list[CareerSeason]
  Fetches Baseball Savant data for each year in range
  Falls back to FanGraphs if Savant unavailable for a year
  Returns list of CareerSeason sorted ascending by year
```

---

## SERVICES LAYER (services/)

Pure math and orchestration. No API calls. No Flask. No imports from api/.

### services/hit_probability.py
```
Purpose: Calculate hit probability

Functions:

normalize_value(value: float, stat_name: str) -> float
  Looks up range in config.NORMALIZATION_RANGES[stat_name]
  Clips to [0, 1]

calculate_component_scores(
    batter: BatterMetrics,
    pitcher: ProbablePitcher,
    park_hit_factor: float,
    weather: Weather
) -> dict[str, float]
  Returns normalized 0-1 score for each component:
    xba: normalize batter.xba
    hard_hit_pct: normalize batter.hard_hit_pct
    sweet_spot_pct: normalize batter.sweet_spot_pct   ← NEW Savant field
    pitcher_xera: normalize pitcher.xera (inverted — higher xERA better for batter)
    platoon_adv: batter.platoon_advantage directly
    park_factor: normalize park_hit_factor
    recent_form: normalize batter.recent_avg
    lineup_position: normalize (10 - batter.lineup_position)  inverted so pos 1 = highest
    whiff_pct: 1 - normalize(batter.whiff_pct)  inverted — lower whiff is better

calculate_hit_probability(
    batter: BatterMetrics,
    pitcher: ProbablePitcher,
    park_hit_factor: float,
    weather: Weather
) -> tuple[float, dict]
  Calls calculate_component_scores
  Multiplies each score by weight from config.HIT_WEIGHTS
  Sums all weighted scores
  Applies temperature adjustment from config
  Converts to P(1+ hit) via Poisson:
    expected_hits = weighted_score * 4.2
    prob = 1 - e^(-expected_hits)
  Clips to [0.25, 0.85]
  Returns (probability, component_scores_dict)

get_verdict(probability: float) -> str
  Uses config.HIT_VERDICT_THRESHOLDS
  Returns YES / LEAN / NO
```

### services/hr_probability.py
```
Purpose: Calculate home run probability

Functions:

calculate_wind_factor(weather: Weather) -> float
  Wind direction 90-270 AND speed > WIND_OUT_THRESHOLD = WIND_OUT_BOOST
  Wind direction < 45 or > 315 AND speed > WIND_IN_THRESHOLD = WIND_IN_SUPPRESS
  Dome stadiums: return 1.0 (no wind effect)
  Otherwise: 1.0

calculate_component_scores(
    batter: BatterMetrics,
    pitcher: ProbablePitcher,
    park_hr_factor: float,
    weather: Weather
) -> dict[str, float]
  Returns normalized 0-1 score per component:
    barrel_pct: normalize batter.barrel_pct
    exit_velocity: normalize batter.avg_exit_velo
    ev50: normalize batter.ev50                ← NEW Savant field
    launch_angle: normalize batter.ideal_la_pct
    xwoba: normalize batter.xwoba             ← NEW Savant field
    hr_fb_ratio: normalize batter.hr_fb_ratio
    park_hr_factor: normalize park_hr_factor
    pitcher_hr9: normalize pitcher.hr9 (NOT inverted — higher HR/9 = better for batter)
    platoon_adv: batter.platoon_advantage

calculate_hr_probability(
    batter: BatterMetrics,
    pitcher: ProbablePitcher,
    park_hr_factor: float,
    weather: Weather
) -> tuple[float, dict]
  Calls calculate_component_scores
  Applies weights from config.HR_WEIGHTS
  Applies calculate_wind_factor
  Applies temperature factor
  Converts: hr_prob = weighted_score * 0.28
  Clips to [0.03, 0.35]
  Returns (probability, component_scores_dict)

get_verdict(probability: float) -> str
  Uses config.HR_VERDICT_THRESHOLDS
```

### services/model_builder.py
```
Purpose: Orchestrate building the complete daily model

Class: ModelBuilder

__init__(
    pipeline: DataPipeline,
    hit_service,        hit_probability module
    hr_service,         hr_probability module
)
  Dependency injection — receives collaborators as arguments

build_daily_model(date_str: str) -> dict
  Calls pipeline.load_season_savant_data (primary)
  Calls pipeline.load_season_fangraphs_data (fallback)
  Calls pipeline.load_park_factors
  Calls pipeline.load_games_for_date
  For each game:
    For each player in home and away confirmed lineups:
      Calls pipeline.load_batter_data
      Gets park factors for this venue
      Calls hit_service.calculate_hit_probability
      Calls hr_service.calculate_hr_probability
      Builds HitProbabilityResult and HRProbabilityResult
  Sorts hit_probabilities by hit_probability descending
  Sorts hr_probabilities by hr_probability descending
  Returns dict:
    date, generated_at, games, hit_probabilities,
    hr_probabilities, top_hit_plays (YES only),
    top_hr_plays (YES only), data_sources (list of what was used)

get_model_for_date(date_str: str) -> dict
  Checks cache first — returns cached model if fresh
  Otherwise calls build_daily_model and caches result
  Cache key: "model_{date_str}"
```

### services/historical_service.py
```
Purpose: Historical data queries

Functions:

get_player_career(
    player_name: str,
    pipeline: DataPipeline,
    start_year: int,
    end_year: int
) -> list[CareerSeason]
  Fetches year-by-year Savant + FanGraphs data for a player
  Returns list sorted ascending by year

get_all_time_leaders(
    stat: str,
    pipeline: DataPipeline,
    start_year: int,
    end_year: int,
    top_n: int
) -> list[dict]
  Available stats: HR, AVG, OPS, WAR, RBI, xwOBA, barrel_pct,
                   hard_hit_pct, exit_velocity
  Fetches multi-year data
  Aggregates by player
  Returns top_n sorted descending

compare_seasons(
    player_name: str,
    pipeline: DataPipeline,
    years: list[int]
) -> list[CareerSeason]
  Returns CareerSeasons for only those specific years
```

### services/live_tracker.py
```
Purpose: Background polling for live game updates

Class: LiveTracker

__init__(poll_interval: int)
  Initializes: active_games dict, stop_event threading.Event

start(game_pks: list[int]) -> None
  Starts background daemon thread
  Thread polls each game_pk every poll_interval seconds
  Calls mlb_api.get_live_feed for each game
  Updates self.active_games[game_pk] with latest data

stop() -> None
  Sets stop_event to halt polling thread

get_scores() -> list[dict]
  Returns current score + status for all active games

get_current_at_bat(game_pk: int) -> dict
  Returns: batter, pitcher, balls, strikes, outs, base runners

get_pitch_log(game_pk: int) -> list[dict]
  Returns all pitches this game: type, speed, zone, result, count

get_inning_scores(game_pk: int) -> list[dict]
  Returns run totals per inning for both teams
```

---

## WEB LAYER (web/)

### web/app.py
```
Flask app factory only.

create_app() -> Flask
  Creates Flask instance
  Registers blueprint from routes.py
  Sets template globals: current_date, app_version
  Returns app

No routes defined here.
```

### web/routes.py
```
All routes defined here using Flask Blueprint.

Instantiate Cache, DataPipeline, ModelBuilder once here
and reuse across all routes via app context.

Routes:

GET /
  Builds today's model via ModelBuilder
  Renders index.html

GET /date/<date_str>
  Builds model for any date
  Renders index.html

GET /player/<player_name>
  Fetches career stats via historical_service
  Renders player.html

GET /historical
  Renders historical.html

GET /game/<int:game_pk>
  Builds model for game's date
  Filters model to that specific game
  Renders game.html

GET /api/model
  Returns today's full model as JSON

GET /api/live
  Returns ESPN live scoreboard as JSON

GET /api/live/game/<int:game_pk>
  Returns live pitch log for a game as JSON

GET /api/historical
  Query: player, start_year, end_year
  Returns career stats as JSON

GET /api/leaders
  Query: stat, top_n, start_year, end_year
  Returns all-time leaders as JSON

GET /api/player/<player_name>/stats
  Returns full player career stats as JSON
```

---

## WEB TEMPLATES

### base.html
Dark theme. All pages extend this.

Colors (CSS variables in :root):
  --bg:       #0a0c10
  --panel:    #111318
  --border:   #1e2230
  --accent:   #e8ff00   (yellow — primary highlight)
  --red:      #ff4d4d
  --blue:     #00d4ff
  --text:     #e8eaf0
  --muted:    #5a6070
  --green:    #00e878

Fonts (Google Fonts):
  Bebas Neue — all headings and large display text
  DM Mono — all numbers, stats, data values, code
  Syne — body text, labels, navigation

Header:
  Left: App title MLB PROPS MODEL in Bebas Neue yellow
  Right: Current date badge

Navigation links:
  / → Today
  /historical → Historical
  Search box → navigates to /player/{name}

Include in <head>:
  Google Fonts link
  D3.js from cdnjs.cloudflare.com
  Chart.js from cdnjs.cloudflare.com
  /static/css/style.css
  Block for per-page CSS

Include before </body>:
  /static/js/main.js
  /static/js/live.js
  /static/js/strike_zone.js
  Block for per-page JavaScript

### index.html
Main daily model page. Four tabs:

TAB 1: Games
  Responsive grid of game cards
  Each card:
    Matchup header (Away @ Home) in Bebas Neue
    Game time local
    Venue name
    Weather: temp °F and wind speed mph and direction
    Dome indicator if applicable
    Away pitcher: name, ERA, xERA from Savant
    Home pitcher: name, ERA, xERA from Savant
    Click card → /game/{game_pk}

TAB 2: Hit Probability
  Search bar — filters table in real time as user types player name
  Filter buttons: All / YES / LEAN / NO
  Table is sortable — clicking any column header sorts ascending/descending
  Columns:
    Player (name + team + lineup position)
    Game (matchup shorthand)
    vs Pitcher
    AVG
    xBA        ← from Baseball Savant
    xwOBA      ← from Baseball Savant custom leaderboard NEW
    Hard Hit%  ← from Baseball Savant
    Sweet Spot% ← from Baseball Savant NEW
    Barrel%    ← from Baseball Savant
    Whiff%     ← from Baseball Savant custom leaderboard NEW (inverted — lower is better, shown in blue when low)
    Exit Velo  ← from Baseball Savant
    Hit Prob   (progress bar + percentage)
    Verdict    (colored badge: green=YES, yellow=LEAN, red=NO)

  Color code each row:
    YES verdict: subtle green left border
    LEAN verdict: subtle yellow left border
    NO verdict: no special styling

TAB 3: Home Runs
  Responsive card grid
  Each card shows:
    Player name in Bebas Neue
    Team and game info
    Stat grid (3 columns):
      Barrel%     Exit Velo     EV50        ← EV50 is new Savant field
      xwOBA       HR/Game%      Sweet Spot%  ← all from Savant
    Probability circle (red) with HR%
    Sportsbook odds if available from Odds API
    Value edge if odds available (model prob vs implied prob)
    2-3 word analysis note (matchup context)
    Top picks highlighted with yellow border

TAB 4: Live Scores
  Auto-refreshes every 30 seconds via live.js
  Shows each live game: score, inning, status
  Click any game to expand:
    Current at bat situation
    Pitch log table: #, Type, Speed, Zone, Result, Count
    Basic strike zone grid (1-13) with pitches plotted
  Completed games show as FINAL with score

### player.html
Individual player career page

Header section:
  Player name large in Bebas Neue
  Team, position, bats/throws
  2026 season highlight stats in large cards:
    AVG, xBA, xwOBA, HR, OPS, Barrel%, Hard Hit%, Exit Velo, EV50

Career stats table:
  One row per season from start_year to present
  Columns: Season, Team, G, PA, AVG, HR, RBI, OPS, xBA, xwOBA,
           Barrel%, Hard Hit%, Sweet Spot%, EV50, Whiff%, WAR
  Data sourced from Baseball Savant per year + FanGraphs fallback

Trends section:
  Line chart using Chart.js showing year-over-year:
    xBA trend
    Barrel% trend
    Hard Hit% trend
    xwOBA trend
  All on same chart with different colored lines

### historical.html
Historical data browser

Section 1 — Player Search:
  Text input for player name
  Start year selector (min 2015 for Savant, 2002 for FanGraphs)
  End year selector
  Load button
  Results load via AJAX to /api/historical
  Displays same career table as player.html

Section 2 — All-Time Leaders:
  Stat selector dropdown:
    HR, AVG, OPS, WAR, xwOBA, Barrel%, Hard Hit%, Exit Velo
  Year range selectors
  Top N selector: 10, 25, 50
  Load button
  Results table: Rank, Player, Team(s), Value

### game.html
Individual game detail page

Sections:
  Matchup header with team names and game info
  Weather details for this stadium

  Pitching matchup card:
    Each pitcher: ERA, xERA, K%, BB%, Hard Hit% allowed,
    Barrel% allowed, xwOBA allowed, Whiff% generated
    All from Baseball Savant where available

  Lineups section:
    Both team lineups side by side
    Each player shows:
      Lineup position, name, position, bats
      AVG, xBA, Barrel%, Hard Hit%, xwOBA
      Hit probability bar and percentage
      HR probability percentage
      Verdict badge

  Live game section (visible when game is in progress):
    Current score and inning
    Current at bat info
    Pitch log table
    Strike zone visualizer

---

## JAVASCRIPT FILES

### static/js/main.js
Tab switching logic — showTab(tabName, btnElement)
Table sorting — sortTable(tableId, columnIndex)
Table filtering — filterTable(tableId, verdict, btn)
Player search — searchTable(tableId, query)
Date navigation — navigateToDate(dateStr)

### static/js/live.js
Polls /api/live every 30 seconds
Updates score display for each live game
For expanded games: polls /api/live/game/{game_pk}
Appends new pitches to pitch log table
Calls strike_zone.js updateZone when new pitches arrive

### static/js/strike_zone.js
Uses D3.js

drawStrikeZone(containerId, pitches)
  Draws SVG strike zone
  3x3 grid for zones 1-9 (strike zone)
  Surrounding area for zones 10-13 (ball zones)
  Each pitch plotted as circle in correct zone
  Color coding:
    Called strike: #ff4d4d (red)
    Swinging strike: #ff8c00 (orange)
    Ball: #00d4ff (blue)
    In play hit: #00e878 (green)
    In play out: #5a6070 (muted)
    Home run: #e8ff00 (yellow)
  Tooltip on hover: pitch type, speed, count, result

updateZone(containerId, newPitches)
  Adds only new pitches since last update
  Animates new pitch dot appearing with small fade-in

clearZone(containerId)
  Removes all pitch dots

---

## MAIN.PY

CLI entry point.

Arguments:
  --date    YYYY-MM-DD (default today)
  --output  html / web / json (default html)
  --port    integer (default 5000)

html mode:
  Build model, render to mlb_model_{date}.html file, print path

web mode:
  Build model into cache
  Start Flask via create_app()
  Print: Server running at http://localhost:{port}
  Print: Friends on same network: http://{local_ip}:{port}

json mode:
  Build model, print JSON to stdout

---

## REQUIREMENTS.TXT

```
pybaseball>=2.2.7
requests>=2.31.0
pandas>=2.0.0
numpy>=1.24.0
flask>=3.0.0
jinja2>=3.1.0
beautifulsoup4>=4.12.0
lxml>=4.9.0
```

---

## DATA PRIORITY ORDER

Always attempt data sources in this order.
Fall back to next source only if previous fails or returns empty.

For batter Statcast metrics (xBA, barrel%, hard hit%, etc.):
  1. Baseball Savant statcast leaderboard (most precise, official Statcast)
  2. Baseball Savant custom leaderboard (xwOBA, whiff%, sweet spot%)
  3. pybaseball/FanGraphs (broader coverage, slightly less precise)
  4. Defaults from config normalization ranges midpoint

For pitcher stats (xERA, barrel% allowed, etc.):
  1. Baseball Savant expected stats and statcast leaderboard
  2. pybaseball/FanGraphs pitching stats
  3. MLB Stats API season stats
  4. Defaults

For schedules and lineups:
  1. MLB Stats API (official, always use this)
  2. ESPN as supplemental only

For live scores:
  1. MLB Stats API live feed (official, most detailed)
  2. ESPN scoreboard as backup

For weather:
  1. Open-Meteo (always — free and reliable)

---

## BASEBALL SAVANT STAT DEFINITIONS

Make sure these are used correctly in the model:

xBA (Expected Batting Average)
  Likelihood a batted ball becomes a hit based on exit velo + launch angle
  Range roughly .150 to .380 for qualified batters
  Higher = better for hit probability

xwOBA (Expected Weighted On-Base Average)
  Overall offensive value based on quality of contact
  Range roughly .250 to .450 for qualified batters
  Key indicator of true offensive skill vs luck
  Very useful for HR probability — accounts for power + contact

Barrel%  (Barrels per PA)
  Statcast's definition: batted ball with exit velo >= 98 mph
  AND launch angle in the range that produces HRs at that speed
  Best single predictor of home run probability

Hard Hit% (Hard Hit Rate)
  Exit velocity >= 95 mph
  Strong predictor of hits and power
  Correlates heavily with xBA and HR probability

Sweet Spot% (Sweet Spot Percentage)
  Launch angle between 8 and 32 degrees
  Balls hit at this angle have highest BABIP
  Key predictor of batting average on balls in play
  Distinct from ideal HR launch angle (25-35 degrees)

EV50 (50th percentile exit velocity of hardest batted balls)
  Average exit velo of a batter's hardest 50% of batted balls
  More stable than avg exit velo — less affected by weak contact
  Better power indicator than average exit velo

Whiff% (Whiff Percentage)
  Swings and misses / total swings
  INVERSE relationship with hit probability
  High whiff% = fewer balls in play = lower hit probability
  Used inverted in hit probability formula

xERA (Expected ERA for pitchers)
  ERA predicted from quality of contact allowed (xwOBA allowed)
  Better predictor of future performance than ERA
  Key input to pitcher matchup score

---

## WHAT THE APP SHOULD DO ON FIRST RUN

User runs: python main.py --output web

App should:
1. Create .cache directory
2. Fetch today's MLB schedule from MLB Stats API
3. Fetch Baseball Savant statcast leaderboard for batters (2026)
4. Fetch Baseball Savant custom leaderboard for batters (2026)
5. Fetch Baseball Savant statcast leaderboard for pitchers (2026)
6. Fetch Baseball Savant expected stats for pitchers (2026)
7. If any Savant fetch fails, fall back to pybaseball/FanGraphs
8. Fetch park factors
9. For each game today:
   a. Fetch weather for the stadium
   b. Get probable pitchers from schedule
   c. Look up each pitcher's Savant stats
10. When lineups confirm (~1 hour before first pitch):
    a. Look up each confirmed batter's Savant stats
    b. Get recent 14-day form for each batter from Savant
    c. Calculate hit and HR probability for each player
11. Start Flask server
12. Print URL to console
13. App is ready — open browser and see full model

If lineups not yet confirmed:
  Show probable pitcher matchup info on games tab
  Show placeholder on hit/HR tabs with note: "Lineups not yet posted"
  Live tab shows scheduled games with TBD status
  Auto-refresh every 5 minutes looking for lineup confirmation

---

## FINAL INSTRUCTIONS FOR CLAUDE CODE

1. Read this entire spec before writing any code
2. Build in this exact order:
   config.py → models/ → api/ → data/ → services/ → web/ → main.py
3. After each layer is built, verify imports are correct
4. Do not skip any file in the folder structure
5. Do not combine files — every file listed must be its own file
6. After building all files, run a final import check across all modules
7. Provide exact commands to install and run
8. The Baseball Savant integration in api/baseball_savant_api.py
   is the highest priority in the API layer — spend the most time
   getting this right since it is the primary data source
9. All probability formulas use only config.py values — never hardcode
   a weight, threshold, or normalization range in a service file
10. The app must start and run without errors on first launch
```
