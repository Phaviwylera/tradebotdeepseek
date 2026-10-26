"""Calibrated stacked probability model.

The model predicts P(YES resolves true). No model is shipped with the repo:
if ``model.joblib`` is absent the live bot deliberately degrades to arbitrage
and near-resolution sniping only.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from sklearn.base import BaseEstimator, ClassifierMixin, clone

from features import FEATURE_COLUMNS

logger = logging.getLogger(__name__)

MODEL_BUNDLE_VERSION = 1


class TimeSeriesStackingClassifier(ClassifierMixin, BaseEstimator):
    """Time-ordered equivalent of ``StackingClassifier``.

    scikit-learn's ``StackingClassifier`` calls ``cross_val_predict`` and
    therefore rejects ``TimeSeriesSplit`` (its folds are not partitions). This
    class performs the same job manually: out-of-fold base predictions from
    five expanding-window splits feed a logistic meta-model, base models are
    refitted on all data, and ``passthrough`` appends the original features.
    No future row is ever used to predict a past row.
    """

    def __init__(
        self,
        base_estimators: list[tuple[str, Any]] | None = None,
        final_estimator: Any | None = None,
        n_splits: int = 5,
        passthrough: bool = True,
    ) -> None:
        self.base_estimators = base_estimators
        self.final_estimator = final_estimator
        self.n_splits = n_splits
        self.passthrough = passthrough

    def __sklearn_clone__(self) -> TimeSeriesStackingClassifier:
        return TimeSeriesStackingClassifier(
            base_estimators=[
                (name, clone(estimator)) for name, estimator in (self.base_estimators or [])
            ],
            final_estimator=clone(self.final_estimator),
            n_splits=self.n_splits,
            passthrough=self.passthrough,
        )

    def fit(self, X: Any, y: Any) -> TimeSeriesStackingClassifier:
        from sklearn.linear_model import LogisticRegression
        from sklearn.model_selection import TimeSeriesSplit

        features = np.asarray(X, dtype=float)
        labels = np.asarray(y, dtype=int).reshape(-1)
        if len(features) != len(labels):
            raise ValueError("X and y have different lengths")
        if len(np.unique(labels)) < 2:
            raise ValueError("y must contain both classes")
        if not self.base_estimators:
            raise ValueError("base_estimators cannot be empty")

        self.classes_ = np.unique(labels)
        self.n_features_in_ = features.shape[1]
        splitter = TimeSeriesSplit(n_splits=self.n_splits)
        oof_predictions = np.full((len(features), len(self.base_estimators)), np.nan, dtype=float)

        for train_index, test_index in splitter.split(features):
            train_labels = labels[train_index]
            if len(np.unique(train_labels)) < 2:
                logger.warning("Skipping stacking fold with one training class")
                continue
            for column, (_, estimator) in enumerate(self.base_estimators):
                fold_model = clone(estimator)
                fold_model.fit(features[train_index], train_labels)
                classes = list(getattr(fold_model, "classes_", []))
                if 1 not in classes:
                    continue
                positive_index = classes.index(1)
                oof_predictions[test_index, column] = fold_model.predict_proba(
                    features[test_index]
                )[:, positive_index]

        complete = ~np.isnan(oof_predictions).any(axis=1)
        if complete.sum() < 10:
            raise ValueError("Not enough out-of-fold rows to fit the meta-model")

        meta_features = oof_predictions[complete]
        if self.passthrough:
            meta_features = np.column_stack((meta_features, features[complete]))
        meta_labels = labels[complete]
        if len(np.unique(meta_labels)) < 2:
            raise ValueError("Meta-model training rows contain only one class")

        self.final_estimator_ = clone(self.final_estimator or LogisticRegression(max_iter=2_000))
        self.final_estimator_.fit(meta_features, meta_labels)

        self.estimators_ = [
            clone(estimator).fit(features, labels) for _, estimator in self.base_estimators
        ]
        self.n_features_in_ = features.shape[1]
        return self

    def predict_proba(self, X: Any) -> np.ndarray:
        if not hasattr(self, "estimators_"):
            raise RuntimeError("TimeSeriesStackingClassifier is not fitted")
        features = np.asarray(X, dtype=float)
        base_predictions = []
        for estimator in self.estimators_:
            classes = list(getattr(estimator, "classes_", []))
            positive_index = classes.index(1) if 1 in classes else 0
            base_predictions.append(estimator.predict_proba(features)[:, positive_index])
        meta_features = np.column_stack(base_predictions)
        if self.passthrough:
            meta_features = np.column_stack((meta_features, features))
        return self.final_estimator_.predict_proba(meta_features)

    def predict(self, X: Any) -> np.ndarray:
        probabilities = self.predict_proba(X)
        positive_index = list(self.final_estimator_.classes_).index(1)
        return (probabilities[:, positive_index] >= 0.5).astype(int)


def _build_estimator() -> Any:
    """Construct the required time-ordered stack lazily."""
    from lightgbm import LGBMClassifier
    from sklearn.calibration import CalibratedClassifierCV
    from sklearn.ensemble import (
        ExtraTreesClassifier,
        HistGradientBoostingClassifier,
        RandomForestClassifier,
    )
    from sklearn.linear_model import LogisticRegression
    from sklearn.model_selection import TimeSeriesSplit
    from xgboost import XGBClassifier

    xgb = XGBClassifier(
        n_estimators=350,
        max_depth=4,
        learning_rate=0.04,
        subsample=0.85,
        colsample_bytree=0.85,
        min_child_weight=3,
        reg_lambda=2.0,
        objective="binary:logistic",
        eval_metric="logloss",
        tree_method="hist",
        n_jobs=1,
        random_state=42,
    )
    lgbm = LGBMClassifier(
        n_estimators=350,
        num_leaves=31,
        learning_rate=0.04,
        subsample=0.85,
        colsample_bytree=0.85,
        min_child_samples=20,
        reg_lambda=2.0,
        objective="binary",
        n_jobs=1,
        random_state=42,
        verbosity=-1,
        force_col_wise=True,
    )
    hgb = HistGradientBoostingClassifier(
        max_iter=350,
        learning_rate=0.05,
        max_leaf_nodes=31,
        l2_regularization=1.0,
        random_state=42,
    )
    extra = ExtraTreesClassifier(
        n_estimators=400,
        max_depth=None,
        min_samples_leaf=3,
        n_jobs=1,
        random_state=42,
        class_weight="balanced",
    )
    forest = RandomForestClassifier(
        n_estimators=400,
        max_depth=None,
        min_samples_leaf=3,
        n_jobs=1,
        random_state=42,
        class_weight="balanced_subsample",
    )

    stacking = TimeSeriesStackingClassifier(
        base_estimators=[
            ("xgb", xgb),
            ("lgbm", lgbm),
            ("hgb", hgb),
            ("extra_trees", extra),
            ("random_forest", forest),
        ],
        final_estimator=LogisticRegression(C=0.5, max_iter=2_000, class_weight="balanced"),
        n_splits=5,
        passthrough=True,
    )

    # Three chronological calibration folds. Using TimeSeriesSplit here (rather
    # than random K-fold) prevents future outcomes leaking into past folds.
    return CalibratedClassifierCV(
        estimator=stacking,
        method="isotonic",
        cv=TimeSeriesSplit(n_splits=3),
    )


class ProbabilityModel:
    def __init__(self) -> None:
        self.model: Any | None = None
        self.feature_columns: list[str] = list(FEATURE_COLUMNS)
        self.metadata: dict[str, Any] = {}

    @property
    def loaded(self) -> bool:
        return self.model is not None

    def fit(
        self,
        X: pd.DataFrame,
        y: Sequence[int] | np.ndarray,
        *,
        timestamps: Sequence[Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ProbabilityModel:
        """Fit on chronologically ordered rows (oldest first)."""
        if self.feature_columns:
            missing = [column for column in self.feature_columns if column not in X]
            if missing:
                raise ValueError(f"Missing model features: {missing}")
            frame = X.loc[:, self.feature_columns].astype(float).copy()
        else:
            frame = X.astype(float).copy()

        labels = np.asarray(y, dtype=int).reshape(-1)
        if len(frame) != len(labels):
            raise ValueError("X and y have different lengths")
        if len(frame) < 60:
            raise ValueError("At least 60 resolved samples are required to fit")
        if len(np.unique(labels)) < 2:
            raise ValueError("Training data must contain both resolution classes")

        if timestamps is not None:
            stamp_array = np.asarray(list(timestamps))
            if len(stamp_array) != len(frame):
                raise ValueError("timestamps and X have different lengths")
            order = np.argsort(stamp_array, kind="stable")
            frame = frame.iloc[order].reset_index(drop=True)
            labels = labels[order]

        # Prefer median imputation over silently manufacturing zeros for
        # missing market observations. Features.py already supplies complete
        # rows for live inference.
        medians = frame.median(numeric_only=True)
        frame = frame.fillna(medians).fillna(0.0)

        estimator = _build_estimator()
        estimator.fit(frame, labels)
        self.model = estimator
        self.feature_columns = list(frame.columns)

        model_metadata = {
            "trained_at": datetime.now(timezone.utc).isoformat(),
            "samples": len(frame),
            "positive_rate": float(labels.mean()),
            "model_bundle_version": MODEL_BUNDLE_VERSION,
        }
        if metadata:
            model_metadata.update(metadata)
        self.metadata = model_metadata
        logger.info(
            "Model fitted on %d samples (positive rate %.3f)",
            len(frame),
            labels.mean(),
        )
        return self

    def predict_proba(self, X: pd.DataFrame | dict[str, Any]) -> float:
        """Return calibrated P(YES resolves true) for one row."""
        if self.model is None:
            raise RuntimeError("Probability model is not loaded")
        frame = pd.DataFrame([X]) if isinstance(X, dict) else X.copy()
        missing = [column for column in self.feature_columns if column not in frame]
        if missing:
            raise ValueError(f"Missing model features: {missing}")
        frame = frame.loc[:, self.feature_columns].astype(float)
        frame = frame.fillna(0.0)
        probabilities = self.model.predict_proba(frame)
        classes = getattr(self.model, "classes_", np.array([0, 1]))
        positive_index = int(np.where(classes == 1)[0][0])
        probability = float(probabilities[0, positive_index])
        return min(1.0, max(0.0, probability))

    @staticmethod
    def bayesian_anchor(
        model_prob: float, market_prob: float, prior_strength: float = 0.3
    ) -> float:
        """Shrink a model probability toward the market-implied prior."""
        if not 0.0 <= prior_strength <= 1.0:
            raise ValueError("prior_strength must be in [0, 1]")
        model_prob = min(1.0, max(0.0, float(model_prob)))
        market_prob = min(1.0, max(0.0, float(market_prob)))
        anchored = (1.0 - prior_strength) * model_prob + prior_strength * market_prob
        return min(1.0, max(0.0, anchored))

    def save(self, path: str | Path) -> Path:
        if self.model is None:
            raise RuntimeError("Cannot save an unfitted model")
        import joblib

        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(destination.name + ".tmp")
        bundle = {
            "version": MODEL_BUNDLE_VERSION,
            "model": self.model,
            "feature_columns": self.feature_columns,
            "metadata": self.metadata,
        }
        joblib.dump(bundle, temporary)
        os.replace(temporary, destination)
        logger.info("Saved model to %s", destination)
        return destination

    def load(self, path: str | Path) -> bool:
        destination = Path(path)
        if not destination.exists():
            return False
        import joblib

        bundle = joblib.load(destination)
        if isinstance(bundle, dict) and "model" in bundle:
            self.model = bundle["model"]
            self.feature_columns = list(bundle.get("feature_columns") or FEATURE_COLUMNS)
            self.metadata = dict(bundle.get("metadata") or {})
        else:
            # Backward-compatible with a bare estimator saved by joblib.
            self.model = bundle
            self.feature_columns = list(FEATURE_COLUMNS)
            self.metadata = {}
        logger.info(
            "Loaded model from %s trained at %s",
            destination,
            self.metadata.get("trained_at", "unknown"),
        )
        return True
