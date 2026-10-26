"""Train and evaluate the calibrated model from a resolved-market CSV.

The CSV must contain the ten feature columns from ``features.py`` plus a binary
``resolved_yes`` label. Include an ``end_date`` (or ``resolved_at``) column so
rows can be sorted chronologically. Never randomly split time-series data.
"""

from __future__ import annotations

import argparse
import json
import logging
from itertools import pairwise
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.metrics import brier_score_loss, log_loss, roc_auc_score

from features import FEATURE_COLUMNS
from model import ProbabilityModel

logger = logging.getLogger("train_model")
LABEL_COLUMN = "resolved_yes"


def expected_calibration_error(
    y_true: np.ndarray, probabilities: np.ndarray, bins: int = 10
) -> float:
    edges = np.linspace(0.0, 1.0, bins + 1)
    total = len(y_true)
    if total == 0:
        return float("nan")
    error = 0.0
    for lower, upper in pairwise(edges):
        if upper == 1.0:
            mask = (probabilities >= lower) & (probabilities <= upper)
        else:
            mask = (probabilities >= lower) & (probabilities < upper)
        count = int(mask.sum())
        if count == 0:
            continue
        observed = float(y_true[mask].mean())
        predicted = float(probabilities[mask].mean())
        error += (count / total) * abs(observed - predicted)
    return float(error)


def evaluate(model: ProbabilityModel, frame: pd.DataFrame, labels: np.ndarray) -> dict[str, float]:
    probabilities = np.array(
        [model.predict_proba(frame.iloc[[index]]) for index in range(len(frame))]
    )
    metrics = {
        "samples": float(len(labels)),
        "positive_rate": float(labels.mean()),
        "brier": float(brier_score_loss(labels, probabilities)),
        "log_loss": float(log_loss(labels, probabilities, labels=[0, 1])),
        "ece_10": expected_calibration_error(labels, probabilities),
    }
    if len(np.unique(labels)) == 2:
        metrics["auc"] = float(roc_auc_score(labels, probabilities))
    return metrics


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", required=True, help="Resolved-market CSV")
    parser.add_argument("--output", default="model.joblib")
    parser.add_argument("--holdout-fraction", type=float, default=0.20)
    parser.add_argument(
        "--skip-holdout",
        action="store_true",
        help="Fit on all rows without printing an out-of-time calibration report",
    )
    return parser.parse_args()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = parse_args()
    data = pd.read_csv(args.data)

    missing = [column for column in FEATURE_COLUMNS if column not in data]
    if missing:
        raise SystemExit(f"Training data missing features: {missing}")
    if LABEL_COLUMN not in data:
        raise SystemExit(f"Training data missing label column {LABEL_COLUMN!r}")

    data = data.dropna(subset=[LABEL_COLUMN]).copy()
    data[LABEL_COLUMN] = data[LABEL_COLUMN].astype(int)
    if not set(data[LABEL_COLUMN].unique()).issubset({0, 1}):
        raise SystemExit("resolved_yes must contain only 0 and 1")

    timestamp_column = next(
        (column for column in ("end_date", "resolved_at", "timestamp") if column in data),
        None,
    )
    if timestamp_column:
        data[timestamp_column] = pd.to_datetime(data[timestamp_column], utc=True)
        data = data.sort_values(timestamp_column, kind="stable").reset_index(drop=True)
        logger.info("Sorted by %s", timestamp_column)
    else:
        logger.warning(
            "No end_date/resolved_at column; assuming CSV rows are already chronological"
        )

    X = data.loc[:, FEATURE_COLUMNS]
    y = data[LABEL_COLUMN].to_numpy()
    timestamps = data[timestamp_column] if timestamp_column else None

    holdout_metrics: dict[str, float] | None = None
    split = int(len(data) * (1.0 - args.holdout_fraction))
    if not args.skip_holdout and split >= 60 and len(data) - split >= 20:
        train_X, train_y = X.iloc[:split], y[:split]
        test_X, test_y = X.iloc[split:], y[split:]
        train_timestamps = timestamps.iloc[:split] if timestamps is not None else None
        model = ProbabilityModel().fit(train_X, train_y, timestamps=train_timestamps)
        holdout_metrics = evaluate(model, test_X, test_y)
        logger.info("Out-of-time holdout calibration: %s", holdout_metrics)
        if holdout_metrics["ece_10"] > 0.10:
            logger.warning("Holdout ECE exceeds 0.10; do not trade this model live")
    elif not args.skip_holdout:
        logger.warning("Not enough rows for a meaningful holdout; fitting all data")

    final_model = ProbabilityModel().fit(
        X,
        y,
        timestamps=timestamps,
        metadata={
            "training_file": str(args.data),
            "holdout_metrics": holdout_metrics,
            "feature_columns": FEATURE_COLUMNS,
        },
    )
    destination = final_model.save(args.output)
    print(json.dumps({"model": str(Path(destination)), "holdout": holdout_metrics}, indent=2))


if __name__ == "__main__":
    main()
