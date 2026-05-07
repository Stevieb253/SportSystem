# data/results_fetcher.py
# Fetches actual game outcomes and writes them back to the predictions SQLite DB.
#
# USAGE (run manually after games finish, e.g. the morning after):
#
#   python data/results_fetcher.py                    # yesterday's games
#   python data/results_fetcher.py --date 2026-05-01  # specific date
#   python data/results_fetcher.py --date 2026-05-01 --dry-run
#
# DESIGN:
#   - Reads pending rows from predictions table (actual_result IS NULL)
#   - Fetches completed boxscores from MLB Stats API (no key needed)
#   - Matches by player_id — no fuzzy name matching needed
#   - For 'hit':  actual_result = 1 if hits >= 1, else 0
#   - For 'hr':   actual_result = 1 if homeRuns >= 1, else 0
#   - Skips games still in progress or not yet started
#   - Logs every unmatched player so you can diagnose gaps
#   - Dry-run mode prints what would be written without touching the DB
#
# PROP LINES ASSUMED:
#   Both "hit" and "hr" props use a 0.5 line (anytime hit / anytime HR).
#   This matches every major sportsbook for these markets.

import argparse
import logging
import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import config
from data.logger import PredictionLogger

logger = logging.getLogger("results_fetcher")


# Prop type → (stat key in boxscore, line, description)
_PROP_RULES: dict[str, tuple[str, float, str]] = {
    "hit": ("hits",      0.5, "anytime hit"),
    "hr":  ("homeRuns",  0.5, "anytime HR"),
}


class ResultsFetcher:
    """Fetches completed-game stats and updates prediction rows in the DB."""

    def __init__(self, prediction_logger: PredictionLogger, dry_run: bool = False) -> None:
        self.pred_logger = prediction_logger
        self.dry_run     = dry_run
        self._mlb_api    = _load_mlb_api()

    def run(self, target_date: str) -> dict:
        """Fetch and record results for all pending predictions on target_date.

        Returns a summary dict with counts for logging / CI reporting.
        """
        summary = {
            "date":      target_date,
            "pending":   0,
            "updated":   0,
            "skipped_in_progress": 0,
            "skipped_no_stat":     0,
            "unmatched": 0,
            "errors":    [],
        }

        pending = self.pred_logger.get_pending_results(target_date)
        summary["pending"] = len(pending)

        if not pending:
            logger.info("No pending predictions for %s", target_date)
            return summary

        logger.info(
            "%d pending predictions for %s — fetching boxscores…",
            len(pending), target_date,
        )

        # Build player_id → stats map from all completed boxscores for the date
        player_stats, skipped_games = self._fetch_all_player_stats(target_date)
        summary["skipped_in_progress"] = skipped_games

        if not player_stats:
            logger.warning(
                "No completed game stats found for %s (games still in progress?)",
                target_date,
            )
            return summary

        logger.info(
            "Collected stats for %d players across completed games", len(player_stats)
        )

        # Match each pending row to boxscore stats
        for row in pending:
            player_id = row["player_id"]
            prop_type = row["prop_type"]
            player_name = row["player_name"]

            stat_rule = _PROP_RULES.get(prop_type)
            if not stat_rule:
                logger.debug("No rule for prop_type '%s' — skipping %s", prop_type, player_name)
                continue

            stat_key, line, description = stat_rule

            if player_id not in player_stats:
                logger.info(
                    "UNMATCHED  %-25s  (id=%d, %s) — not found in any boxscore",
                    player_name, player_id, prop_type,
                )
                summary["unmatched"] += 1
                continue

            raw_stats   = player_stats[player_id]
            stat_value  = float(raw_stats.get(stat_key, 0) or 0)
            did_not_play = raw_stats.get("_did_not_play", False)

            if did_not_play:
                logger.info(
                    "NO STAT    %-25s  (id=%d, %s) — did not play / no AB",
                    player_name, player_id, prop_type,
                )
                summary["skipped_no_stat"] += 1
                continue

            actual_result = 1 if stat_value >= (line + 0.5) else 0

            if self.dry_run:
                verdict = "OVER" if actual_result else "UNDER"
                logger.info(
                    "[DRY-RUN]  %-25s  %s  %s=%.0f -> %s  (model=%.2f, %s)",
                    player_name, prop_type, stat_key, stat_value,
                    verdict, row["model_probability"], row["verdict"],
                )
            else:
                self.pred_logger.update_result(
                    date_str=target_date,
                    player_id=player_id,
                    prop_type=prop_type,
                    actual_result=actual_result,
                    actual_stat_value=stat_value,
                )

            summary["updated"] += 1

        _print_summary(summary, self.dry_run)
        return summary

    # ── Internal ─────────────────────────────────────────────────────────────

    def _fetch_all_player_stats(
        self, date_str: str
    ) -> tuple[dict[int, dict], int]:
        """Fetch boxscores for all completed games on date_str.

        Returns:
            (player_stats_map, skipped_game_count)
            player_stats_map: {player_id: {stat_key: value, ...}}
        """
        games = self._mlb_api.get_schedule(date_str)
        if not games:
            return {}, 0

        all_stats: dict[int, dict] = {}
        skipped = 0

        for game in games:
            game_pk = game.get("gamePk")
            if not game_pk:
                continue

            status = game.get("status", {})
            abstract_state = status.get("abstractGameState", "")
            coded_state    = status.get("codedGameState", "")

            # Only process Final games — skip Live, Preview, Postponed
            if abstract_state != "Final":
                if abstract_state in ("Live", "Preview"):
                    logger.debug(
                        "Game %d still %s — skipping", game_pk, abstract_state
                    )
                    skipped += 1
                continue

            # Double-header game 1 vs 2 (codedGameState 'O' = official)
            if coded_state not in ("F", "O", "C"):  # F=Final, O=Official, C=Complete
                logger.debug(
                    "Game %d coded state '%s' — skipping", game_pk, coded_state
                )
                skipped += 1
                continue

            try:
                stats = _parse_boxscore_stats(
                    self._mlb_api.get_boxscore(game_pk)
                )
                all_stats.update(stats)
            except Exception as exc:
                logger.warning("Failed to parse boxscore for game %d: %s", game_pk, exc)

        return all_stats, skipped


# ── Helpers ────────────────────────────────────────────────────────────────────

def _load_mlb_api():
    """Import mlb_api module without a cache (standalone script context)."""
    from api import mlb_api
    return mlb_api


def _parse_boxscore_stats(boxscore: dict) -> dict[int, dict]:
    """Extract per-player batting stats from a raw boxscore dict.

    Returns: {player_id: {hits, homeRuns, rbi, strikeOuts, atBats, ...}}
    Players with 0 at-bats (pitchers used as PH, etc.) are marked _did_not_play.
    """
    result: dict[int, dict] = {}
    if not boxscore:
        return result

    for side in ("home", "away"):
        team_data = boxscore.get("teams", {}).get(side, {})
        players   = team_data.get("players", {})

        for key, pdata in players.items():
            if not key.startswith("ID"):
                continue
            try:
                pid = int(key[2:])
            except ValueError:
                continue

            batting = pdata.get("stats", {}).get("batting", {})
            if not batting:
                continue

            ab = int(batting.get("atBats", 0) or 0)
            result[pid] = {
                "hits":        int(batting.get("hits", 0) or 0),
                "homeRuns":    int(batting.get("homeRuns", 0) or 0),
                "rbi":         int(batting.get("rbi", 0) or 0),
                "strikeOuts":  int(batting.get("strikeOuts", 0) or 0),
                "atBats":      ab,
                # Mark as did-not-play if zero plate appearances
                # (bench players who were never used)
                "_did_not_play": ab == 0 and int(batting.get("plateAppearances", 0) or 0) == 0,
            }

    return result


def _print_summary(summary: dict, dry_run: bool) -> None:
    prefix = "[DRY-RUN] " if dry_run else ""
    logger.info(
        "%sResults for %s: %d pending -> %d updated, %d unmatched, "
        "%d in-progress games, %d no-stat",
        prefix,
        summary["date"],
        summary["pending"],
        summary["updated"],
        summary["unmatched"],
        summary["skipped_in_progress"],
        summary["skipped_no_stat"],
    )


# ── CLI ────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fetch MLB game results and update prediction rows in SQLite."
    )
    parser.add_argument(
        "--date",
        default=(date.today() - timedelta(days=1)).isoformat(),
        help="Date to fetch results for (YYYY-MM-DD). Defaults to yesterday.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be written without modifying the database.",
    )
    args = parser.parse_args()

    pred_logger = PredictionLogger(config.PREDICTIONS_DB_PATH)
    fetcher     = ResultsFetcher(pred_logger, dry_run=args.dry_run)
    fetcher.run(args.date)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    main()
