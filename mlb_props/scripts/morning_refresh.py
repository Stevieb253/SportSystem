# scripts/morning_refresh.py
# Morning pre-game automation: build models, log predictions, fetch odds, persist to SQLite.
#
# SCHEDULE: Run via Windows Task Scheduler at 9:00 AM ET every day.
#
# USAGE:
#   python scripts/morning_refresh.py                         # today only, odds ON
#   python scripts/morning_refresh.py --tomorrow              # today + tomorrow
#   python scripts/morning_refresh.py --no-odds               # skip odds (saves API quota)
#   python scripts/morning_refresh.py --date 2026-05-04       # one specific date
#   python scripts/morning_refresh.py --timeout 900           # override build timeout (seconds)
#
# IDEMPOTENT: safe to run multiple times per day.
#   - Predictions:  INSERT OR IGNORE -- duplicate dates never create duplicate rows
#   - Odds:         UPDATE SET -- reruns overwrite with the latest line (correct for line moves)
#
# TIMEOUT BEHAVIOUR:
#   Model build runs in a daemon thread. If it does not complete within --timeout seconds
#   (default 600 = 10 min), the script logs the stall and continues to the next date.
#   The background thread is abandoned -- it will terminate naturally when the stalled
#   socket closes or the OS reclaims the process after the script exits.
#
# OUTPUTS:
#   logs/morning_refresh.log   rotating log file (5 MB, 7 backups)
#   db/predictions.sqlite      updated with new predictions + odds

import argparse
import logging
import sys
import threading
from datetime import date, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path

# ── Path setup ─────────────────────────────────────────────────────────────────
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import config
from data.cache import Cache
from data.logger import PredictionLogger
from data.pipeline import DataPipeline
from services import hit_probability, hr_probability, odds_service
from services.model_builder import ModelBuilder
from api import odds_api as _odds_api


# ── Logging setup ──────────────────────────────────────────────────────────────

_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def _setup_logging() -> None:
    logs_dir = ROOT / "logs"
    logs_dir.mkdir(exist_ok=True)

    formatter = logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT)
    root = logging.getLogger()
    root.setLevel(logging.INFO)

    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(formatter)
    root.addHandler(ch)

    fh = RotatingFileHandler(
        logs_dir / "morning_refresh.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=7,
        encoding="utf-8",
    )
    fh.setFormatter(formatter)
    root.addHandler(fh)


log = logging.getLogger("morning_refresh")


# ── Timeout-protected model build ──────────────────────────────────────────────

def _build_model_with_timeout(
    builder: ModelBuilder,
    date_str: str,
    timeout_secs: int,
    batch_timeout_seconds: int = 600,
) -> tuple:
    """Run get_model_for_date() in a daemon thread with a heartbeat and timeout.

    Logs '  still building...' every 60 seconds so the log file shows the script
    is alive and not silently frozen.

    Args:
        timeout_secs: Outer process timeout (script-level safety net).
        batch_timeout_seconds: Inner game-batch timeout forwarded to model_builder.

    Returns:
        (model_dict, None)   on success
        (None, error_str)    on timeout or exception
    """
    _result: list = [None]
    _error:  list = [None]

    def _run() -> None:
        try:
            _result[0] = builder.get_model_for_date(
                date_str, batch_timeout_seconds=batch_timeout_seconds
            )
        except Exception as exc:
            _error[0] = str(exc)

    thread = threading.Thread(
        target=_run,
        name=f"model-build-{date_str}",
        daemon=True,   # abandoned threads won't block process exit
    )
    thread.start()

    heartbeat = 60   # log a status line every N seconds while waiting
    elapsed   = 0

    while elapsed < timeout_secs:
        thread.join(timeout=heartbeat)
        if not thread.is_alive():
            break
        elapsed += heartbeat
        remaining = timeout_secs - elapsed
        log.info(
            "  [%s] Model still building... (%d min elapsed, %d min left before timeout)",
            date_str, elapsed // 60, remaining // 60,
        )

    if thread.is_alive():
        return None, f"timed out after {timeout_secs}s — a lineup or API call is stalled"

    if _error[0]:
        return None, _error[0]

    return _result[0], None


# ── Odds persistence (standalone — no Flask dependency) ────────────────────────

def _persist_odds(
    date_str: str,
    model: dict,
    odds_by_player: dict,
    pred_logger: PredictionLogger,
) -> dict:
    """Write sportsbook odds + edge to prediction rows for date_str.

    Mirrors the logic in routes._persist_odds_to_db() but without Flask.
    UPDATE is idempotent -- reruns overwrite with the latest line.

    Returns count dict: {hit_written, hit_skipped, hr_written, hr_skipped}
    """
    hit_written = hit_skipped = hr_written = hr_skipped = 0

    for r in model.get("hit_probabilities", []):
        if not isinstance(r, dict):
            continue
        name      = (r.get("player") or {}).get("name", "")
        odds_info = odds_by_player.get(name)
        if not odds_info:
            hit_skipped += 1
            continue
        prob      = float(r.get("hit_probability", 0))
        edge_data = odds_service.enrich_with_edge(prob, odds_info)
        if edge_data["implied_probability"] is None:
            hit_skipped += 1
            continue
        pred_logger.update_odds(
            date_str            = date_str,
            player_name         = name,
            prop_type           = "hit",
            sportsbook_odds     = edge_data["sportsbook_odds"] or "",
            sportsbook_line     = edge_data["sportsbook_line"] or 0.5,
            implied_probability = edge_data["implied_probability"],
            best_book           = edge_data["best_book"] or "",
            edge                = edge_data["edge"],
        )
        hit_written += 1

    for r in model.get("hr_probabilities", []):
        if not isinstance(r, dict):
            continue
        name      = (r.get("player") or {}).get("name", "")
        odds_info = odds_by_player.get(name + "_hr") or odds_by_player.get(name)
        if not odds_info:
            hr_skipped += 1
            continue
        prob      = float(r.get("hr_probability", 0))
        edge_data = odds_service.enrich_with_edge(prob, odds_info)
        if edge_data["implied_probability"] is None:
            hr_skipped += 1
            continue
        pred_logger.update_odds(
            date_str            = date_str,
            player_name         = name,
            prop_type           = "hr",
            sportsbook_odds     = edge_data["sportsbook_odds"] or "",
            sportsbook_line     = edge_data["sportsbook_line"] or 0.5,
            implied_probability = edge_data["implied_probability"],
            best_book           = edge_data["best_book"] or "",
            edge                = edge_data["edge"],
        )
        hr_written += 1

    return {
        "hit_written": hit_written,
        "hit_skipped": hit_skipped,
        "hr_written":  hr_written,
        "hr_skipped":  hr_skipped,
    }


# ── Per-date refresh ───────────────────────────────────────────────────────────

def refresh_date(
    date_str: str,
    builder: ModelBuilder,
    pred_logger: PredictionLogger,
    fetch_odds: bool,
    timeout_secs: int,
    batch_timeout_seconds: int = 600,
) -> dict:
    """Build model, log predictions, fetch odds, persist to DB for one date.

    Never raises -- all errors are logged and collected in the result dict
    so the caller can always continue to the next date.
    """
    result = {
        "date":                date_str,
        "games_in_schedule":   0,
        "games_with_players":  0,
        "players_projected":   0,
        "predictions_inserted": 0,
        "predictions_in_db":   0,
        "odds_hit_written":    0,
        "odds_hr_written":     0,
        "odds_hit_skipped":    0,
        "odds_hr_skipped":     0,
        "errors":              [],
    }

    log.info("--- Processing %s ---", date_str)
    log.info("  Odds: %s", "ENABLED" if fetch_odds else "DISABLED (--no-odds)")

    # ── Step 1: Force-invalidate cache so morning build is always from live data
    # A previous web-app request or scheduler run may have cached a partial model
    # (e.g. built before all lineups were posted, or built by old timeout-prone code).
    builder.invalidate_date(date_str)
    log.info("  Cache invalidated — forcing fresh build from live data")

    # ── Step 2: Build model with timeout ──────────────────────────────────────
    log.info(
        "  Building model (game batch=%ds, process limit=%ds)...",
        batch_timeout_seconds, timeout_secs,
    )
    model, build_err = _build_model_with_timeout(
        builder, date_str, timeout_secs, batch_timeout_seconds=batch_timeout_seconds
    )

    if build_err:
        log.error("  Model build FAILED for %s: %s", date_str, build_err)
        log.error("  Skipping predictions + odds for %s.", date_str)
        result["errors"].append(f"model_build: {build_err}")
        return result

    n_hit    = len(model.get("hit_probabilities", []))
    n_hr     = len(model.get("hr_probabilities", []))
    n_games  = len(model.get("games", []))

    # Count games that actually produced player projections (detects partial builds)
    game_pks_with_players: set = set()
    for r in model.get("hit_probabilities", []):
        if isinstance(r, dict):
            gk = (r.get("game") or {}).get("game_pk")
            if gk:
                game_pks_with_players.add(gk)

    result["games_in_schedule"]  = n_games
    result["games_with_players"] = len(game_pks_with_players)
    result["players_projected"]  = n_hit

    log.info(
        "  Model: %d games in schedule | %d games with players | %d batters projected",
        n_games, len(game_pks_with_players), n_hit,
    )

    # Warn when the build appears incomplete
    if n_games > 0 and len(game_pks_with_players) < n_games:
        missing = n_games - len(game_pks_with_players)
        log.warning(
            "  INCOMPLETE BUILD: %d of %d games have no player projections "
            "(lineup unavailable or game future timed out)",
            missing, n_games,
        )

    if n_hit == 0:
        if n_games > 0:
            log.critical(
                "  BUILD FAILED: %d games in schedule but 0 player projections. "
                "All game futures likely timed out. "
                "Run again after the circuit breaker clears, or check API latency.",
                n_games,
            )
            result["errors"].append("build_failed_0_projections")
        else:
            log.info("  No players found for %s — no games scheduled (off-day)", date_str)
        return result

    # ── Step 3: Log predictions to SQLite ─────────────────────────────────────
    # Always call log_from_model here — INSERT OR IGNORE makes it fully idempotent.
    # get_model_for_date() only logs during a fresh build; if the model was returned
    # from cache (e.g. built by the web app), predictions would never reach SQLite.
    try:
        pre_count  = pred_logger.row_count(date_str)
        inserted   = pred_logger.log_from_model(model, date_str)
        post_count = pred_logger.row_count(date_str)
        result["predictions_inserted"] = inserted
        result["predictions_in_db"]    = post_count
        log.info(
            "  Predictions: %d in DB total | %d new this run | %d already existed",
            post_count, inserted, pre_count,
        )
    except Exception as exc:
        log.error("  Prediction logging failed for %s: %s", date_str, exc)
        result["errors"].append(f"prediction_log: {exc}")
        # Don't continue to odds — rows must exist before UPDATE can attach odds
        return result

    if result["predictions_in_db"] == 0:
        log.error(
            "  CRITICAL: 0 prediction rows in DB after logging attempt — "
            "skipping odds step (nothing to UPDATE)."
        )
        result["errors"].append("no_prediction_rows")
        return result

    # ── Step 4: Odds enabled/disabled check ───────────────────────────────────
    if not fetch_odds:
        log.info("  Skipping odds step (--no-odds).")
        return result

    if not config.ODDS_API_KEY:
        log.warning("  ODDS_API_KEY not set in .env — cannot fetch odds")
        result["errors"].append("no_api_key")
        return result

    # ── Step 5: Collect player names and fetch odds ───────────────────────────
    try:
        player_names: list[str] = []
        seen: set[str] = set()
        for r in model.get("hit_probabilities", []):
            name = (r.get("player") or {}).get("name", "")
            if name and name not in seen:
                player_names.append(name)
                seen.add(name)
        for r in model.get("hr_probabilities", []):
            name = (r.get("player") or {}).get("name", "")
            if name and name not in seen:
                player_names.append(name)
                seen.add(name)

        log.info("  Fetching odds for %d unique players...", len(player_names))
        odds_by_player = _odds_api.fetch_all_props_for_today(player_names)
        quota          = _odds_api.get_quota_info()
        log.info(
            "  Odds API: %d/%d players matched | quota remaining=%s",
            len(odds_by_player), len(player_names), quota.get("remaining", "unknown"),
        )

        if not odds_by_player:
            log.warning(
                "  No odds returned for %s — games may be live, already final, "
                "or books have not yet posted lines for this date",
                date_str,
            )
            # Not an error — expected for tomorrow and for off-days
            return result

    except Exception as exc:
        log.error("  Odds fetch failed for %s: %s", date_str, exc)
        result["errors"].append(f"odds_fetch: {exc}")
        return result

    # ── Step 6: Persist odds to SQLite ────────────────────────────────────────
    try:
        counts = _persist_odds(date_str, model, odds_by_player, pred_logger)
        result["odds_hit_written"] = counts["hit_written"]
        result["odds_hr_written"]  = counts["hr_written"]
        result["odds_hit_skipped"] = counts["hit_skipped"]
        result["odds_hr_skipped"]  = counts["hr_skipped"]
        log.info(
            "  Odds: hit=%d written / %d no line | hr=%d written / %d no line",
            counts["hit_written"], counts["hit_skipped"],
            counts["hr_written"],  counts["hr_skipped"],
        )
        if counts["hit_written"] == 0 and len(odds_by_player) > 0:
            log.warning(
                "  WARNING: Odds API returned %d players but 0 hit rows were updated. "
                "Player name mismatch between model and sportsbook?",
                len(odds_by_player),
            )
    except Exception as exc:
        log.error("  Odds persist failed for %s: %s", date_str, exc)
        result["errors"].append(f"odds_persist: {exc}")

    return result


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Morning pre-game refresh: build models, log predictions, fetch and persist odds. "
            "Odds are ON by default. Safe to run multiple times per day."
        )
    )
    parser.add_argument(
        "--date",
        default=None,
        help="Process a specific date YYYY-MM-DD. Defaults to today only.",
    )
    parser.add_argument(
        "--tomorrow",
        action="store_true",
        help="Also process tomorrow's date (useful for pre-loading next-day odds when lines are posted).",
    )
    parser.add_argument(
        "--no-odds",
        action="store_true",
        help="Skip odds fetch (conserves Odds API monthly quota).",
    )
    parser.add_argument(
        "--fast",
        action="store_true",
        help=(
            "Fast/scheduled mode: 300s game-batch timeout. "
            "Completes quickly but may miss slow-loading games. "
            "Incomplete builds are clearly flagged. "
            "Default (no flag): 600s timeout for full-quality output."
        ),
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=None,
        help=(
            "Override the outer process timeout in seconds. "
            "Default: batch_timeout + 300s (auto-computed). "
            "Use this only if you need a custom ceiling."
        ),
    )
    args = parser.parse_args()

    _setup_logging()

    today    = date.today().isoformat()
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    if args.date:
        dates = [args.date]
    elif args.tomorrow:
        dates = [today, tomorrow]
    else:
        dates = [today]

    # Determine timeouts
    # --fast:    game-batch = 300s, outer = 600s   (scheduler-safe, may be incomplete)
    # default:   game-batch = 600s, outer = 900s   (full quality, waits for all games)
    # --timeout: always overrides outer ceiling
    batch_timeout = 300 if args.fast else 600
    outer_timeout = args.timeout if args.timeout is not None else batch_timeout + 300

    mode_label = "FAST (300s batch)" if args.fast else "FULL-QUALITY (600s batch)"

    log.info("=================================================")
    log.info("Morning refresh starting")
    log.info("  dates    : %s", dates)
    log.info("  mode     : %s", mode_label)
    log.info("  odds     : %s", "DISABLED (--no-odds)" if args.no_odds else "ENABLED")
    log.info(
        "  timeouts : game batch=%ds, process limit=%ds",
        batch_timeout, outer_timeout,
    )
    log.info("=================================================")

    # Build singletons once, reuse across all dates
    cache       = Cache(config.CACHE_DIR, config.CACHE_TTL_HOURS)
    pred_logger = PredictionLogger(config.PREDICTIONS_DB_PATH)
    pipeline    = DataPipeline(cache)
    _odds_api.set_cache(cache)
    builder     = ModelBuilder(pipeline, hit_probability, hr_probability, pred_logger)

    # Process each date independently -- one failure never blocks the next
    results = []
    for d in dates:
        r = refresh_date(
            date_str              = d,
            builder               = builder,
            pred_logger           = pred_logger,
            fetch_odds            = not args.no_odds,
            timeout_secs          = outer_timeout,
            batch_timeout_seconds = batch_timeout,
        )
        results.append(r)

    # Final summary
    log.info("")
    log.info("=== FINAL SUMMARY ===")
    log.info(
        "  %-12s  %5s  %5s  %7s  %7s  %8s  %8s  %s",
        "date", "sched", "built", "players", "in_db", "hit_odds", "hr_odds", "status",
    )
    log.info("  " + "-" * 72)
    any_hard_errors = False
    for r in results:
        # Only count real errors (not "no odds available") as hard failures
        hard = [e for e in r["errors"] if not e.startswith("no_odds")]
        if hard:
            any_hard_errors = True

        is_incomplete = (
            r["games_in_schedule"] > 0
            and r["games_with_players"] < r["games_in_schedule"]
        )
        if hard:
            status = f"ERRORS: {hard}"
        elif is_incomplete:
            status = (
                f"INCOMPLETE ({r['games_with_players']}/{r['games_in_schedule']} games built)"
            )
        else:
            status = "OK"
        log.info(
            "  %-12s  %5d  %5d  %7d  %7d  %8d  %8d  %s",
            r["date"],
            r["games_in_schedule"],
            r["games_with_players"],
            r["players_projected"],
            r["predictions_in_db"],
            r["odds_hit_written"],
            r["odds_hr_written"],
            status,
        )
        if r["predictions_inserted"] > 0:
            log.info("    -> %d new prediction rows inserted this run", r["predictions_inserted"])

    log.info("Morning refresh complete.")
    sys.exit(1 if any_hard_errors else 0)


if __name__ == "__main__":
    main()
