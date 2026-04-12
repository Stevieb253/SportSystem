# services/live_tracker.py
# Background polling for live game updates.

import logging
import threading
import time
from typing import Any

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import config

logger = logging.getLogger(__name__)


class LiveTracker:
    """Polls MLB live feed in a background thread for real-time scores.

    Stores the latest feed per game_pk in self.active_games.
    Safe to read from the main thread at any time.
    """

    def __init__(self, poll_interval: int = config.LIVE_POLL_SECONDS) -> None:
        """Initialise tracker.

        Args:
            poll_interval: Seconds between poll cycles.
        """
        self.poll_interval = poll_interval
        self.active_games: dict[int, dict] = {}
        self.stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self, game_pks: list[int]) -> None:
        """Start background polling for the given games.

        Args:
            game_pks: List of MLB game primary keys to track.
        """
        self.stop_event.clear()
        self._thread = threading.Thread(
            target=self._poll_loop,
            args=(game_pks,),
            daemon=True,
            name="live-tracker",
        )
        self._thread.start()
        logger.info("LiveTracker started for games: %s", game_pks)

    def stop(self) -> None:
        """Stop the polling thread."""
        self.stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
        logger.info("LiveTracker stopped")

    def get_scores(self) -> list[dict]:
        """Return current score and status for all tracked games.

        Returns:
            List of dicts with game_pk, home_score, away_score,
            status, inning, inning_half.
        """
        results = []
        for game_pk, feed in self.active_games.items():
            results.append(_extract_score(game_pk, feed))
        return results

    def get_current_at_bat(self, game_pk: int) -> dict:
        """Return the current at-bat situation for a game.

        Args:
            game_pk: MLB game primary key.

        Returns:
            Dict with batter, pitcher, balls, strikes, outs, runners.
        """
        feed = self.active_games.get(game_pk, {})
        return _extract_at_bat(feed)

    def get_pitch_log(self, game_pk: int) -> list[dict]:
        """Return the full pitch log for a game.

        Args:
            game_pk: MLB game primary key.

        Returns:
            List of pitch dicts: type, speed, zone, result, count.
        """
        from api import mlb_api
        feed = self.active_games.get(game_pk, {})
        return mlb_api.parse_live_pitches(feed)

    def get_inning_scores(self, game_pk: int) -> list[dict]:
        """Return run totals per inning for both teams.

        Args:
            game_pk: MLB game primary key.

        Returns:
            List of dicts with inning, home_runs, away_runs.
        """
        feed = self.active_games.get(game_pk, {})
        try:
            innings = (
                feed.get("liveData", {})
                .get("linescore", {})
                .get("innings", [])
            )
            return [
                {
                    "inning":    inn.get("num", idx + 1),
                    "home_runs": inn.get("home", {}).get("runs", 0),
                    "away_runs": inn.get("away", {}).get("runs", 0),
                }
                for idx, inn in enumerate(innings)
            ]
        except Exception as exc:
            logger.warning("get_inning_scores failed (game_pk=%s): %s", game_pk, exc)
            return []

    # ── Internal ──────────────────────────────────────────────────────────────

    def _poll_loop(self, game_pks: list[int]) -> None:
        """Background thread: polls each game repeatedly until stopped."""
        from api import mlb_api
        while not self.stop_event.is_set():
            for game_pk in game_pks:
                try:
                    feed = mlb_api.get_live_feed(game_pk)
                    if feed:
                        self.active_games[game_pk] = feed
                except Exception as exc:
                    logger.warning("Live feed poll failed (game_pk=%s): %s", game_pk, exc)
            self.stop_event.wait(timeout=self.poll_interval)


# ── Module-level helpers ──────────────────────────────────────────────────────

def _extract_score(game_pk: int, feed: dict) -> dict:
    """Pull score details from a live feed dict."""
    try:
        ls = feed.get("liveData", {}).get("linescore", {})
        state = (
            feed.get("gameData", {})
            .get("status", {})
            .get("abstractGameState", "Unknown")
        )
        return {
            "game_pk":     game_pk,
            "home_score":  ls.get("teams", {}).get("home", {}).get("runs", 0),
            "away_score":  ls.get("teams", {}).get("away", {}).get("runs", 0),
            "status":      state,
            "inning":      ls.get("currentInning", 0),
            "inning_half": ls.get("inningHalf", ""),
        }
    except Exception:
        return {"game_pk": game_pk, "home_score": 0, "away_score": 0,
                "status": "Unknown", "inning": 0, "inning_half": ""}


def _extract_at_bat(feed: dict) -> dict:
    """Pull current at-bat details from a live feed dict."""
    try:
        live = feed.get("liveData", {})
        plays = live.get("plays", {})
        current = plays.get("currentPlay", {})
        matchup = current.get("matchup", {})
        count = current.get("count", {})
        runners = live.get("linescore", {}).get("offense", {})
        return {
            "batter":  matchup.get("batter", {}).get("fullName", ""),
            "pitcher": matchup.get("pitcher", {}).get("fullName", ""),
            "balls":   count.get("balls", 0),
            "strikes": count.get("strikes", 0),
            "outs":    count.get("outs", 0),
            "runners": {
                "first":  bool(runners.get("first")),
                "second": bool(runners.get("second")),
                "third":  bool(runners.get("third")),
            },
        }
    except Exception:
        return {"batter": "", "pitcher": "", "balls": 0,
                "strikes": 0, "outs": 0, "runners": {}}
