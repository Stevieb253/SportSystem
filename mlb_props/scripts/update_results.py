# scripts/update_results.py
# Nightly results collection: fetch game outcomes, update SQLite, export feature store.
#
# SCHEDULE: Run via Windows Task Scheduler at 6:00 AM ET every day
#           (after all west-coast games finish ~1-2 AM ET).
#
# USAGE:
#   python scripts/update_results.py                    # yesterday's games
#   python scripts/update_results.py --date 2026-05-01  # specific date
#   python scripts/update_results.py --dry-run          # preview without writing
#
# IDEMPOTENT: safe to run multiple times.
#   get_pending_results() returns only rows where actual_result IS NULL.
#   After the first successful run, pending rows are filled -- reruns find nothing
#   pending and exit cleanly without touching the DB.
#
# OUTPUTS:
#   db/predictions.sqlite         actual_result + actual_stat_value filled in
#   training/data/hits_DATE.csv   ML-ready hit feature export (labeled rows only)
#   training/data/hrs_DATE.csv    ML-ready HR feature export (labeled rows only)
#   logs/update_results.log       rotating log file (5 MB, 7 backups)

import argparse
import logging
import sqlite3
import sys
from datetime import date, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path

# ── Path setup ─────────────────────────────────────────────────────────────────
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import config
from data.logger import PredictionLogger
from data.results_fetcher import ResultsFetcher
from training.feature_store import FeatureStore


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
        logs_dir / "update_results.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=7,
        encoding="utf-8",
    )
    fh.setFormatter(formatter)
    root.addHandler(fh)


log = logging.getLogger("update_results")


# ── DB summary helper ──────────────────────────────────────────────────────────

def _print_db_summary() -> None:
    """Print labeled row counts per date/prop from the predictions DB."""
    try:
        conn = sqlite3.connect(config.PREDICTIONS_DB_PATH)
        rows = conn.execute("""
            SELECT date, prop_type,
                   COUNT(*)                              AS total,
                   SUM(actual_result IS NOT NULL)        AS labeled,
                   SUM(edge IS NOT NULL)                 AS has_odds,
                   ROUND(AVG(CASE WHEN actual_result IS NOT NULL
                                  THEN actual_result END), 3) AS over_rate
            FROM predictions
            GROUP BY date, prop_type
            ORDER BY date DESC
        """).fetchall()
        conn.close()

        if not rows:
            log.info("DB is empty -- no predictions logged yet")
            return

        log.info("")
        log.info("=== DB SUMMARY (all dates) ===")
        log.info("  %-12s  %-4s  %6s  %7s  %8s  %9s",
                 "date", "prop", "total", "labeled", "has_odds", "over_rate")
        log.info("  " + "-" * 56)
        for r in rows:
            over_rate = f"{r[5]:.3f}" if r[5] is not None else "  N/A"
            log.info("  %-12s  %-4s  %6d  %7d  %8d  %9s",
                     r[0], r[1], r[2], r[3] or 0, r[4] or 0, over_rate)

        # Training readiness check
        total_labeled_hits = sum(r[3] or 0 for r in rows if r[1] == "hit")
        n_dates = len({r[0] for r in rows if r[1] == "hit" and (r[3] or 0) > 0})
        log.info("")
        log.info("  Labeled hit rows: %d across %d date(s)", total_labeled_hits, n_dates)
        if total_labeled_hits < 200:
            log.info("  Training status: collecting data (%d / 200 minimum)", total_labeled_hits)
        elif total_labeled_hits < 500:
            log.info("  Training status: early signal -- run train_hits.py --cv-only to watch AUC")
        elif total_labeled_hits < 1000:
            log.info("  Training status: good signal -- consider saving model (python training/train_hits.py)")
        else:
            log.info("  Training status: sufficient data -- train and deploy (python training/train_hits.py)")

    except Exception as exc:
        log.warning("DB summary failed (non-fatal): %s", exc)


# ── Feature store export ───────────────────────────────────────────────────────

def _export_feature_store(target_date: str) -> None:
    """Export labeled rows to CSV. Non-fatal -- a failed export never stops the script."""
    export_dir = ROOT / "training" / "data"
    export_dir.mkdir(parents=True, exist_ok=True)

    try:
        fs = FeatureStore()
    except Exception as exc:
        log.warning("FeatureStore init failed (non-fatal): %s", exc)
        return

    for prop in ("hit", "hr"):
        try:
            df       = fs.build_dataset(prop)
            labeled  = int(df["actual_result"].notna().sum())
            total    = len(df)
            pct      = f"{100 * labeled / total:.0f}%" if total else "0%"

            # Name the file after the date being processed, not today
            out_path = export_dir / f"{prop}s_{target_date}.csv"
            df.to_csv(out_path, index=False)

            log.info(
                "  Feature store [%s]: %d total rows | %d labeled (%s) | saved to %s",
                prop, total, labeled, pct, out_path.name,
            )
        except Exception as exc:
            log.warning("  Feature store export failed for %s: %s", prop, exc)


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Fetch game results for a date, update SQLite, export feature store CSVs. "
            "Safe to run multiple times -- already-filled rows are skipped."
        )
    )
    parser.add_argument(
        "--date",
        default=(date.today() - timedelta(days=1)).isoformat(),
        help="Date to update results for (YYYY-MM-DD). Defaults to yesterday.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be written without modifying the database.",
    )
    args = parser.parse_args()

    _setup_logging()

    target_date = args.date
    log.info("=================================================")
    log.info("Results update starting | date=%s | dry_run=%s", target_date, args.dry_run)
    log.info("=================================================")

    pred_logger = PredictionLogger(config.PREDICTIONS_DB_PATH)

    # ── Step 1: Fetch and record results ──────────────────────────────────────
    log.info("Step 1: Fetching game results for %s", target_date)
    try:
        fetcher = ResultsFetcher(pred_logger, dry_run=args.dry_run)
        summary = fetcher.run(target_date)
    except Exception as exc:
        # Fatal -- don't export a stale feature store if results failed to load
        log.error("Results fetch failed: %s", exc)
        log.error("Aborting -- fix the error above before exporting feature store.")
        sys.exit(1)

    if summary["pending"] == 0:
        log.info("  No pending rows for %s -- already up to date or no predictions logged", target_date)
    else:
        log.info(
            "  Results: %d updated | %d unmatched | %d in-progress games | %d did-not-play",
            summary["updated"],
            summary["unmatched"],
            summary["skipped_in_progress"],
            summary["skipped_no_stat"],
        )
        if summary["errors"]:
            log.warning("  Errors during results fetch: %s", summary["errors"])

    # ── Step 2: Export feature store CSVs ─────────────────────────────────────
    if args.dry_run:
        log.info("Step 2: Skipped (dry-run mode -- no feature store export)")
    else:
        log.info("Step 2: Exporting feature store CSVs")
        _export_feature_store(target_date)

    # ── Step 3: Print DB summary with training readiness ──────────────────────
    log.info("Step 3: DB summary")
    _print_db_summary()

    log.info("")
    log.info("Results update complete.")


if __name__ == "__main__":
    main()
