# api/mlb_api.py
# Official MLB Stats API — free, no key required.

import logging
from typing import Any

import requests

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import config

logger = logging.getLogger(__name__)

_cache = None


def set_cache(cache: Any) -> None:
    """Inject cache instance."""
    global _cache
    _cache = cache


def _get(url: str, params: dict | None = None) -> dict:
    """GET helper — returns parsed JSON dict or empty dict on failure."""
    try:
        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        logger.warning("MLB API request failed (%s): %s", url, exc)
        return {}


def get_schedule(date_str: str) -> list[dict]:
    """Fetch MLB schedule for a given date.

    Today's schedule uses a short 30-minute TTL so official lineups (posted
    ~3 h before first pitch) are picked up automatically. Past and future
    dates keep the default 12-hour TTL.

    Args:
        date_str: Date in YYYY-MM-DD format.

    Returns:
        List of raw game dicts from the MLB API.
    """
    from datetime import date as _date
    is_today = date_str == _date.today().isoformat()
    ttl = 0.5 if is_today else None  # 30 min for today, default otherwise

    cache_key = f"mlb_schedule_{date_str}"
    if _cache:
        cached = _cache.get(cache_key, ttl_hours=ttl)
        if cached is not None:
            return cached

    url = f"{config.MLB_API_BASE_URL}/schedule"
    params = {
        "date":    date_str,
        "sportId": 1,
        "hydrate": "probablePitcher,lineups,team,venue(timezone),weather",
    }
    data = _get(url, params)
    games: list[dict] = []
    for date_entry in data.get("dates", []):
        games.extend(date_entry.get("games", []))

    if _cache and games:
        _cache.set(cache_key, games)
    return games


def get_live_feed(game_pk: int) -> dict:
    """Fetch live game feed — current at-bat, pitch log, scores.

    Args:
        game_pk: MLB game primary key.

    Returns:
        Raw live feed dict.
    """
    url = f"{config.MLB_API_LIVE_URL}/game/{game_pk}/feed/live"
    return _get(url)


def get_boxscore(game_pk: int) -> dict:
    """Fetch completed game boxscore.

    Args:
        game_pk: MLB game primary key.

    Returns:
        Raw boxscore dict.
    """
    cache_key = f"mlb_boxscore_{game_pk}"
    if _cache:
        cached = _cache.get(cache_key)
        if cached is not None:
            return cached

    url = f"{config.MLB_API_BASE_URL}/game/{game_pk}/boxscore"
    data = _get(url)
    if _cache and data:
        _cache.set(cache_key, data)
    return data


def get_player_info(player_id: int) -> dict:
    """Fetch player profile including season hitting stats.

    Args:
        player_id: MLBAM player ID.

    Returns:
        Raw player info dict.
    """
    cache_key = f"mlb_player_{player_id}"
    if _cache:
        cached = _cache.get(cache_key)
        if cached is not None:
            return cached

    url = f"{config.MLB_API_BASE_URL}/people/{player_id}"
    params = {"hydrate": "stats(group=hitting,type=season)"}
    data = _get(url, params)
    if _cache and data:
        _cache.set(cache_key, data)
    return data


def get_players_bat_sides(player_ids: list[int]) -> dict[int, str]:
    """Batch-fetch batting handedness for a list of player IDs in one API call.

    Uses /people?personIds=... which returns all players at once.
    Results are cached per player so repeat calls are instant.

    Args:
        player_ids: List of MLBAM player IDs.

    Returns:
        Dict mapping player_id → 'L', 'R', or 'S'.
    """
    if not player_ids:
        return {}

    result: dict[int, str] = {}
    to_fetch: list[int] = []

    # Serve from cache where possible
    for pid in player_ids:
        cache_key = f"mlb_bat_side_{pid}"
        if _cache:
            cached = _cache.get(cache_key)
            if cached is not None:
                result[pid] = cached
                continue
        to_fetch.append(pid)

    if not to_fetch:
        return result

    # Batch in chunks of 60 (API limit)
    try:
        for i in range(0, len(to_fetch), 60):
            chunk = to_fetch[i:i + 60]
            url = f"{config.MLB_API_BASE_URL}/people"
            params = {"personIds": ",".join(str(p) for p in chunk)}
            data = _get(url, params)
            for person in data.get("people", []):
                pid = person.get("id", 0)
                if not pid:
                    continue
                bat_side = person.get("batSide", {})
                code = bat_side.get("code", "R") if isinstance(bat_side, dict) else str(bat_side or "R")
                code = code.upper()[0] if code else "R"
                if code not in ("L", "R", "S"):
                    code = "R"
                result[pid] = code
                if _cache:
                    _cache.set(f"mlb_bat_side_{pid}", code)
    except Exception as exc:
        logger.warning("Batch bat-side lookup failed: %s", exc)

    # Fill any misses with default
    for pid in to_fetch:
        result.setdefault(pid, "R")

    return result


def get_player_bat_side(player_id: int) -> str:
    """Return the batter's hitting hand: 'L', 'R', or 'S' (switch).

    Calls /people/{player_id} which always includes batSide.
    Result is cached permanently (handedness never changes).

    Args:
        player_id: MLBAM player ID.

    Returns:
        'L', 'R', or 'S'. Defaults to 'R' on failure.
    """
    cache_key = f"mlb_bat_side_{player_id}"
    if _cache:
        cached = _cache.get(cache_key)
        if cached is not None:
            return cached

    url = f"{config.MLB_API_BASE_URL}/people/{player_id}"
    data = _get(url)
    people = data.get("people", [])
    if not people:
        return "R"

    bat_side = people[0].get("batSide", {})
    code = bat_side.get("code", "R") if isinstance(bat_side, dict) else str(bat_side or "R")
    code = code.upper()[0] if code else "R"
    if code not in ("L", "R", "S"):
        code = "R"

    if _cache:
        _cache.set(cache_key, code)
    return code


def get_player_stats(player_id: int, group: str, season: int) -> dict:
    """Fetch a player's season stats for a given stat group from the MLB Stats API.

    Used as a fallback when Baseball Savant and FanGraphs data are unavailable.
    Returns a flat dict of stat keys so normalizer.py can read them directly.

    Args:
        player_id: MLBAM player ID.
        group: Stat group — 'hitting' or 'pitching'.
        season: Season year (e.g. 2026).

    Returns:
        Flat dict of stats (e.g. {'era': 3.52, 'avg': 0.271, ...}),
        empty dict if player not found or API failure.
    """
    cache_key = f"mlb_stats_{player_id}_{group}_{season}"
    if _cache:
        cached = _cache.get(cache_key)
        if cached is not None:
            return cached

    url = f"{config.MLB_API_BASE_URL}/people/{player_id}/stats"
    params = {
        "stats":  "season",
        "group":  group,
        "season": season,
    }
    data = _get(url, params)

    # Drill into the nested stats structure
    flat: dict = {}
    try:
        stats_list = data.get("stats", [])
        for stat_group in stats_list:
            splits = stat_group.get("splits", [])
            if splits:
                raw = splits[0].get("stat", {})
                flat.update(raw)
    except Exception as exc:
        logger.warning("Failed to parse player stats (id=%s group=%s): %s", player_id, group, exc)

    if _cache and flat:
        _cache.set(cache_key, flat)
    return flat


def get_team_roster(team_id: int, season: int) -> list[dict]:
    """Fetch the active roster for a team, excluding pitchers.

    Used to build a probable lineup when official lineups are not yet posted.

    Args:
        team_id: MLB team ID.
        season: Season year.

    Returns:
        List of dicts with id, fullName, position for each position player.
        Sorted by jersey number as a rough proxy for lineup slot.
    """
    cache_key = f"mlb_roster_{team_id}_{season}"
    if _cache:
        cached = _cache.get(cache_key)
        if cached is not None:
            return cached

    url = f"{config.MLB_API_BASE_URL}/teams/{team_id}/roster"
    params = {"rosterType": "active", "season": season, "hydrate": "person"}
    data = _get(url, params)

    players: list[dict] = []
    for entry in data.get("roster", []):
        pos  = entry.get("position", {})
        pos_type = pos.get("type", "")
        pos_abbr = pos.get("abbreviation", "")
        # Skip pitchers and two-way players primarily used as pitchers
        if pos_type == "Pitcher" or pos_abbr == "P":
            continue
        person = entry.get("person", {})
        pid = person.get("id", 0)
        if not pid:
            continue
        bat_side = person.get("batSide", {})
        hand = (
            bat_side.get("code", "R")
            if isinstance(bat_side, dict)
            else str(bat_side) if bat_side else "R"
        )
        players.append({
            "id":       pid,
            "fullName": person.get("fullName", ""),
            "position": pos_abbr,
            "hand":     hand,
        })

    if _cache and players:
        _cache.set(cache_key, players)
    return players


def get_recent_boxscore_lineup(team_id: int, before_date: str) -> tuple[list[dict], str]:
    """Find the most recent completed game for a team and return its batting order.

    Searches back up to 7 days from before_date. Returns (lineup, source_date).
    Lineup is a list of player dicts ordered by batting position.

    Args:
        team_id: MLB team ID.
        before_date: YYYY-MM-DD — search for games strictly before this date.

    Returns:
        (player_list, source_date_str) — empty list and empty string on failure.
    """
    from datetime import datetime as dt, timedelta

    cache_key = f"mlb_recent_bs_lineup_{team_id}_{before_date}"
    if _cache:
        cached = _cache.get(cache_key)
        if cached is not None:
            return cached.get("lineup", []), cached.get("date", "")

    try:
        base = dt.strptime(before_date, "%Y-%m-%d")
        for days_back in range(1, 8):
            check_date = (base - timedelta(days=days_back)).strftime("%Y-%m-%d")

            # Get schedule for that date filtered to this team
            url = f"{config.MLB_API_BASE_URL}/schedule"
            params = {
                "date":    check_date,
                "teamId":  team_id,
                "sportId": 1,
                "gameType": "R",
            }
            sched = _get(url, params)

            game_pk   = None
            team_side = None
            for date_entry in sched.get("dates", []):
                for g in date_entry.get("games", []):
                    if g.get("status", {}).get("abstractGameState") == "Final":
                        game_pk = g.get("gamePk")
                        home_id = (
                            g.get("teams", {}).get("home", {})
                             .get("team", {}).get("id")
                        )
                        team_side = "home" if home_id == team_id else "away"
                        break
                if game_pk:
                    break

            if not game_pk:
                continue

            # Fetch boxscore and extract batting order
            bs_url  = f"{config.MLB_API_BASE_URL}/game/{game_pk}/boxscore"
            boxscore = _get(bs_url)
            if not boxscore:
                continue

            team_data     = boxscore.get("teams", {}).get(team_side, {})
            batting_order = team_data.get("battingOrder", [])
            players_dict  = team_data.get("players", {})

            if not batting_order:
                continue

            lineup: list[dict] = []
            for pid in batting_order:
                pdata  = players_dict.get(f"ID{pid}", {})
                person = pdata.get("person", {})
                bat_side = person.get("batSide", {})
                hand = (
                    bat_side.get("code", "R")
                    if isinstance(bat_side, dict)
                    else "R"
                )
                lineup.append({
                    "id":       int(pid),
                    "fullName": person.get("fullName", ""),
                    "hand":     hand,
                    "position": pdata.get("position", {}).get("abbreviation", ""),
                })

            if lineup:
                result = {"lineup": lineup, "date": check_date}
                if _cache:
                    _cache.set(cache_key, result)
                return lineup, check_date

    except Exception as exc:
        logger.warning("get_recent_boxscore_lineup failed (team=%s): %s", team_id, exc)

    return [], ""


def get_player_career_stats(player_id: int) -> list[dict]:
    """Fetch year-by-year hitting stats for a player from the MLB Stats API.

    Returns one entry per season the player appeared in the majors.
    Cached for 6 hours — career stats don't change intra-day.

    Args:
        player_id: MLBAM player ID.

    Returns:
        List of season dicts sorted ascending by season year.
    """
    cache_key = f"mlb_career_{player_id}"
    if _cache:
        cached = _cache.get(cache_key, ttl_hours=6)
        if cached is not None:
            return cached

    url = f"{config.MLB_API_BASE_URL}/people/{player_id}/stats"
    params = {"stats": "yearByYear", "group": "hitting", "sportId": 1}
    data = _get(url, params)

    seasons: list[dict] = []
    for stat_group in data.get("stats", []):
        for split in stat_group.get("splits", []):
            season_year = int(split.get("season", 0) or 0)
            if season_year < 2000:
                continue
            team = split.get("team", {})
            stat = split.get("stat", {})

            def _f(key: str) -> float:
                try:
                    v = stat.get(key, 0)
                    return float(v) if v not in (None, "", "-.--", ".---") else 0.0
                except (ValueError, TypeError):
                    return 0.0

            def _i(key: str) -> int:
                try:
                    return int(stat.get(key, 0) or 0)
                except (ValueError, TypeError):
                    return 0

            seasons.append({
                "season": season_year,
                "team":   team.get("abbreviation", ""),
                "games":  _i("gamesPlayed"),
                "pa":     _i("plateAppearances"),
                "ab":     _i("atBats"),
                "hits":   _i("hits"),
                "doubles":_i("doubles"),
                "triples":_i("triples"),
                "hr":     _i("homeRuns"),
                "rbi":    _i("rbi"),
                "bb":     _i("baseOnBalls"),
                "so":     _i("strikeOuts"),
                "sb":     _i("stolenBases"),
                "avg":    _f("avg"),
                "obp":    _f("obp"),
                "slg":    _f("slg"),
                "ops":    _f("ops"),
            })

    seasons.sort(key=lambda s: s["season"])

    if _cache and seasons:
        _cache.set(cache_key, seasons)
    return seasons


def search_players(query: str) -> list[dict]:
    """Search for players by name using the MLB Stats API.

    Args:
        query: Partial or full player name.

    Returns:
        List of dicts with player_id, name, team, position, active.
    """
    if not query or len(query) < 2:
        return []

    url = f"{config.MLB_API_BASE_URL}/people/search"
    params = {"names": query, "sportId": 1}
    data = _get(url, params)

    results: list[dict] = []
    for person in data.get("people", []):
        pid = person.get("id", 0)
        if not pid:
            continue
        # Current team abbreviation (may be absent for retired players)
        team = ""
        curr = person.get("currentTeam", {})
        if isinstance(curr, dict):
            team = curr.get("abbreviation", "") or curr.get("name", "")
        results.append({
            "player_id": pid,
            "name":      person.get("fullName", ""),
            "team":      team,
            "position":  person.get("primaryPosition", {}).get("abbreviation", ""),
            "active":    person.get("active", False),
        })
    # Active players first, then alphabetical
    results.sort(key=lambda p: (not p["active"], p["name"]))
    return results[:15]


def get_season_leaders(stat_category: str, season: int, limit: int = 25) -> list[dict]:
    """Fetch statistical leaders for a given season from the MLB Stats API.

    Args:
        stat_category: MLB API leader category (e.g. 'homeRuns', 'battingAverage').
        season: Season year.
        limit: Number of leaders to return.

    Returns:
        List of dicts with rank, player_id, name, team, value.
    """
    cache_key = f"mlb_leaders_{stat_category}_{season}_{limit}"
    if _cache:
        cached = _cache.get(cache_key, ttl_hours=6)
        if cached is not None:
            return cached

    url = f"{config.MLB_API_BASE_URL}/stats/leaders"
    params = {
        "leaderCategories": stat_category,
        "season":           season,
        "sportId":          1,
        "limit":            limit,
        "hydrate":          "person,team",
    }
    data = _get(url, params)

    results: list[dict] = []
    for category in data.get("leagueLeaders", []):
        for entry in category.get("leaders", []):
            person = entry.get("person", {})
            team   = entry.get("team", {})
            pid    = person.get("id", 0)
            results.append({
                "rank":      entry.get("rank", len(results) + 1),
                "player_id": pid,
                "name":      person.get("fullName", ""),
                "team":      team.get("abbreviation", ""),
                "value":     entry.get("value", ""),
            })

    if _cache and results:
        _cache.set(cache_key, results)
    return results


def get_standings(season: int) -> dict:
    """Fetch league standings for a season.

    Args:
        season: Season year.

    Returns:
        Raw standings dict.
    """
    cache_key = f"mlb_standings_{season}"
    if _cache:
        cached = _cache.get(cache_key)
        if cached is not None:
            return cached

    url = f"{config.MLB_API_BASE_URL}/standings"
    params = {"leagueId": "103,104", "season": season}
    data = _get(url, params)
    if _cache and data:
        _cache.set(cache_key, data)
    return data


def parse_live_pitches(live_feed: dict) -> list[dict]:
    """Extract structured pitch list from live feed response.

    Args:
        live_feed: Raw live feed dict from get_live_feed.

    Returns:
        List of pitch dicts sorted by pitch_number.
    """
    pitches: list[dict] = []
    try:
        plays = (
            live_feed.get("liveData", {})
            .get("plays", {})
            .get("allPlays", [])
        )
        pitch_num = 0
        for play in plays:
            events = play.get("playEvents", [])
            for event in events:
                if event.get("isPitch", False):
                    pitch_num += 1
                    details = event.get("pitchData", {})
                    count = event.get("count", {})
                    pitches.append({
                        "pitch_number": pitch_num,
                        "pitch_type":   event.get("details", {}).get("type", {}).get("description", ""),
                        "speed":        details.get("startSpeed", 0.0),
                        "zone":         details.get("zone", 0),
                        "description":  event.get("details", {}).get("description", ""),
                        "balls":        count.get("balls", 0),
                        "strikes":      count.get("strikes", 0),
                        "outs":         count.get("outs", 0),
                        "result":       play.get("result", {}).get("type", ""),
                        "event":        play.get("result", {}).get("event", ""),
                    })
    except Exception as exc:
        logger.warning("parse_live_pitches failed: %s", exc)
    return sorted(pitches, key=lambda p: p["pitch_number"])
