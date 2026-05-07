# training/feature_store.py
# Reads logged prediction rows from SQLite and builds clean ML-ready datasets.
#
# USAGE:
#   python training/feature_store.py                            # export both hit + hr CSVs
#   python training/feature_store.py --prop hit                 # hit props only
#   python training/feature_store.py --prop hr                  # HR props only
#   python training/feature_store.py --prop hit --min-date 2026-05-01
#   python training/feature_store.py --prop hit --output training/data/hits.csv
#   python training/feature_store.py --summary                  # print stats, no files written
#
# OUTPUT COLUMNS (in CSV):
#   Metadata   — date, player_id, player_name, team, prop_type, lineup_status,
#                lineup_position, verdict, logged_at
#   Target     — actual_result (1=over/0=under), actual_stat_value
#   Model      — model_probability  (for comparison vs ML model later)
#   Odds       — sportsbook_odds, sportsbook_line, implied_probability, edge, best_book
#                (NULL/NaN when no odds refresh was triggered for that date)
#   Features   — all stat columns (listed in HIT_FEATURES / HR_FEATURES below)
#
# MISSING VALUES:
#   - Odds columns are NaN when not available — left as-is in the CSV so callers
#     can decide whether to impute, drop, or use a "has_odds" indicator.
#   - Feature columns should be fully populated once the logger field-name fixes
#     land (exit_velocity, launch_angle_pct were broken before 2026-05-02).
#     Rows with >50% feature nulls are flagged in the summary report.
#   - apply_median_imputation() is provided for training pipelines that need a
#     complete matrix (e.g. logistic regression). Tree models (XGBoost, LightGBM)
#     handle NaN natively — skip imputation for those.

import argparse
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd

import config

logger = logging.getLogger(__name__)

# ── Column groups ──────────────────────────────────────────────────────────────

# Columns exported in every dataset regardless of prop type
_META_COLS = [
    "date", "player_id", "player_name", "team",
    "prop_type", "lineup_status", "lineup_position",
    "verdict", "logged_at",
]

_TARGET_COLS = [
    "actual_result",        # 1 = over (≥1 hit / ≥1 HR), 0 = under — training label
    "actual_stat_value",    # raw count (0, 1, 2, …)
]

_MODEL_COLS = [
    "model_probability",    # current rules-based model output (0–1)
]

_ODDS_COLS = [
    "sportsbook_odds",       # American odds string e.g. "+350" — NaN if not available
    "sportsbook_line",       # prop line (0.5 for anytime hit/HR)
    "implied_probability",   # derived from sportsbook_odds — NaN if not available
    "edge",                  # model_probability - implied_probability — NaN if not available
    "best_book",             # bookmaker name — NaN if not available
]

# Raw feature columns shared by both hit and HR models.
# The hit model uses a subset; the HR model uses a different weighted subset.
# Both are included in both CSVs so you can experiment with cross-prop features.
HIT_FEATURES = [
    # Batter contact profile (primary hit predictors)
    "xba",              # expected batting average (most predictive)
    "xwoba",            # expected weighted on-base average
    "hard_hit_pct",     # % balls hit 95+ mph
    "sweet_spot_pct",   # % balls hit 8–32° launch angle
    "whiff_pct",        # swings and misses / total swings (inverted: lower = better)
    "ev50",             # average of top 50% exit velocities
    "exit_velocity",    # avg exit velocity (may be sparse for early-season data)
    # Power/HR profile (also useful for hit prediction at the margins)
    "barrel_pct",       # % barrels (95+ mph, optimal launch angle)
    "hr_fb_ratio",      # HR per fly ball
    "launch_angle_pct", # % ideal launch angle (may be sparse for early-season data)
    # Pitcher factors
    "pitcher_xera",     # opposing pitcher expected ERA
    "pitcher_hr9",      # opposing pitcher HR per 9 innings
    # Context
    "park_factor",      # hit park factor (100 = neutral)
    "platoon_adv",      # platoon advantage score (0–1)
    "weather_adjustment", # temperature/dome multiplier applied to model
    "lineup_position",  # batting order slot (1–9)
]

HR_FEATURES = [
    # Power profile (primary HR predictors)
    "barrel_pct",
    "ev50",
    "exit_velocity",
    "launch_angle_pct",
    "hr_fb_ratio",
    "xwoba",
    # Contact quality (secondary)
    "hard_hit_pct",
    "sweet_spot_pct",
    "xba",
    "whiff_pct",
    # Pitcher factors
    "pitcher_hr9",
    "pitcher_xera",
    # Context
    "park_factor",
    "platoon_adv",
    "weather_adjustment",
    "lineup_position",
]


class FeatureStore:
    """Reads SQLite predictions and returns clean pandas DataFrames."""

    def __init__(self, db_path: str | None = None) -> None:
        self.db_path = db_path or config.PREDICTIONS_DB_PATH

    # ── Public API ─────────────────────────────────────────────────────────────

    def build_dataset(
        self,
        prop_type: str,              # "hit" | "hr"
        min_date: str | None = None,
        max_date: str | None = None,
        labeled_only: bool = True,   # if True, drop rows where actual_result IS NULL
    ) -> pd.DataFrame:
        """Return a clean DataFrame ready for ML training or analysis.

        Args:
            prop_type:    "hit" or "hr".
            min_date:     Earliest date to include (YYYY-MM-DD), inclusive.
            max_date:     Latest date to include (YYYY-MM-DD), inclusive.
            labeled_only: When True (default), only include rows with known outcomes.

        Returns:
            DataFrame with meta + target + model + odds + feature columns.
            Columns are in a consistent order regardless of what's NULL.
        """
        import sqlite3

        prop_type = prop_type.lower()
        if prop_type not in ("hit", "hr"):
            raise ValueError(f"prop_type must be 'hit' or 'hr', got {prop_type!r}")

        feature_cols = HIT_FEATURES if prop_type == "hit" else HR_FEATURES

        # Build all columns we want in final order — deduplicate while preserving order.
        # lineup_position lives in both _META_COLS and feature_cols; keep the first occurrence.
        seen: set[str] = set()
        all_cols: list[str] = []
        for col in _META_COLS + _TARGET_COLS + _MODEL_COLS + _ODDS_COLS + feature_cols:
            if col not in seen:
                all_cols.append(col)
                seen.add(col)

        # Build the SQL query — select all columns from predictions
        where_clauses = [f"prop_type = '{prop_type}'"]
        if labeled_only:
            where_clauses.append("actual_result IS NOT NULL")
        if min_date:
            where_clauses.append(f"date >= '{min_date}'")
        if max_date:
            where_clauses.append(f"date <= '{max_date}'")

        where_sql = " AND ".join(where_clauses)
        sql = f"SELECT * FROM predictions WHERE {where_sql} ORDER BY date, player_id"

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            raw_df = pd.read_sql_query(sql, conn)
        finally:
            conn.close()

        if raw_df.empty:
            logger.warning(
                "No %s rows found (labeled_only=%s, min_date=%s, max_date=%s)",
                prop_type, labeled_only, min_date, max_date,
            )
            return pd.DataFrame(columns=all_cols)

        # Select and order columns — handle any that are missing from the DB
        present = [c for c in all_cols if c in raw_df.columns]
        missing_cols = [c for c in all_cols if c not in raw_df.columns]
        if missing_cols:
            logger.warning(
                "Columns not in DB (will be NaN): %s", missing_cols
            )
            for col in missing_cols:
                raw_df[col] = float("nan")

        df = raw_df[all_cols].copy()

        # Ensure correct dtypes
        df["actual_result"]    = pd.to_numeric(df["actual_result"],    errors="coerce").astype("Int8")
        df["actual_stat_value"] = pd.to_numeric(df["actual_stat_value"], errors="coerce")
        df["model_probability"] = pd.to_numeric(df["model_probability"], errors="coerce")
        df["lineup_position"]   = pd.to_numeric(df["lineup_position"],   errors="coerce").astype("Int8")

        for col in feature_cols + ["implied_probability", "edge", "sportsbook_line"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")

        logger.info(
            "Built %s dataset: %d rows, %d features, %d labeled",
            prop_type, len(df), len(feature_cols),
            df["actual_result"].notna().sum(),
        )
        return df

    def summary(self, prop_type: str | None = None) -> None:
        """Print a human-readable summary of the prediction DB without writing files."""
        import sqlite3

        conn = sqlite3.connect(self.db_path)

        print(f"\n{'='*60}")
        print(f" Feature Store Summary  —  {self.db_path}")
        print(f"{'='*60}")

        # Row counts
        props = [prop_type] if prop_type else ["hit", "hr"]
        for pt in props:
            rows = conn.execute(f"""
                SELECT
                    COUNT(*) as total,
                    SUM(CASE WHEN actual_result IS NOT NULL THEN 1 ELSE 0 END) as labeled,
                    SUM(CASE WHEN actual_result = 1 THEN 1 ELSE 0 END) as over_count,
                    SUM(CASE WHEN actual_result = 0 THEN 1 ELSE 0 END) as under_count,
                    SUM(CASE WHEN implied_probability IS NOT NULL THEN 1 ELSE 0 END) as has_odds,
                    MIN(date) as earliest,
                    MAX(date) as latest,
                    COUNT(DISTINCT date) as n_dates
                FROM predictions WHERE prop_type = ?
            """, (pt,)).fetchone()

            if not rows or rows[0] == 0:
                print(f"\n  {pt.upper()} props: no data")
                continue

            total, labeled, over_c, under_c, has_odds, earliest, latest, n_dates = rows
            rate = over_c / labeled if labeled else 0.0
            print(f"\n  {pt.upper()} props")
            print(f"    Dates:    {earliest} to {latest}  ({n_dates} day{'s' if n_dates != 1 else ''})")
            print(f"    Total rows:  {total:,}  (labeled={labeled:,}, unlabeled={total-labeled:,})")
            print(f"    Outcomes:    OVER={over_c:,}  UNDER={under_c:,}  hit-rate={rate:.1%}")
            print(f"    Has odds:    {has_odds:,} rows  ({100*has_odds/total:.0f}%)")

        # Feature null rates
        print(f"\n  {'-'*40}")
        print("  Feature null rates (all labeled rows):")
        feature_cols = list(dict.fromkeys(HIT_FEATURES + HR_FEATURES))  # deduplicated, ordered
        total_labeled = conn.execute(
            "SELECT COUNT(*) FROM predictions WHERE actual_result IS NOT NULL"
        ).fetchone()[0]

        if total_labeled > 0:
            for col in feature_cols:
                try:
                    nulls = conn.execute(
                        f"SELECT COUNT(*) FROM predictions WHERE actual_result IS NOT NULL AND {col} IS NULL"
                    ).fetchone()[0]
                    pct = 100 * nulls / total_labeled
                    flag = "  *** sparse — check logger field names" if pct > 50 else ""
                    bar  = "#" * min(20, int((100 - pct) / 5))
                    print(f"    {col:22s}  {100-pct:5.1f}% populated  {bar}{flag}")
                except Exception:
                    print(f"    {col:22s}  (column not found)")
        print()
        conn.close()

    def export_csv(
        self,
        df: pd.DataFrame,
        path: str,
        overwrite: bool = False,
    ) -> str:
        """Write DataFrame to CSV, creating parent directories if needed.

        Returns the absolute path of the written file.
        """
        abs_path = os.path.abspath(path)
        os.makedirs(os.path.dirname(abs_path), exist_ok=True)

        if os.path.exists(abs_path) and not overwrite:
            raise FileExistsError(
                f"{abs_path} already exists. Pass overwrite=True or choose a different path."
            )

        df.to_csv(abs_path, index=False)
        logger.info("Wrote %d rows to %s", len(df), abs_path)
        return abs_path


# ── Missing value utilities ────────────────────────────────────────────────────

def apply_median_imputation(
    df: pd.DataFrame,
    feature_cols: list[str],
    fill_value: float | None = None,
) -> tuple[pd.DataFrame, dict[str, float]]:
    """Fill NaN feature values with column medians (or a fixed fill_value).

    Use for models that require a complete feature matrix (e.g. logistic
    regression, SVM).  Tree-based models (XGBoost, LightGBM, sklearn trees)
    handle NaN natively — skip this for those.

    Returns:
        (imputed_df, fill_values_dict) — fill_values_dict maps col → value used,
        so you can apply the same fills to future inference data.
    """
    df = df.copy()
    fills: dict[str, float] = {}
    for col in feature_cols:
        if col not in df.columns:
            continue
        if df[col].isna().any():
            val = fill_value if fill_value is not None else df[col].median()
            fills[col] = float(val) if val is not None else 0.0
            df[col] = df[col].fillna(fills[col])
    return df, fills


def feature_null_report(df: pd.DataFrame, feature_cols: list[str]) -> pd.DataFrame:
    """Return a DataFrame showing null counts and rates per feature column."""
    rows = []
    for col in feature_cols:
        if col not in df.columns:
            rows.append({"feature": col, "null_count": len(df), "null_pct": 100.0, "note": "missing column"})
            continue
        nulls = int(df[col].isna().sum())
        rows.append({
            "feature":    col,
            "null_count": nulls,
            "null_pct":   round(100 * nulls / len(df), 1) if len(df) else 0.0,
            "note":       "*** sparse" if nulls / len(df) > 0.5 else "",
        })
    return pd.DataFrame(rows)


# ── CLI ────────────────────────────────────────────────────────────────────────

def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    parser = argparse.ArgumentParser(
        description="Export ML-ready feature CSVs from the predictions SQLite DB."
    )
    parser.add_argument(
        "--prop",
        choices=["hit", "hr", "both"],
        default="both",
        help="Prop type to export (default: both).",
    )
    parser.add_argument(
        "--output",
        default=None,
        help=(
            "Output CSV path. When exporting both prop types, this is used as a "
            "prefix: <output>_hits.csv and <output>_hr.csv. "
            "Defaults to training/data/hits_<date>.csv etc."
        ),
    )
    parser.add_argument(
        "--min-date",
        default=None,
        help="Only include rows on or after this date (YYYY-MM-DD).",
    )
    parser.add_argument(
        "--max-date",
        default=None,
        help="Only include rows on or before this date (YYYY-MM-DD).",
    )
    parser.add_argument(
        "--include-unlabeled",
        action="store_true",
        help="Include rows where actual_result is NULL (no outcome yet).",
    )
    parser.add_argument(
        "--summary",
        action="store_true",
        help="Print DB summary and exit without writing any files.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite existing output files.",
    )
    args = parser.parse_args()

    fs = FeatureStore()

    if args.summary:
        fs.summary()
        return

    from datetime import date
    today = date.today().isoformat()
    props = ["hit", "hr"] if args.prop == "both" else [args.prop]

    for pt in props:
        df = fs.build_dataset(
            prop_type=pt,
            min_date=args.min_date,
            max_date=args.max_date,
            labeled_only=not args.include_unlabeled,
        )

        if df.empty:
            print(f"No labeled {pt} rows found — nothing written.")
            continue

        # Determine output path
        if args.output:
            if args.prop == "both":
                suffix = "hits" if pt == "hit" else "hr"
                path = f"{args.output}_{suffix}.csv"
            else:
                path = args.output
        else:
            data_dir = os.path.join(os.path.dirname(__file__), "data")
            suffix   = "hits" if pt == "hit" else "hr"
            path     = os.path.join(data_dir, f"{suffix}_{today}.csv")

        try:
            out = fs.export_csv(df, path, overwrite=args.overwrite)
            feature_cols = HIT_FEATURES if pt == "hit" else HR_FEATURES
            null_df = feature_null_report(df, feature_cols)
            sparse = null_df[null_df["null_pct"] > 50]

            print(f"\n  {pt.upper()} props exported: {out}")
            print(f"    Rows:     {len(df):,}  ({df['actual_result'].notna().sum():,} labeled)")
            print(f"    Features: {len(feature_cols)}")
            print(f"    Outcome rate (OVER): {df['actual_result'].mean():.1%}")
            if not sparse.empty:
                print(f"    *** Sparse features (>50% null):")
                for _, row in sparse.iterrows():
                    print(f"        {row['feature']:22s}  {row['null_pct']:.0f}% null  {row['note']}")
        except FileExistsError as exc:
            print(f"  Skipped ({exc}). Use --overwrite to replace.")


if __name__ == "__main__":
    main()
