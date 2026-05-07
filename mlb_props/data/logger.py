# data/logger.py
# Logs daily model predictions to SQLite for ML training and backtesting.
#
# DESIGN RULES:
#   - Every public method wraps its DB work in try/except — a logging failure
#     must NEVER crash the app or affect model output.
#   - INSERT OR IGNORE on (date, player_id, prop_type) — rebuilding the model
#     for the same day does not overwrite previously-logged rows.
#   - Odds columns are nullable — Phase 0 logs model output only.
#     Phase 1 will call update_odds() after the manual odds refresh.
#   - actual_result / actual_stat_value are NULL until the next-day results
#     fetcher (Phase 2) calls update_result().
#
# SCHEMA NOTE:
#   Raw feature values (xba, barrel_pct, etc.) are stored as individual columns
#   for easy SQL querying during training.  The full component_scores dict is
#   also stored as JSON for complete reproducibility.

import json
import logging
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS predictions (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,

    -- Identity
    date                  TEXT    NOT NULL,
    player_id             INTEGER NOT NULL,
    player_name           TEXT    NOT NULL,
    team                  TEXT,
    prop_type             TEXT    NOT NULL,   -- 'hit' | 'hr'
    lineup_status         TEXT,               -- 'official' | 'probable_recent' | 'probable_roster'
    lineup_position       INTEGER,

    -- Model output
    model_probability     REAL    NOT NULL,
    verdict               TEXT    NOT NULL,   -- 'YES' | 'LEAN' | 'NO'

    -- Sportsbook (nullable — filled by update_odds() in Phase 1)
    sportsbook_odds       TEXT,               -- '+350' or '-135'
    implied_probability   REAL,
    edge                  REAL,               -- model_prob - implied_prob
    best_book             TEXT,

    -- Batter features (raw stat values before normalization)
    xba                   REAL,
    xwoba                 REAL,
    barrel_pct            REAL,
    exit_velocity         REAL,
    ev50                  REAL,
    hard_hit_pct          REAL,
    sweet_spot_pct        REAL,
    whiff_pct             REAL,
    launch_angle_pct      REAL,
    hr_fb_ratio           REAL,

    -- Pitcher features
    pitcher_xera          REAL,
    pitcher_hr9           REAL,

    -- Context
    park_factor           REAL,
    platoon_adv           REAL,
    weather_adjustment    REAL,

    -- Full normalized component scores as JSON (exact model inputs)
    component_scores_json TEXT,

    -- Outcome (filled by update_result() in Phase 2)
    actual_result         INTEGER,            -- 1 = over, 0 = under, NULL = pending
    actual_stat_value     REAL,
    result_logged_at      TEXT,

    -- Metadata
    logged_at             TEXT    NOT NULL,

    UNIQUE(date, player_id, prop_type)
);
"""

_CREATE_INDEX_DATE       = "CREATE INDEX IF NOT EXISTS idx_pred_date ON predictions(date);"
_CREATE_INDEX_PLAYER     = "CREATE INDEX IF NOT EXISTS idx_pred_player ON predictions(player_id);"
_CREATE_INDEX_PENDING    = "CREATE INDEX IF NOT EXISTS idx_pred_pending ON predictions(actual_result) WHERE actual_result IS NULL;"


class PredictionLogger:
    """Writes and reads prediction rows in the SQLite training database."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._ready = False
        try:
            os.makedirs(os.path.dirname(db_path), exist_ok=True)
            self._init_schema()
            self._ready = True
            logger.info("PredictionLogger ready: %s", db_path)
        except Exception as exc:
            logger.warning("PredictionLogger init failed (non-fatal): %s", exc)

    # ── Public API ─────────────────────────────────────────────────────────────

    def log_from_model(self, serialised_model: dict, date_str: str) -> int:
        """Log all hit + HR predictions from a serialised model dict.

        Called once per model build.  Rows that already exist for this date
        (e.g. from an earlier rebuild) are silently skipped via INSERT OR IGNORE.

        Returns the number of new rows inserted.
        """
        if not self._ready:
            return 0
        try:
            hit_rows = self._extract_hit_rows(
                serialised_model.get("hit_probabilities", []), date_str
            )
            hr_rows = self._extract_hr_rows(
                serialised_model.get("hr_probabilities", []), date_str
            )
            inserted = self._bulk_insert(hit_rows + hr_rows)
            logger.info(
                "Logged %d new prediction rows for %s (%d hit, %d hr)",
                inserted, date_str, len(hit_rows), len(hr_rows),
            )
            return inserted
        except Exception as exc:
            logger.warning("log_from_model failed (non-fatal): %s", exc)
            return 0

    def update_odds(
        self,
        date_str: str,
        player_name: str,
        prop_type: str,               # 'hit' | 'hr'
        sportsbook_odds: str,         # American odds string e.g. '+350' or '-135'
        sportsbook_line: float,       # prop line (0.5 for anytime hit/HR)
        implied_probability: float,
        best_book: str,
        edge: float | None = None,    # pre-computed by odds_service.calculate_edge()
    ) -> None:
        """Attach sportsbook odds to an existing prediction row.

        Called in Phase 1 after the manual odds refresh.
        Edge should be pre-computed by odds_service.enrich_with_edge() and passed in
        so this layer stays free of math logic.
        """
        if not self._ready:
            return
        try:
            with self._connect() as conn:
                cursor = conn.execute(
                    """
                    UPDATE predictions
                       SET sportsbook_odds     = ?,
                           sportsbook_line     = ?,
                           implied_probability = ?,
                           edge                = ?,
                           best_book           = ?
                     WHERE date        = ?
                       AND player_name = ?
                       AND prop_type   = ?
                    """,
                    (sportsbook_odds, sportsbook_line, implied_probability, edge, best_book,
                     date_str, player_name, prop_type),
                )
                if cursor.rowcount == 0:
                    logger.debug(
                        "update_odds matched 0 rows: date=%s player=%r prop=%s",
                        date_str, player_name, prop_type,
                    )
        except Exception as exc:
            logger.warning("update_odds failed (non-fatal): %s", exc)

    def update_result(
        self,
        date_str: str,
        player_id: int,
        prop_type: str,
        actual_result: int,        # 1 = over/yes, 0 = under/no
        actual_stat_value: float,
    ) -> None:
        """Record the actual outcome for a prediction row.

        Called in Phase 2 by the nightly results fetcher.
        """
        if not self._ready:
            return
        try:
            now = datetime.now(timezone.utc).isoformat()
            with self._connect() as conn:
                conn.execute(
                    """
                    UPDATE predictions
                       SET actual_result    = ?,
                           actual_stat_value = ?,
                           result_logged_at = ?
                     WHERE date      = ?
                       AND player_id = ?
                       AND prop_type = ?
                    """,
                    (actual_result, actual_stat_value, now,
                     date_str, player_id, prop_type),
                )
        except Exception as exc:
            logger.warning("update_result failed (non-fatal): %s", exc)

    def get_pending_results(self, date_str: str) -> list[dict]:
        """Return rows for a date that have no actual result yet.

        Used by the Phase 2 results fetcher to know which players to look up.
        """
        if not self._ready:
            return []
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    """
                    SELECT player_id, player_name, prop_type, model_probability, verdict
                      FROM predictions
                     WHERE date          = ?
                       AND actual_result IS NULL
                    """,
                    (date_str,),
                ).fetchall()
            return [dict(r) for r in rows]
        except Exception as exc:
            logger.warning("get_pending_results failed (non-fatal): %s", exc)
            return []

    def row_count(self, date_str: str | None = None) -> int:
        """Return total logged rows, optionally filtered to a single date."""
        if not self._ready:
            return 0
        try:
            with self._connect() as conn:
                if date_str:
                    return conn.execute(
                        "SELECT COUNT(*) FROM predictions WHERE date = ?", (date_str,)
                    ).fetchone()[0]
                return conn.execute(
                    "SELECT COUNT(*) FROM predictions"
                ).fetchone()[0]
        except Exception as exc:
            logger.warning("row_count failed (non-fatal): %s", exc)
            return 0

    # ── Internal ───────────────────────────────────────────────────────────────

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(_CREATE_TABLE)
            conn.execute(_CREATE_INDEX_DATE)
            conn.execute(_CREATE_INDEX_PLAYER)
            conn.execute(_CREATE_INDEX_PENDING)
            # Phase 1 migration — add sportsbook_line if DB was created before this column existed.
            try:
                conn.execute("ALTER TABLE predictions ADD COLUMN sportsbook_line REAL;")
            except Exception:
                pass  # column already exists — safe to ignore

    def _extract_hit_rows(self, results: list[dict], date_str: str) -> list[dict]:
        rows = []
        for r in results:
            if not isinstance(r, dict):
                continue
            pdict  = r.get("player", {})
            vpdict = r.get("vs_pitcher", {})
            comp   = r.get("component_scores", {})
            row = self._base_row(date_str, pdict, r, comp)
            row["prop_type"]         = "hit"
            row["model_probability"] = float(r.get("hit_probability", 0))
            row["verdict"]           = str(r.get("hit_verdict", ""))
            row["pitcher_xera"]      = _safe_float(vpdict.get("xera"))
            row["pitcher_hr9"]       = _safe_float(vpdict.get("hr9"))
            rows.append(row)
        return rows

    def _extract_hr_rows(self, results: list[dict], date_str: str) -> list[dict]:
        rows = []
        for r in results:
            if not isinstance(r, dict):
                continue
            pdict  = r.get("player", {})
            vpdict = r.get("vs_pitcher", {})
            comp   = r.get("component_scores", {})
            row = self._base_row(date_str, pdict, r, comp)
            row["prop_type"]         = "hr"
            row["model_probability"] = float(r.get("hr_probability", 0))
            row["verdict"]           = str(r.get("hr_verdict", ""))
            row["pitcher_xera"]      = _safe_float(vpdict.get("xera"))
            row["pitcher_hr9"]       = _safe_float(vpdict.get("hr9"))
            rows.append(row)
        return rows

    def _base_row(self, date_str: str, pdict: dict, r: dict, comp: dict) -> dict:
        return {
            "date":                 date_str,
            "player_id":            int(pdict.get("player_id", 0) or 0),
            "player_name":          str(pdict.get("name", "")),
            "team":                 str(pdict.get("team", "")),
            "lineup_status":        str(r.get("lineup_status", "")),
            "lineup_position":      _safe_int(pdict.get("lineup_position")),
            "xba":                  _safe_float(pdict.get("xba")),
            "xwoba":                _safe_float(pdict.get("xwoba")),
            "barrel_pct":           _safe_float(pdict.get("barrel_pct")),
            "exit_velocity":        _safe_float(pdict.get("avg_exit_velo")),    # BatterMetrics field name
            "ev50":                 _safe_float(pdict.get("ev50")),
            "hard_hit_pct":         _safe_float(pdict.get("hard_hit_pct")),
            "sweet_spot_pct":       _safe_float(pdict.get("sweet_spot_pct")),
            "whiff_pct":            _safe_float(pdict.get("whiff_pct")),
            "launch_angle_pct":     _safe_float(pdict.get("ideal_la_pct")),     # BatterMetrics field name
            "hr_fb_ratio":          _safe_float(pdict.get("hr_fb_ratio")),
            "park_factor":          _safe_float(comp.get("park_factor")),
            "platoon_adv":          _safe_float(comp.get("platoon_adv")),
            "weather_adjustment":   _safe_float(r.get("weather_adjustment")),
            "component_scores_json": json.dumps(comp) if comp else None,
            "logged_at":            datetime.now(timezone.utc).isoformat(),
        }

    def _bulk_insert(self, rows: list[dict]) -> int:
        if not rows:
            return 0
        cols = [
            "date", "player_id", "player_name", "team", "prop_type",
            "lineup_status", "lineup_position",
            "model_probability", "verdict",
            "xba", "xwoba", "barrel_pct", "exit_velocity", "ev50",
            "hard_hit_pct", "sweet_spot_pct", "whiff_pct",
            "launch_angle_pct", "hr_fb_ratio",
            "pitcher_xera", "pitcher_hr9",
            "park_factor", "platoon_adv", "weather_adjustment",
            "component_scores_json", "logged_at",
        ]
        placeholders = ", ".join("?" * len(cols))
        sql = (
            f"INSERT OR IGNORE INTO predictions ({', '.join(cols)}) "
            f"VALUES ({placeholders})"
        )
        inserted = 0
        with self._connect() as conn:
            for row in rows:
                values = tuple(row.get(c) for c in cols)
                cursor = conn.execute(sql, values)
                inserted += cursor.rowcount
        return inserted


# ── Helpers ────────────────────────────────────────────────────────────────────

def _safe_float(val: Any) -> float | None:
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _safe_int(val: Any) -> int | None:
    if val is None:
        return None
    try:
        return int(val)
    except (TypeError, ValueError):
        return None
