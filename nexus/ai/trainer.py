"""
Model Trainer — Phase 4.

Offline training pipeline for Nexus AI models.
This module is NEVER called in the hot path — it runs as a one-time or
scheduled batch job to (re)train models on historical data.

Models trained:
  1. RegimeDetector HMM  — unsupervised 5-state Gaussian HMM on feature vectors
  2. RegimeDetector XGBoost — supervised classifier using HMM-labelled data
     (semi-supervised: HMM labels history, XGB learns to replicate with more features)
  3. SignalEnsemble weights — rolling Sharpe ratios from backtest analytics

Training data sources:
  - TimescaleDB market_bars (historical OHLCV)
  - nexus.ai.features.FeatureStore (computes features from bars)
  - nexus.backtest results (for ensemble weight calculation)

Model storage:
  - Files: nexus/ai/models/  (regime_hmm.pkl, regime_xgb.pkl)
  - DB: model_registry MySQL table (version, path, metrics — already in Phase 1 schema)

Anti-lookahead guarantee:
  Features are computed in strict forward-only order using FeatureStore.update().
  No future data leaks into any feature vector.

Usage:
    # Train regime model on 2 years of SPY daily bars
    trainer = ModelTrainer()
    bars = await DataLoader.from_timescaledb("SPY", "1d", start, end)
    result = await trainer.train_regime_model(bars, symbol="SPY")
    print(result)  # {'hmm_aic': ..., 'n_states': 5, 'model_path': ...}

    # Train ensemble weights from backtest runs
    weights = trainer.compute_ensemble_weights(backtest_results)
"""

from __future__ import annotations

import logging
import os
import pickle
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from nexus.ai.features import FeatureStore, FeatureSnapshot
from nexus.ai.regime import RegimeState, _DEFAULT_STATE_MAP
from nexus.core.schemas import BarData

logger = logging.getLogger(__name__)

_MODELS_DIR = Path(__file__).parent / "models"


@dataclass
class TrainingResult:
    """Summary of a completed model training run."""
    model_name: str
    model_path: str
    n_samples: int
    metrics: dict[str, Any]
    trained_at: datetime
    duration_seconds: float

    def __str__(self) -> str:
        return (
            f"TrainingResult({self.model_name}): "
            f"samples={self.n_samples}, "
            f"metrics={self.metrics}, "
            f"path={self.model_path}"
        )


class ModelTrainer:
    """
    Trains and persists Nexus AI models.

    All training methods are async to allow calling from an async application.
    CPU-intensive work (hmmlearn, xgboost fitting) runs in a thread pool
    to avoid blocking the event loop.
    """

    def __init__(self, models_dir: Path | str | None = None) -> None:
        self._models_dir = Path(models_dir) if models_dir else _MODELS_DIR
        self._models_dir.mkdir(parents=True, exist_ok=True)

    # ─── Regime model training ────────────────────────────────────────────────

    async def train_regime_model(
        self,
        bars: list[BarData],
        symbol: str = "SPY",
        n_states: int = 5,
        n_iter: int = 100,
        random_state: int = 42,
    ) -> TrainingResult:
        """
        Train a GaussianHMM regime model on historical bars.

        Step 1: Compute feature vectors from bars (strict forward-only order)
        Step 2: Fit GaussianHMM (unsupervised — no labels required)
        Step 3: Analyse state centroids to assign regime labels
        Step 4: Train XGBoost classifier on HMM-labelled data (more features)
        Step 5: Save model bundle to disk

        Args:
            bars        : historical BarData (must be sorted chronologically)
            symbol      : symbol name (used for logging)
            n_states    : number of hidden states (default: 5 matches RegimeState count)
            n_iter      : max EM iterations for HMM training
            random_state: reproducibility seed

        Returns:
            TrainingResult with model path and quality metrics
        """
        import asyncio
        import time

        t_start = time.monotonic()
        logger.info("Training regime model on %d bars for %s (n_states=%d)", len(bars), symbol, n_states)

        # Step 1: Build feature matrix (forward-only)
        store = FeatureStore(publish=False)
        feature_snapshots: list[FeatureSnapshot] = []

        for bar in bars:
            snap = await store.update(bar)
            if snap.is_warmed_up:
                feature_snapshots.append(snap)

        if len(feature_snapshots) < n_states * 10:
            raise ValueError(
                f"Insufficient training data: {len(feature_snapshots)} warmed-up bars. "
                f"Need at least {n_states * 10}."
            )

        # Step 2: Build numpy feature matrix
        try:
            import numpy as np
        except ImportError:
            raise ImportError("numpy required for regime model training")

        X = np.array([snap.to_vector() for snap in feature_snapshots], dtype=float)

        # Replace any NaN/Inf with 0
        X = np.where(np.isfinite(X), X, 0.0)

        # Step 3: Fit HMM
        hmm_model, state_map, hmm_metrics = await asyncio.get_event_loop().run_in_executor(
            None, _fit_hmm, X, n_states, n_iter, random_state
        )

        # Step 4: Train XGBoost on HMM-labelled data
        xgb_model = await asyncio.get_event_loop().run_in_executor(
            None, _fit_xgboost, X, hmm_model, random_state
        )

        # Step 5: Save
        hmm_path = self._models_dir / "regime_hmm.pkl"
        xgb_path = self._models_dir / "regime_xgb.pkl"

        bundle = {
            "hmm": hmm_model,
            "state_map": state_map,
            "feature_names": [
                "log_return", "rolling_vol_20", "rolling_vol_60",
                "momentum_5", "momentum_20", "momentum_60",
                "rsi_14_norm", "macd_signal", "bollinger_pct", "atr_14", "volume_ratio_20",
            ],
            "n_samples": len(X),
            "trained_at": datetime.utcnow().isoformat(),
            "symbol": symbol,
        }

        with open(hmm_path, "wb") as f:
            pickle.dump(bundle, f, protocol=pickle.HIGHEST_PROTOCOL)
        logger.info("HMM model saved to %s", hmm_path)

        if xgb_model is not None:
            with open(xgb_path, "wb") as f:
                pickle.dump(xgb_model, f, protocol=pickle.HIGHEST_PROTOCOL)
            logger.info("XGBoost model saved to %s", xgb_path)

        t_end = time.monotonic()

        result = TrainingResult(
            model_name="regime_hmm",
            model_path=str(hmm_path),
            n_samples=len(X),
            metrics={**hmm_metrics, "state_map": state_map},
            trained_at=datetime.utcnow(),
            duration_seconds=t_end - t_start,
        )
        logger.info(str(result))
        return result

    # ─── Ensemble weight calculation ──────────────────────────────────────────

    def compute_ensemble_weights(
        self,
        backtest_results: dict[str, Any],
    ) -> dict[str, float]:
        """
        Compute SignalEnsemble weights from backtest PerformanceMetrics.

        Args:
            backtest_results: dict of {strategy_id: PerformanceMetrics}
                              (the .metrics field from BacktestResult)

        Returns:
            dict of {strategy_id: sharpe_ratio} for use with SignalEnsemble.set_weight()
        """
        weights: dict[str, float] = {}
        for strategy_id, metrics in backtest_results.items():
            sharpe = getattr(metrics, "sharpe_ratio", 0.0)
            weights[strategy_id] = max(0.1, sharpe)  # floor at 0.1

        logger.info("Computed ensemble weights: %s", weights)
        return weights

    # ─── Feature matrix export ────────────────────────────────────────────────

    async def export_feature_matrix(
        self,
        bars: list[BarData],
        output_path: str | Path | None = None,
    ) -> Any:
        """
        Build and export the feature matrix for a bar series.
        Useful for offline analysis, debugging, and custom model training.

        Returns a numpy array (n_samples × n_features) or saves to Parquet if path given.
        """
        try:
            import numpy as np
        except ImportError:
            raise ImportError("numpy required for feature matrix export")

        store = FeatureStore(publish=False)
        snapshots: list[FeatureSnapshot] = []
        for bar in bars:
            snap = await store.update(bar)
            if snap.is_warmed_up:
                snapshots.append(snap)

        X = np.array([s.to_vector() for s in snapshots], dtype=float)

        if output_path:
            try:
                import pandas as pd  # type: ignore[import-untyped]
                cols = [
                    "log_return", "rolling_vol_20", "rolling_vol_60",
                    "momentum_5", "momentum_20", "momentum_60",
                    "rsi_14_norm", "macd_signal", "bollinger_pct", "atr_14", "volume_ratio_20",
                ]
                df = pd.DataFrame(X, columns=cols)
                df["timestamp"] = [s.timestamp for s in snapshots]
                df["symbol"] = [s.symbol for s in snapshots]
                df.to_parquet(output_path, index=False)
                logger.info("Feature matrix exported: %s rows → %s", len(df), output_path)
            except ImportError:
                logger.warning("pandas not available — skipping Parquet export")

        return X


# ─── HMM fitting (runs in thread pool) ───────────────────────────────────────

def _fit_hmm(
    X: Any,
    n_states: int,
    n_iter: int,
    random_state: int,
) -> tuple[Any, dict[int, str], dict[str, Any]]:
    """
    Fit a GaussianHMM and return (model, state_map, metrics).
    Runs synchronously — call via run_in_executor to avoid blocking.
    """
    try:
        from hmmlearn.hmm import GaussianHMM  # type: ignore[import-untyped]
        import numpy as np
    except ImportError:
        raise ImportError("hmmlearn required for HMM training: pip install hmmlearn")

    model = GaussianHMM(
        n_components=n_states,
        covariance_type="full",
        n_iter=n_iter,
        random_state=random_state,
        verbose=False,
    )
    model.fit(X)
    log_likelihood = model.score(X)
    n_params = n_states ** 2 + 2 * n_states * X.shape[1]  # rough param count
    aic = -2 * log_likelihood * len(X) + 2 * n_params

    # Assign regime labels based on feature centroids
    state_map = _label_states_from_centroids(model.means_, n_states)

    metrics = {
        "log_likelihood": round(float(log_likelihood), 4),
        "aic": round(float(aic), 2),
        "n_states": n_states,
        "converged": model.monitor_.converged,
        "n_iter_done": model.monitor_.iter,
    }
    logger.info("HMM fit complete: AIC=%.2f, converged=%s", aic, model.monitor_.converged)
    return model, state_map, metrics


def _label_states_from_centroids(
    means: Any,
    n_states: int,
) -> dict[int, str]:
    """
    Heuristically assign regime labels to HMM states by inspecting centroids.

    Feature indices in the feature vector (from FeatureSnapshot.to_vector()):
      0: log_return
      1: rolling_vol_20   ← volatility level
      2: rolling_vol_60
      3: momentum_5
      4: momentum_20      ← trend direction
      5: momentum_60
      6: rsi_14_norm      ← momentum
      7: macd_signal
      8: bollinger_pct
      9: atr_14
      10: volume_ratio_20
    """
    import numpy as np

    state_map: dict[int, str] = {}
    used_regimes: set[str] = set()

    vol_idx = 1     # rolling_vol_20
    mom_idx = 4     # momentum_20
    rsi_idx = 6     # rsi_14_norm

    for state_i in range(n_states):
        centroid = means[state_i]
        vol = centroid[vol_idx]
        mom = centroid[mom_idx]
        rsi = centroid[rsi_idx]

        if vol > 0.40:
            regime = "CRISIS"
        elif vol > 0.25:
            regime = "HIGH_VOL"
        elif mom > 0.03 and rsi > 0.55:
            regime = "BULL_TREND"
        elif mom < -0.03 and rsi < 0.45:
            regime = "BEAR_TREND"
        else:
            regime = "SIDEWAYS_CHOP"

        # Avoid duplicates by appending index if already used
        if regime in used_regimes:
            # Assign to least-used remaining regime
            fallbacks = ["SIDEWAYS_CHOP", "BULL_TREND", "BEAR_TREND", "HIGH_VOL", "CRISIS"]
            for fb in fallbacks:
                if fb not in used_regimes:
                    regime = fb
                    break

        state_map[state_i] = regime
        used_regimes.add(regime)

    return state_map


def _fit_xgboost(
    X: Any,
    hmm_model: Any,
    random_state: int,
) -> Any | None:
    """
    Train an XGBoost classifier using HMM-predicted states as labels.
    Semi-supervised: HMM provides pseudo-labels, XGB learns richer boundaries.
    Returns None if xgboost is not installed.
    """
    try:
        from xgboost import XGBClassifier  # type: ignore[import-untyped]
    except ImportError:
        logger.info("xgboost not installed — skipping XGBoost companion classifier")
        return None

    labels = hmm_model.predict(X)

    clf = XGBClassifier(
        n_estimators=200,
        max_depth=4,
        learning_rate=0.05,
        subsample=0.8,
        colsample_bytree=0.8,
        random_state=random_state,
        eval_metric="mlogloss",
        verbosity=0,
    )
    clf.fit(X, labels)

    train_acc = (clf.predict(X) == labels).mean()
    logger.info("XGBoost classifier trained: train_accuracy=%.3f", train_acc)
    return clf
