# data/static_park_factors.py
# Static MLB stadium park factor dataset — multi-year (2022-2024) averages.
#
# Factors are normalized to 100 = league average.
# Values >100 favour hitters; <100 favour pitchers.
#
# Sources: Baseball Reference Park Factors, FanGraphs, Statcast environment data.
# Updated: 2025 season. Refresh annually if significant park changes occur.
#
# Usage:
#   from data.static_park_factors import lookup_park_factors
#   ctx = lookup_park_factors(venue_name="Yankee Stadium", home_team_abbr="NYY")

from __future__ import annotations
import logging

logger = logging.getLogger(__name__)

# ── Per-team park factor records ───────────────────────────────────────────────
# Keys are the canonical 2-3 letter team abbreviation used throughout this app.
#
# Fields:
#   venue           – Primary display name (matches MLB schedule API where possible)
#   run_factor      – Overall run factor (100 = league avg)
#   hr_factor       – HR factor (100 = league avg)
#   hit_factor      – Hit factor (100 = league avg)
#   lhb_hr_factor   – LH batter HR factor (None if not significantly asymmetric)
#   rhb_hr_factor   – RH batter HR factor (None if not significantly asymmetric)
#   is_dome         – True for fully enclosed / permanent roof
#   is_retractable  – True for retractable / convertible roof (roof often closed)
#   tendency_notes  – 1-2 sentence plain-English park tendency summary for Gemini
STATIC_PARK_FACTORS: dict[str, dict] = {
    "ARI": {
        "venue":          "Chase Field",
        "run_factor":     99,
        "hr_factor":      104,
        "hit_factor":     99,
        "lhb_hr_factor":  None,
        "rhb_hr_factor":  None,
        "is_dome":        False,
        "is_retractable": True,
        "tendency_notes": (
            "The roof is usually closed, so weather rarely matters. "
            "When it is open, the dry desert heat makes the ball carry further and home runs are easier to hit. "
            "Either way, this stadium is slightly above average for home runs."
        ),
    },
    "ATL": {
        "venue":          "Truist Park",
        "run_factor":     97,
        "hr_factor":      100,
        "hit_factor":     98,
        "lhb_hr_factor":  None,
        "rhb_hr_factor":  None,
        "is_dome":        False,
        "is_retractable": False,
        "tendency_notes": (
            "A fair, balanced stadium. Scoring and home runs land right around the MLB average. "
            "Neither hitters nor pitchers have a meaningful advantage here."
        ),
    },
    "BAL": {
        "venue":          "Camden Yards",
        "run_factor":     100,
        "hr_factor":      106,
        "hit_factor":     100,
        "lhb_hr_factor":  None,
        "rhb_hr_factor":  None,
        "is_dome":        False,
        "is_retractable": False,
        "tendency_notes": (
            "More home runs get hit here than at the average MLB stadium. "
            "The left field wall is shorter than most parks, giving right-handed hitters a good target. "
            "Overall scoring is close to league average."
        ),
    },
    "BOS": {
        "venue":          "Fenway Park",
        "run_factor":     104,
        "hr_factor":      104,
        "hit_factor":     106,
        "lhb_hr_factor":  113,
        "rhb_hr_factor":  95,
        "is_dome":        False,
        "is_retractable": False,
        "tendency_notes": (
            "One of the best hitter's parks in baseball. "
            "The famous 37-foot left field wall makes it much easier for right-handed hitters to get extra-base hits and home runs. "
            "Left-handed hitters have a deeper right field to deal with, but the park still produces above-average offense overall."
        ),
    },
    "CHC": {
        "venue":          "Wrigley Field",
        "run_factor":     102,
        "hr_factor":      104,
        "hit_factor":     103,
        "lhb_hr_factor":  None,
        "rhb_hr_factor":  None,
        "is_dome":        False,
        "is_retractable": False,
        "tendency_notes": (
            "Wind matters more here than almost anywhere else in baseball. "
            "When wind blows toward the outfield, it is one of the best home run environments in the league. "
            "When wind blows in from Lake Michigan, scoring drops sharply. "
            "Always check the wind direction before betting on this game."
        ),
    },
    "CWS": {
        "venue":          "Guaranteed Rate Field",
        "run_factor":     104,
        "hr_factor":      112,
        "hit_factor":     103,
        "lhb_hr_factor":  None,
        "rhb_hr_factor":  None,
        "is_dome":        False,
        "is_retractable": False,
        "tendency_notes": (
            "One of the better home run parks in the American League. "
            "The right field seats are closer than most stadiums, giving left-handed hitters a clear advantage. "
            "Overall offense is above average here."
        ),
    },
    "CIN": {
        "venue":          "Great American Ball Park",
        "run_factor":     107,
        "hr_factor":      118,
        "hit_factor":     106,
        "lhb_hr_factor":  128,
        "rhb_hr_factor":  107,
        "is_dome":        False,
        "is_retractable": False,
        "tendency_notes": (
            "One of the best home run parks in all of baseball. "
            "The short fences and humid Ohio River air help the ball carry farther. "
            "Left-handed hitters get the biggest boost — the right field seats are very close to home plate."
        ),
    },
    "CLE": {
        "venue":          "Progressive Field",
        "run_factor":     97,
        "hr_factor":      94,
        "hit_factor":     97,
        "lhb_hr_factor":  None,
        "rhb_hr_factor":  None,
        "is_dome":        False,
        "is_retractable": False,
        "tendency_notes": (
            "A pitcher-friendly park. The outfield is spacious, making it harder than average to hit home runs. "
            "Cold winds off Lake Erie in early and late season make it even tougher on hitters."
        ),
    },
    "COL": {
        "venue":          "Coors Field",
        "run_factor":     117,
        "hr_factor":      126,
        "hit_factor":     119,
        "lhb_hr_factor":  132,
        "rhb_hr_factor":  120,
        "is_dome":        False,
        "is_retractable": False,
        "tendency_notes": (
            "The most extreme hitter's park in baseball, by far. "
            "Denver sits a mile above sea level. At that altitude, the thin air makes the ball travel much farther than at any other stadium — "
            "pitches also break less, which makes them easier to hit. "
            "Expect significantly more runs and home runs than in any other park."
        ),
    },
    "DET": {
        "venue":          "Comerica Park",
        "run_factor":     95,
        "hr_factor":      89,
        "hit_factor":     96,
        "lhb_hr_factor":  None,
        "rhb_hr_factor":  None,
        "is_dome":        False,
        "is_retractable": False,
        "tendency_notes": (
            "One of the hardest parks in baseball to hit a home run. "
            "The outfield walls are very deep, especially in center and left-center, so well-hit balls that would leave most stadiums are caught here. "
            "Overall hits are close to average, but power hitters are at a real disadvantage."
        ),
    },
    "HOU": {
        "venue":          "Minute Maid Park",
        "run_factor":     99,
        "hr_factor":      97,
        "hit_factor":     99,
        "lhb_hr_factor":  None,
        "rhb_hr_factor":  None,
        "is_dome":        False,
        "is_retractable": True,
        "tendency_notes": (
            "A well-balanced, fair park. The retractable roof is often closed, so weather rarely affects play. "
            "The left field seats are very close to home plate, giving right-handed hitters a tempting short target for home runs. "
            "Overall scoring and home run rates are close to the MLB average."
        ),
    },
    "KC": {
        "venue":          "Kauffman Stadium",
        "run_factor":     95,
        "hr_factor":      92,
        "hit_factor":     97,
        "lhb_hr_factor":  None,
        "rhb_hr_factor":  None,
        "is_dome":        False,
        "is_retractable": False,
        "tendency_notes": (
            "A pitcher-friendly park with a large outfield. Fewer home runs and overall runs are scored here than at an average stadium. "
            "Singles and doubles happen at a more normal rate, but home run hitters lose a noticeable edge in this stadium."
        ),
    },
    "LAA": {
        "venue":          "Angel Stadium",
        "run_factor":     97,
        "hr_factor":      97,
        "hit_factor":     98,
        "lhb_hr_factor":  None,
        "rhb_hr_factor":  None,
        "is_dome":        False,
        "is_retractable": False,
        "tendency_notes": (
            "A fairly neutral park that leans slightly toward pitchers. "
            "On cool Southern California evenings, the coastal air can make the ball carry less, reducing home runs. "
            "Neither hitters nor pitchers have a dramatic advantage here."
        ),
    },
    "LAD": {
        "venue":          "Dodger Stadium",
        "run_factor":     96,
        "hr_factor":      94,
        "hit_factor":     97,
        "lhb_hr_factor":  None,
        "rhb_hr_factor":  None,
        "is_dome":        False,
        "is_retractable": False,
        "tendency_notes": (
            "A pitcher-friendly park. The large outfield and cool ocean air blowing in from the Pacific reduce how far the ball travels. "
            "Home runs are harder to hit here than at an average stadium, and overall scoring tends to be a bit lower than usual."
        ),
    },
    "MIA": {
        "venue":          "loanDepot Park",
        "run_factor":     92,
        "hr_factor":      88,
        "hit_factor":     93,
        "lhb_hr_factor":  None,
        "rhb_hr_factor":  None,
        "is_dome":        False,
        "is_retractable": True,
        "tendency_notes": (
            "One of the most pitcher-friendly parks in baseball. The stadium is large and the roof keeps conditions controlled, "
            "but the deep outfield walls mean fewer home runs than almost anywhere else in the league. "
            "Expect lower-scoring games here."
        ),
    },
    "MIL": {
        "venue":          "American Family Field",
        "run_factor":     100,
        "hr_factor":      103,
        "hit_factor":     100,
        "lhb_hr_factor":  None,
        "rhb_hr_factor":  None,
        "is_dome":        False,
        "is_retractable": True,
        "tendency_notes": (
            "A fair, well-balanced park. The retractable roof limits the impact of weather. "
            "Home runs are hit at a slightly above-average rate, but overall scoring is close to the MLB average."
        ),
    },
    "MIN": {
        "venue":          "Target Field",
        "run_factor":     97,
        "hr_factor":      96,
        "hit_factor":     97,
        "lhb_hr_factor":  None,
        "rhb_hr_factor":  None,
        "is_dome":        False,
        "is_retractable": False,
        "tendency_notes": (
            "A slightly pitcher-friendly park in Minnesota's cold climate. "
            "Early and late in the season, frigid temperatures make scoring even tougher. "
            "In the warmer summer months, conditions are more neutral, but home runs are still a little below average year-round."
        ),
    },
    "NYM": {
        "venue":          "Citi Field",
        "run_factor":     94,
        "hr_factor":      92,
        "hit_factor":     95,
        "lhb_hr_factor":  None,
        "rhb_hr_factor":  None,
        "is_dome":        False,
        "is_retractable": False,
        "tendency_notes": (
            "A clear pitcher's park. The large outfield and cool New York coastal air reduce how far the ball travels. "
            "Home runs are meaningfully harder to hit here than at the average stadium — "
            "one of the toughest parks for home run hitters in the National League."
        ),
    },
    "NYY": {
        "venue":          "Yankee Stadium",
        "run_factor":     104,
        "hr_factor":      118,
        "hit_factor":     103,
        "lhb_hr_factor":  133,
        "rhb_hr_factor":  103,
        "is_dome":        False,
        "is_retractable": False,
        "tendency_notes": (
            "One of the best home run parks in baseball. "
            "The right field seats are only 314 feet from home plate — one of the shortest distances in the league — "
            "giving left-handed hitters a huge advantage. "
            "Overall, home runs are hit here at a dramatically higher rate than at most stadiums."
        ),
    },
    "ATH": {
        "venue":          "Sutter Health Park",
        "run_factor":     102,
        "hr_factor":      105,
        "hit_factor":     101,
        "lhb_hr_factor":  None,
        "rhb_hr_factor":  None,
        "is_dome":        False,
        "is_retractable": False,
        "tendency_notes": (
            "The Athletics' current home in Sacramento. "
            "The warm inland California air helps the ball carry a bit farther than average, "
            "making this a slightly favorable environment for home runs and overall offense."
        ),
    },
    "OAK": {
        "venue":          "Sutter Health Park",
        "run_factor":     102,
        "hr_factor":      105,
        "hit_factor":     101,
        "lhb_hr_factor":  None,
        "rhb_hr_factor":  None,
        "is_dome":        False,
        "is_retractable": False,
        "tendency_notes": (
            "The Athletics' current home in Sacramento. "
            "The warm inland California air helps the ball carry a bit farther than average, "
            "making this a slightly favorable environment for home runs and overall offense."
        ),
    },
    "PHI": {
        "venue":          "Citizens Bank Park",
        "run_factor":     105,
        "hr_factor":      112,
        "hit_factor":     104,
        "lhb_hr_factor":  121,
        "rhb_hr_factor":  104,
        "is_dome":        False,
        "is_retractable": False,
        "tendency_notes": (
            "One of the best hitter's parks in the National League. "
            "Hot and humid Philadelphia summers help the ball carry farther. "
            "The right field wall is closer than most stadiums, giving left-handed hitters a real advantage for home runs. "
            "Expect above-average scoring and home run rates here."
        ),
    },
    "PIT": {
        "venue":          "PNC Park",
        "run_factor":     94,
        "hr_factor":      95,
        "hit_factor":     95,
        "lhb_hr_factor":  None,
        "rhb_hr_factor":  None,
        "is_dome":        False,
        "is_retractable": False,
        "tendency_notes": (
            "A pitcher-friendly park. The outfield walls are deep and the breezes off the Allegheny River "
            "can work against hitters, making it harder to hit home runs and keeping overall scoring below average."
        ),
    },
    "SD": {
        "venue":          "Petco Park",
        "run_factor":     93,
        "hr_factor":      89,
        "hit_factor":     94,
        "lhb_hr_factor":  83,
        "rhb_hr_factor":  95,
        "is_dome":        False,
        "is_retractable": False,
        "tendency_notes": (
            "One of the most pitcher-friendly parks in baseball. "
            "The deep outfield walls and cool San Diego ocean air make it very hard to hit home runs. "
            "Left-handed hitters are especially affected — home runs are significantly below average for them here. "
            "Expect lower run totals in games at this stadium."
        ),
    },
    "SEA": {
        "venue":          "T-Mobile Park",
        "run_factor":     96,
        "hr_factor":      94,
        "hit_factor":     97,
        "lhb_hr_factor":  None,
        "rhb_hr_factor":  None,
        "is_dome":        False,
        "is_retractable": True,
        "tendency_notes": (
            "A pitcher-friendly park. The cool, damp Pacific Northwest air — even when the roof is open — "
            "keeps the ball from carrying well. Home runs are harder to hit here than at the average stadium, "
            "and overall scoring tends to run below the league average."
        ),
    },
    "SF": {
        "venue":          "Oracle Park",
        "run_factor":     93,
        "hr_factor":      88,
        "hit_factor":     94,
        "lhb_hr_factor":  None,
        "rhb_hr_factor":  None,
        "is_dome":        False,
        "is_retractable": False,
        "tendency_notes": (
            "One of the toughest stadiums for home run hitters in all of baseball. "
            "Cold, foggy air rolling in off San Francisco Bay significantly reduces how far the ball travels. "
            "The deep outfield walls add to the challenge — this is one of the lowest home run rates in the sport. "
            "Pitchers have a major advantage at this stadium."
        ),
    },
    "STL": {
        "venue":          "Busch Stadium",
        "run_factor":     97,
        "hr_factor":      91,
        "hit_factor":     98,
        "lhb_hr_factor":  None,
        "rhb_hr_factor":  None,
        "is_dome":        False,
        "is_retractable": False,
        "tendency_notes": (
            "A pitcher-friendly park, especially for home runs. "
            "The large outfield means that balls hit to the gaps are more likely to be caught than at most stadiums. "
            "Scoring overall is slightly below average — expect closer, lower-scoring games here."
        ),
    },
    "TB": {
        "venue":          "Tropicana Field",
        "run_factor":     97,
        "hr_factor":      96,
        "hit_factor":     97,
        "lhb_hr_factor":  None,
        "rhb_hr_factor":  None,
        "is_dome":        True,
        "is_retractable": False,
        "tendency_notes": (
            "A fully enclosed dome — weather never affects this game. "
            "Scoring and home runs are slightly below the MLB average. "
            "The stadium has unusual roof supports that hang over the field, and on rare occasions "
            "a ball hits one of them, which can affect the play."
        ),
    },
    "TEX": {
        "venue":          "Globe Life Field",
        "run_factor":     102,
        "hr_factor":      102,
        "hit_factor":     101,
        "lhb_hr_factor":  None,
        "rhb_hr_factor":  None,
        "is_dome":        False,
        "is_retractable": True,
        "tendency_notes": (
            "A slight hitter's advantage. The retractable roof protects fans from the intense Texas heat, "
            "but the warm, dry air inside still helps the ball carry well. "
            "Home runs and overall scoring are a bit above the MLB average."
        ),
    },
    "TOR": {
        "venue":          "Rogers Centre",
        "run_factor":     100,
        "hr_factor":      104,
        "hit_factor":     100,
        "lhb_hr_factor":  None,
        "rhb_hr_factor":  None,
        "is_dome":        False,
        "is_retractable": True,
        "tendency_notes": (
            "A fair, near-neutral park. The fully enclosed roof completely eliminates weather as a factor. "
            "Home runs are hit at a slightly above-average rate, while overall scoring is close to the MLB average."
        ),
    },
    "WSH": {
        "venue":          "Nationals Park",
        "run_factor":     98,
        "hr_factor":      98,
        "hit_factor":     98,
        "lhb_hr_factor":  None,
        "rhb_hr_factor":  None,
        "is_dome":        False,
        "is_retractable": False,
        "tendency_notes": (
            "A fair, well-balanced park. Scoring and home run rates are close to the MLB average. "
            "Hot and humid Washington DC summers can help the ball carry a bit farther, but the effect is modest."
        ),
    },
}

# ── Venue name → team abbreviation aliases ────────────────────────────────────
# Maps the venue name strings returned by the MLB schedule API to team abbrevs.
# Include common variations to handle API inconsistencies.
VENUE_ALIASES: dict[str, str] = {
    # Arizona
    "Chase Field":                    "ARI",
    # Atlanta
    "Truist Park":                    "ATL",
    "SunTrust Park":                  "ATL",
    # Baltimore
    "Camden Yards":                   "BAL",
    "Oriole Park at Camden Yards":    "BAL",
    "Oriole Park":                    "BAL",
    # Boston
    "Fenway Park":                    "BOS",
    # Chicago Cubs
    "Wrigley Field":                  "CHC",
    # Chicago White Sox
    "Guaranteed Rate Field":          "CWS",
    "Rate Field":                     "CWS",
    # Cincinnati
    "Great American Ball Park":       "CIN",
    "Great American Ballpark":        "CIN",
    # Cleveland
    "Progressive Field":              "CLE",
    # Colorado
    "Coors Field":                    "COL",
    # Detroit
    "Comerica Park":                  "DET",
    # Houston
    "Minute Maid Park":               "HOU",
    "Daikin Park":                    "HOU",
    # Kansas City
    "Kauffman Stadium":               "KC",
    # LA Angels
    "Angel Stadium":                  "LAA",
    "Angel Stadium of Anaheim":       "LAA",
    # LA Dodgers
    "Dodger Stadium":                 "LAD",
    "UNIQLO Field at Dodger Stadium": "LAD",
    # Miami
    "loanDepot park":                 "MIA",   # exact casing from MLB API (lowercase p)
    "loanDepot Park":                 "MIA",
    "LoanDepot Park":                 "MIA",
    "Loan Depot Park":                "MIA",
    "Marlins Park":                   "MIA",
    # Milwaukee
    "American Family Field":          "MIL",
    "Miller Park":                    "MIL",
    # Minnesota
    "Target Field":                   "MIN",
    # NY Mets
    "Citi Field":                     "NYM",
    # NY Yankees
    "Yankee Stadium":                 "NYY",
    # Oakland/Athletics
    "Sutter Health Park":             "ATH",
    "Oakland Coliseum":               "ATH",
    "Oakland-Alameda County Coliseum": "ATH",
    "RingCentral Coliseum":           "ATH",
    # Philadelphia
    "Citizens Bank Park":             "PHI",
    # Pittsburgh
    "PNC Park":                       "PIT",
    # San Diego
    "Petco Park":                     "SD",
    # Seattle
    "T-Mobile Park":                  "SEA",
    "Safeco Field":                   "SEA",
    # San Francisco
    "Oracle Park":                    "SF",
    "AT&T Park":                      "SF",
    # St. Louis
    "Busch Stadium":                  "STL",
    # Tampa Bay
    "Tropicana Field":                "TB",
    # Texas
    "Globe Life Field":               "TEX",
    "Globe Life Park in Arlington":   "TEX",
    # Toronto
    "Rogers Centre":                  "TOR",
    "Rogers Center":                  "TOR",
    # Washington
    "Nationals Park":                 "WSH",
    "Nationals Stadium":              "WSH",
}

# Neutral fallback used when no park data can be found
_NEUTRAL: dict = {
    "available":       True,
    "source":          "neutral_fallback",
    "team":            None,
    "run_factor":      100,
    "hr_factor":       100,
    "hit_factor":      100,
    "lhb_hr_factor":   None,
    "rhb_hr_factor":   None,
    "is_dome":         False,
    "is_retractable":  False,
    "tendency_notes":  "Park factor data unavailable; using league-average neutral values.",
}


def lookup_park_factors(
    venue_name: str,
    home_team_abbr: str = "",
) -> dict:
    """Return park factor data for a game venue.

    Lookup order:
      1. Exact venue name → VENUE_ALIASES → STATIC_PARK_FACTORS
      2. Case-insensitive venue name match through aliases
      3. Partial venue name match (first token)
      4. home_team_abbr directly in STATIC_PARK_FACTORS
      5. Neutral fallback (100 for all factors) with WARNING log

    Returns a dict with keys: available, source, team, run_factor, hr_factor,
    hit_factor, lhb_hr_factor, rhb_hr_factor, is_dome, is_retractable,
    tendency_notes, venue.
    """
    venue_clean = (venue_name or "").strip()
    abbr_clean  = (home_team_abbr or "").strip().upper()

    # ── Step 1: exact alias match (case-sensitive) ────────────────────────────
    team = VENUE_ALIASES.get(venue_clean)

    # ── Step 2: case-insensitive alias match ──────────────────────────────────
    if not team:
        venue_lower = venue_clean.lower()
        for alias, t in VENUE_ALIASES.items():
            if alias.lower() == venue_lower:
                team = t
                break

    # ── Step 3: partial first-word match ─────────────────────────────────────
    if not team and " " in venue_clean:
        first = venue_clean.split()[0].lower()
        for alias, t in VENUE_ALIASES.items():
            if alias.lower().startswith(first):
                team = t
                break

    # ── Step 4: team abbreviation fallback ────────────────────────────────────
    # Normalise non-standard abbreviations used by some MLB API endpoints.
    _ABBR_MAP = {"AZ": "ARI", "OAK": "ATH", "KC": "KC"}
    if not team and abbr_clean:
        normalised = _ABBR_MAP.get(abbr_clean, abbr_clean)
        if normalised in STATIC_PARK_FACTORS:
            team = normalised

    if team and team in STATIC_PARK_FACTORS:
        entry = STATIC_PARK_FACTORS[team]
        result = {
            "available":      True,
            "source":         "static",
            "team":           team,
            "venue":          venue_clean or entry["venue"],
            "run_factor":     entry["run_factor"],
            "hr_factor":      entry["hr_factor"],
            "hit_factor":     entry["hit_factor"],
            "lhb_hr_factor":  entry.get("lhb_hr_factor"),
            "rhb_hr_factor":  entry.get("rhb_hr_factor"),
            "is_dome":        entry.get("is_dome", False),
            "is_retractable": entry.get("is_retractable", False),
            "tendency_notes": entry.get("tendency_notes", ""),
        }
        logger.debug(
            "Park factors: found  venue=%r  team=%s  source=static  "
            "run=%d  hr=%d  hit=%d  dome=%s  retractable=%s",
            venue_clean, team,
            entry["run_factor"], entry["hr_factor"], entry["hit_factor"],
            entry.get("is_dome"), entry.get("is_retractable"),
        )
        return result

    # ── Step 5: neutral fallback ──────────────────────────────────────────────
    logger.warning(
        "Park factors: lookup FAILED  venue=%r  home_team=%r  "
        "→ returning neutral 100 for all factors",
        venue_clean, abbr_clean or "(none)",
    )
    return {
        **_NEUTRAL,
        "venue": venue_clean,
    }
