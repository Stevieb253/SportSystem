# training/train_hits.py
# Train a binary classifier predicting hit prop outcomes (OVER/UNDER 0.5 hits).
#
# USAGE:
#   python training/train_hits.py                  # train and save
#   python training/train_hits.py --cv-only        # cross-validate, do not save
#   python training/train_hits.py --min-date 2026-05-01
#   python training/train_hits.py --model rf       # rf | lr | gb (default: best CV)
#
# DATA REQUIREMENTS (honest):
#   This script works at any sample size but prints warnings when data is too
#   small for reliable estimates.  Rough thresholds:
#       < 200 rows  → results are noise, treat as smoke-test only
#       200–1000    → early signal, cross-validate carefully
#       1000–5000   → decent model, time-based split becomes meaningful
#       5000+       → full time-based train/test split + calibration
#
# SPLIT STRATEGY:
#   - If data spans 3+ dates: hold out the most recent date as test set,
#     train on everything before it. This avoids leakage.
#   - If data spans 1-2 dates: stratified 5-fold CV only (no holdout split).
#     The model is still saved but the evaluation numbers are optimistic.
#
# OUTPUT:
#   ml_models/hits_v1.joblib          trained sklearn Pipeline
#   ml_models/hits_v1_features.json   feature list + training metadata
#   ml_models/hits_v1_report.txt      full evaluation report (appended each run)

import argparse
import json
import logging
import os
import sys
import warnings
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import joblib
import numpy as np
import pandas as pd
from scipy.stats import binomtest  # for significance vs baseline

from sklearn.calibration import calibration_curve
from sklearn.ensemble import GradientBoostingClassifier, RandomForestClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (
    accuracy_score,
    log_loss,
    precision_score,
    recall_score,
    roc_auc_score,
)
from sklearn.model_selection import StratifiedKFold, cross_validate
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from training.feature_store import FeatureStore

warnings.filterwarnings("ignore", category=UserWarning)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("train_hits")

# ── Constants ──────────────────────────────────────────────────────────────────

# Features dropped due to 100% NULL in initial data (field name bugs, now fixed).
# Remove from this list once they start populating in the DB.
SPARSE_FEATURES = {"exit_velocity", "launch_angle_pct"}

# Full stable feature set (drop SPARSE_FEATURES at runtime)
ALL_FEATURES = [
    "xba",
    "xwoba",
    "hard_hit_pct",
    "sweet_spot_pct",
    "whiff_pct",
    "ev50",
    "barrel_pct",
    "hr_fb_ratio",
    "pitcher_xera",
    "pitcher_hr9",
    "park_factor",
    "platoon_adv",
    "weather_adjustment",
    "lineup_position",
    # Sparse — kept in list but filtered at runtime
    "exit_velocity",
    "launch_angle_pct",
]

TARGET = "actual_result"

# Model definitions — all sklearn so no extra installs needed
_MODELS: dict[str, Pipeline] = {
    "lr": Pipeline([
        ("scaler", StandardScaler()),
        ("clf", LogisticRegression(C=1.0, max_iter=2000, random_state=42)),
    ]),
    "rf": Pipeline([
        ("clf", RandomForestClassifier(
            n_estimators=200,
            max_depth=4,
            min_samples_leaf=5,
            random_state=42,
            n_jobs=-1,
        )),
    ]),
    "gb": Pipeline([
        ("clf", GradientBoostingClassifier(
            n_estimators=100,
            max_depth=3,
            learning_rate=0.05,
            subsample=0.8,
            min_samples_leaf=5,
            random_state=42,
        )),
    ]),
}

MODEL_NAMES = {"lr": "Logistic Regression", "gb": "Gradient Boosting", "rf": "Random Forest"}

ML_MODELS_DIR = Path(__file__).parent.parent / "ml_models"


# ── Main training function ─────────────────────────────────────────────────────

def train(
    min_date: str | None = None,
    max_date: str | None = None,
    model_key: str | None = None,   # None = pick best CV ROC-AUC
    cv_only: bool = False,
    save: bool = True,
) -> dict:
    """Load data, cross-validate all models, save best, return result dict."""

    # ── Load data ──────────────────────────────────────────────────────────────
    fs    = FeatureStore()
    df    = fs.build_dataset("hit", min_date=min_date, max_date=max_date)
    dates = sorted(df["date"].unique())

    print(f"\n{'='*62}")
    print(f"  Hit Model Training  v1  (experimental)")
    print(f"{'='*62}")
    print(f"  Rows:     {len(df):,}")
    print(f"  Dates:    {dates[0]} to {dates[-1]}  ({len(dates)} day{'s' if len(dates)!=1 else ''})")
    print(f"  OVER rate: {df[TARGET].mean():.1%}  (naive baseline = always-OVER accuracy)")

    if len(df) < 200:
        print(f"\n  *** WARNING: {len(df)} rows is too small for reliable ML estimates.")
        print(f"  *** Results are a smoke-test only. Collect 1000+ rows before trusting this model.")

    # ── Select features ────────────────────────────────────────────────────────
    # Drop any feature that is >50% null — they add noise, not signal
    null_rates   = df[ALL_FEATURES].isna().mean()
    sparse_now   = set(null_rates[null_rates > 0.5].index) | SPARSE_FEATURES
    feature_cols = [f for f in ALL_FEATURES if f not in sparse_now and f in df.columns]
    dropped      = [f for f in ALL_FEATURES if f in sparse_now]

    print(f"\n  Features used ({len(feature_cols)}): {', '.join(feature_cols)}")
    if dropped:
        print(f"  Dropped (>50% null): {', '.join(dropped)}")

    X = df[feature_cols].values
    y = df[TARGET].astype(int).values

    base_rate = y.mean()

    # ── Split strategy ─────────────────────────────────────────────────────────
    has_time_split = len(dates) >= 3
    if has_time_split:
        test_date  = dates[-1]
        train_mask = df["date"] < test_date
        test_mask  = df["date"] == test_date
        X_train, y_train = X[train_mask], y[train_mask]
        X_test,  y_test  = X[test_mask],  y[test_mask]
        split_desc = f"time-based: train={train_mask.sum()} rows ({dates[0]} to {dates[-2]}), test={test_mask.sum()} rows ({test_date})"
    else:
        X_train, y_train = X, y
        X_test,  y_test  = None, None
        split_desc = f"CV only (need 3+ dates for time-based holdout — currently have {len(dates)})"

    print(f"\n  Split:    {split_desc}")

    # ── Cross-validation ───────────────────────────────────────────────────────
    cv     = StratifiedKFold(n_splits=5, shuffle=True, random_state=42)
    cv_results: dict[str, dict] = {}

    print(f"\n  {'-'*58}")
    print(f"  {'Model':25s}  {'AUC':>6}  {'LL':>7}  {'Acc':>6}  {'Prec':>6}  {'Rec':>6}")
    print(f"  {'-'*58}")

    for key, pipeline in _MODELS.items():
        scores = cross_validate(
            pipeline, X_train, y_train,
            cv=cv,
            scoring=["roc_auc", "neg_log_loss", "accuracy", "precision", "recall"],
            error_score="raise",
        )
        cv_results[key] = {
            "roc_auc":   scores["test_roc_auc"].mean(),
            "log_loss":  -scores["test_neg_log_loss"].mean(),
            "accuracy":  scores["test_accuracy"].mean(),
            "precision": scores["test_precision"].mean(),
            "recall":    scores["test_recall"].mean(),
            "auc_std":   scores["test_roc_auc"].std(),
        }
        r = cv_results[key]
        print(
            f"  {MODEL_NAMES[key]:25s}  "
            f"{r['roc_auc']:.4f}  {r['log_loss']:.4f}  "
            f"{r['accuracy']:.4f}  {r['precision']:.4f}  {r['recall']:.4f}"
        )

    # Naive baseline (always predict OVER)
    naive_acc = base_rate
    print(f"  {'Naive (always OVER)':25s}  {'N/A':>6}  {'N/A':>7}  {naive_acc:.4f}  {base_rate:.4f}  {'1.0000':>6}")
    print(f"  {'-'*58}")

    # ── Select best model ──────────────────────────────────────────────────────
    if model_key:
        best_key = model_key
    else:
        best_key = max(cv_results, key=lambda k: cv_results[k]["roc_auc"])

    print(f"\n  Best model (CV ROC-AUC): {MODEL_NAMES[best_key]}")

    # ── Train final model on full training data ────────────────────────────────
    import copy
    final_model = copy.deepcopy(_MODELS[best_key])
    final_model.fit(X_train, y_train)

    # ── Held-out evaluation (if time split available) ──────────────────────────
    holdout_metrics: dict = {}
    if X_test is not None and len(X_test) > 0:
        y_pred      = final_model.predict(X_test)
        y_proba     = final_model.predict_proba(X_test)[:, 1]
        holdout_metrics = {
            "roc_auc":   roc_auc_score(y_test, y_proba),
            "log_loss":  log_loss(y_test, y_proba),
            "accuracy":  accuracy_score(y_test, y_pred),
            "precision": precision_score(y_test, y_pred, zero_division=0),
            "recall":    recall_score(y_test, y_pred, zero_division=0),
            "n_test":    len(y_test),
        }
        print(f"\n  Held-out test ({test_date}, n={len(y_test)}):")
        for k, v in holdout_metrics.items():
            if k != "n_test":
                print(f"    {k:12s}: {v:.4f}")

    # ── Calibration check (on training data via CV predictions) ───────────────
    print(f"\n  Calibration check (5-fold CV predictions):")
    print(f"  {'Pred bucket':12s}  {'Predicted':>10}  {'Actual':>8}  {'Count':>6}")
    cv_probas = _get_cv_probas(final_model, X_train, y_train, cv)
    _print_calibration(cv_probas, y_train)

    # ── Rules-model comparison ─────────────────────────────────────────────────
    rules_proba = df.loc[df["date"].isin(dates), "model_probability"].values
    if has_time_split:
        rules_proba_train = df.loc[train_mask, "model_probability"].values
    else:
        rules_proba_train = rules_proba

    # Convert rules model prob to binary prediction using 0.60 threshold
    rules_pred = (rules_proba_train >= 0.60).astype(int)
    rules_auc  = roc_auc_score(y_train, rules_proba_train)
    rules_acc  = accuracy_score(y_train, rules_pred)
    rules_ll   = log_loss(y_train, np.clip(rules_proba_train, 1e-6, 1 - 1e-6))

    ml_cv_auc  = cv_results[best_key]["roc_auc"]
    ml_cv_ll   = cv_results[best_key]["log_loss"]

    print(f"\n  Rules model vs ML model (training data):")
    print(f"  {'':20s}  {'AUC':>8}  {'Log-Loss':>9}  {'Accuracy':>9}")
    print(f"  {'Rules (current)':20s}  {rules_auc:8.4f}  {rules_ll:9.4f}  {rules_acc:9.4f}")
    print(f"  {'ML ({})'.format(MODEL_NAMES[best_key][:10]):20s}  {ml_cv_auc:8.4f}  {ml_cv_ll:9.4f}  {cv_results[best_key]['accuracy']:9.4f}")

    # ── Statistical significance of improvement ───────────────────────────────
    n_correct_ml    = int(cv_results[best_key]["accuracy"] * len(y_train))
    n_correct_naive = int(base_rate * len(y_train))
    _print_significance(n_correct_ml, len(y_train), base_rate)

    # ── Save ───────────────────────────────────────────────────────────────────
    result = {
        "model_key":      best_key,
        "model_name":     MODEL_NAMES[best_key],
        "feature_cols":   feature_cols,
        "dropped_features": dropped,
        "n_train":        len(y_train),
        "n_dates":        len(dates),
        "date_range":     f"{dates[0]} to {dates[-1]}",
        "base_rate":      round(float(base_rate), 4),
        "cv_results":     {k: {m: round(v, 4) for m, v in r.items()} for k, r in cv_results.items()},
        "best_cv":        {m: round(v, 4) for m, v in cv_results[best_key].items()},
        "holdout":        {k: round(v, 4) for k, v in holdout_metrics.items() if k != "n_test"},
        "rules_auc":      round(float(rules_auc), 4),
        "trained_at":     datetime.now(timezone.utc).isoformat(),
        "status":         "experimental — insufficient data" if len(df) < 500 else "training",
    }

    if not cv_only and save:
        _save_model(final_model, feature_cols, result)

    return result


# ── Helpers ────────────────────────────────────────────────────────────────────

def _get_cv_probas(model, X, y, cv) -> np.ndarray:
    """Return out-of-fold predicted probabilities for calibration check."""
    import copy
    probas = np.zeros(len(y))
    for train_idx, val_idx in cv.split(X, y):
        m = copy.deepcopy(model)
        m.fit(X[train_idx], y[train_idx])
        probas[val_idx] = m.predict_proba(X[val_idx])[:, 1]
    return probas


def _print_calibration(probas: np.ndarray, y_true: np.ndarray) -> None:
    """Print a text calibration table: predicted bucket vs actual hit rate."""
    bins = [0.0, 0.45, 0.55, 0.62, 0.68, 0.75, 1.0]
    labels = ["<0.45", "0.45-0.55", "0.55-0.62", "0.62-0.68", "0.68-0.75", ">0.75"]
    for i, label in enumerate(labels):
        mask = (probas >= bins[i]) & (probas < bins[i + 1])
        if mask.sum() == 0:
            continue
        mean_pred   = probas[mask].mean()
        actual_rate = y_true[mask].mean()
        n           = mask.sum()
        bar = "#" * int(abs(actual_rate - mean_pred) * 40)
        print(f"  {label:12s}  pred={mean_pred:.3f}  actual={actual_rate:.3f}  n={n:3d}")


def _print_significance(n_correct: int, n_total: int, baseline_prob: float) -> None:
    """Print whether improvement over naive baseline is statistically significant."""
    try:
        p_val = binomtest(n_correct, n_total, baseline_prob, alternative="greater").pvalue
        sig   = "significant (p<0.05)" if p_val < 0.05 else "NOT significant vs naive baseline"
        print(f"\n  Significance vs naive baseline: p={p_val:.4f}  {sig}")
        if p_val >= 0.05:
            print(f"  *** Collect more data before trusting this model over the rules model.")
    except Exception:
        pass


def _save_model(model, feature_cols: list[str], metadata: dict) -> None:
    """Save trained model and metadata to ml_models/."""
    ML_MODELS_DIR.mkdir(parents=True, exist_ok=True)

    model_path   = ML_MODELS_DIR / "hits_v1.joblib"
    meta_path    = ML_MODELS_DIR / "hits_v1_features.json"
    report_path  = ML_MODELS_DIR / "hits_v1_report.txt"

    joblib.dump(model, model_path)
    log.info("Model saved: %s", model_path)

    with open(meta_path, "w") as f:
        json.dump(metadata, f, indent=2)
    log.info("Metadata saved: %s", meta_path)

    # Append this run to the report file so you can track improvement over time
    with open(report_path, "a") as f:
        f.write(f"\n{'='*62}\n")
        f.write(f"  Run: {metadata['trained_at']}\n")
        f.write(f"  Status: {metadata['status']}\n")
        f.write(f"  Model:  {metadata['model_name']}\n")
        f.write(f"  Data:   {metadata['n_train']} rows, {metadata['n_dates']} dates ({metadata['date_range']})\n")
        f.write(f"  CV AUC: {metadata['best_cv']['roc_auc']:.4f} +/- {metadata['best_cv']['auc_std']:.4f}\n")
        f.write(f"  Rules AUC (train data): {metadata['rules_auc']:.4f}\n")
        if metadata["holdout"]:
            f.write(f"  Holdout AUC: {metadata['holdout'].get('roc_auc', 'N/A')}\n")
        f.write(f"  Features ({len(feature_cols)}): {', '.join(feature_cols)}\n")
        if metadata["dropped_features"]:
            f.write(f"  Dropped: {', '.join(metadata['dropped_features'])}\n")

    print(f"\n  Saved:")
    print(f"    Model:    {model_path}")
    print(f"    Metadata: {meta_path}")
    print(f"    Report:   {report_path}")
    print(f"\n  Status: {metadata['status']}")
    if metadata["status"].startswith("experimental"):
        print(f"  The rules-based model stays active in the app.")
        print(f"  Re-run this script as data accumulates (target: 500+ rows).")


# ── CLI ────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Train hit prop binary classifier from logged prediction data."
    )
    parser.add_argument(
        "--model",
        choices=["lr", "rf", "gb"],
        default=None,
        help="Force a specific model (default: pick best CV ROC-AUC).",
    )
    parser.add_argument(
        "--min-date",
        default=None,
        help="Only use rows on or after this date (YYYY-MM-DD).",
    )
    parser.add_argument(
        "--max-date",
        default=None,
        help="Only use rows on or before this date (YYYY-MM-DD).",
    )
    parser.add_argument(
        "--cv-only",
        action="store_true",
        help="Run cross-validation and print results but do not save the model.",
    )
    parser.add_argument(
        "--no-save",
        action="store_true",
        help="Alias for --cv-only.",
    )
    args = parser.parse_args()

    train(
        min_date=args.min_date,
        max_date=args.max_date,
        model_key=args.model,
        cv_only=args.cv_only or args.no_save,
        save=not (args.cv_only or args.no_save),
    )


if __name__ == "__main__":
    main()
