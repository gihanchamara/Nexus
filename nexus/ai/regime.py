"""
Market Regime Detector — Phase 4.

Classifies the current market regime using a two-tier approach:

Tier 1 — Rule-based (always active, zero dependencies):
  Fast, interpretable, predictable fallback.
  Based on: rolling volatility, momentum, RSI thresholds.
  Runs in microseconds on every bar.

Tier 2 — HMM (optional, requires hmmlearn in [ml] dep group):
  Hidden Markov Model with 5 latent states trained on historical feature vectors.
  Better at capturing gradual regime transitions.
  Uses hmmlearn.hmm.GaussianHMM with full covariance.
  Only active after model has been trained (trainer.py) and loaded.

Regime states:
  BULL_TREND    — sustained uptrend, low-moderate vol, momentum positive
  BEAR_TREND    — sustained downtrend, rising vol, momentum negative
  SIDEWAYS_CHOP — low directional momentum, mean-reverting, typical for Iron Condor
  HIGH_VOL      — elevated volatility, large daily swings (earnings season, macro events)
  CRISIS        — extreme vol spike, correlation breakdown, tail-risk regime
  UNKNOWN       — insufficient history (warmup phase)

Published to: ai.regime (on every bar update)

The regime state is the primary input to StrategySelector.
"""

from __future__ import annotations

import json
import logging
import os
import pickle
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

from nexus.ai.features import FeatureSnapshot
from nexus.bus.publisher import publisher

logger = logging.getLogger(__name__)

# Path for persisting trained models
_MODELS_DIR = Path(__file__).parent / "models"

# HMM state-to-regime mapping (determined after training by inspecting state centroids)
# The trainer writes this mapping to the model file.
_DEFAULT_STATE_MAP: dict[int, str] = {
    0: "SIDEWAYS_CHOP",
    1: "BULL_TREND",
    2: "BEAR_TREND",
    3: "HIGH_VOL",
    4: "CRISIS",
}


class RegimeState(str, Enum):
    BULL_TREND    = "BULL_TREND"
    BEAR_TREND    = "BEAR_TREND"
    SIDEWAYS_CHOP = "SIDEWAYS_CHOP"
    HIGH_VOL      = "HIGH_VOL"
    CRISIS        = "CRISIS"
    UNKNOWN       = "UNKNOWN"


@dataclass
class RegimeSignal:
    """Regime classification result published to ai.regime."""
    symbol: str          # 'SPY' or 'MARKET' for index-level regime
    regime: RegimeState
    confidence: float    # 0.0–1.0 (HMM posterior probability; 0.6 for rule-based)
    method: str          # 'rule_based' | 'hmm' | 'xgboost'
    timestamp: datetime

    # Supporting features that drove the decision (for explainability)
    rolling_vol: float = 0.0
    momentum_20: float = 0.0
    rsi_14: float = 50.0
    hmm_state: int = -1  # raw HMM state index (-1 if not used)

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "regime": self.regime.value,
            "confidence": round(self.confidence, 3),
            "method": self.method,
            "timestamp": self.timestamp.isoformat(),
            "rolling_vol": round(self.rolling_vol, 4),
            "momentum_20": round(self.momentum_20, 4),
            "rsi_14": round(self.rsi_14, 2),
            "hmm_state": self.hmm_state,
        }

    @property
    def is_premium_selling_regime(self) -> bool:
        """True when regime is suitable for selling options premium."""
        return self.regime in (RegimeState.SIDEWAYS_CHOP, RegimeState.HIGH_VOL)

    @property
    def is_directional_regime(self) -> bool:
        """True when regime supports directional (trend-following) strategies."""
        return self.regime in (RegimeState.BULL_TREND, RegimeState.BEAR_TREND)

    @property
    def is_defensive_regime(self) -> bool:
        """True when regime calls for reduced position size / defensive posture."""
        return self.regime == RegimeState.CRISIS


class RegimeDetector:
    """
    Two-tier market regime classifier.

    Always starts with rule-based detection (Tier 1).
    Upgrades to HMM (Tier 2) after load_model() is called.

    Maintains regime history for dashboard and StrategySelector context.
    """

    def __init__(self) -> None:
        self._hmm_model: Any = None           # GaussianHMM instance (optional)
        self._hmm_state_map: dict[int, str] = dict(_DEFAULT_STATE_MAP)
        self._xgb_model: Any = None           # XGBClassifier instance (optional)
        self._latest: dict[str, RegimeSignal] = {}
        self._hmm_available = False

    # ─── Public API ───────────────────────────────────────────────────────────

    async def update(self, features: FeatureSnapshot) -> RegimeSignal:
        """
        Classify regime from the latest FeatureSnapshot and publish to ai.regime.
        Call this after FeatureStore.update() on every bar.
        """
        if not features.is_warmed_up:
            signal = RegimeSignal(
                symbol=features.symbol,
                regime=RegimeState.UNKNOWN,
                confidence=0.0,
                method="warmup",
                timestamp=features.timestamp,
            )
            self._latest[features.symbol] = signal
            return signal

        # Try HMM first; fall back to rule-based
        if self._hmm_available and self._hmm_model is not None:
            signal = self._hmm_predict(features)
        elif self._xgb_model is not None:
            signal = self._xgb_predict(features)
        else:
            signal = self._rule_based_predict(features)

        self._latest[features.symbol] = signal
        await self._emit(signal)
        return signal

    def get(self, symbol: str) -> RegimeSignal | None:
        return self._latest.get(symbol)

    def get_regime(self, symbol: str) -> RegimeState:
        sig = self._latest.get(symbol)
        return sig.regime if sig else RegimeState.UNKNOWN

    def snapshot(self) -> dict[str, Any]:
        return {sym: sig.to_dict() for sym, sig in self._latest.items()}

    # ─── Model loading ────────────────────────────────────────────────────────

    def load_model(self, path: str | Path | None = None) -> None:
        """
        Load a pre-trained HMM + optional XGBoost model from disk.
        Called once at startup after training is complete.
        Safe to call even if the file doesn't exist — falls back to rule-based.
        """
        try:
            import hmmlearn  # noqa: F401
            self._hmm_available = True
        except ImportError:
            logger.info("hmmlearn not installed — using rule-based regime detection")
            return

        model_path = Path(path) if path else _MODELS_DIR / "regime_hmm.pkl"
        if not model_path.exists():
            logger.info("No HMM model found at %s — using rule-based regime detection", model_path)
            return

        try:
            with open(model_path, "rb") as f:
                bundle = pickle.load(f)
            self._hmm_model = bundle["hmm"]
            self._hmm_state_map = bundle.get("state_map", _DEFAULT_STATE_MAP)
            logger.info(
                "Loaded HMM regime model from %s (n_components=%d)",
                model_path, self._hmm_model.n_components,
            )
        except Exception as exc:
            logger.warning("Failed to load HMM model: %s — using rule-based", exc)

        # Optionally load XGBoost companion classifier
        xgb_path = model_path.with_name("regime_xgb.pkl")
        if xgb_path.exists():
            try:
                with open(xgb_path, "rb") as f:
                    self._xgb_model = pickle.load(f)
                logger.info("Loaded XGBoost regime classifier from %s", xgb_path)
            except Exception as exc:
                logger.warning("Failed to load XGBoost model: %s", exc)

    # ─── Tier 1: Rule-based detection ────────────────────────────────────────

    def _rule_based_predict(self, features: FeatureSnapshot) -> RegimeSignal:
        """
        Fast heuristic regime classification from feature thresholds.

        Rules (priority-ordered, first match wins):
          vol > 0.50 → CRISIS        (VIX > ~50 equivalent)
          vol > 0.30 → HIGH_VOL      (VIX > ~30 equivalent)
          momentum_20 > 0.05 and RSI > 55 → BULL_TREND
          momentum_20 < -0.05 and RSI < 45 → BEAR_TREND
          default → SIDEWAYS_CHOP
        """
        vol = features.rolling_vol_20
        mom = features.momentum_20
        rsi = features.rsi_14

        if vol > 0.50:
            regime = RegimeState.CRISIS
        elif vol > 0.30:
            regime = RegimeState.HIGH_VOL
        elif mom > 0.05 and rsi > 55:
            regime = RegimeState.BULL_TREND
        elif mom < -0.05 and rsi < 45:
            regime = RegimeState.BEAR_TREND
        else:
            regime = RegimeState.SIDEWAYS_CHOP

        return RegimeSignal(
            symbol=features.symbol,
            regime=regime,
            confidence=0.60,   # rule-based always has moderate confidence
            method="rule_based",
            timestamp=features.timestamp,
            rolling_vol=vol,
            momentum_20=mom,
            rsi_14=rsi,
        )

    # ─── Tier 2: HMM detection ────────────────────────────────────────────────

    def _hmm_predict(self, features: FeatureSnapshot) -> RegimeSignal:
        """
        HMM regime prediction using posterior state probabilities.
        Requires a trained GaussianHMM model loaded via load_model().
        """
        try:
            import numpy as np
            obs = np.array([features.to_vector()], dtype=float)
            state_seq = self._hmm_model.predict(obs)
            posterior = self._hmm_model.predict_proba(obs)

            hmm_state = int(state_seq[0])
            confidence = float(posterior[0][hmm_state])
            regime_str = self._hmm_state_map.get(hmm_state, "SIDEWAYS_CHOP")
            regime = RegimeState(regime_str)

            return RegimeSignal(
                symbol=features.symbol,
                regime=regime,
                confidence=confidence,
                method="hmm",
                timestamp=features.timestamp,
                rolling_vol=features.rolling_vol_20,
                momentum_20=features.momentum_20,
                rsi_14=features.rsi_14,
                hmm_state=hmm_state,
            )
        except Exception as exc:
            logger.warning("HMM prediction failed: %s — falling back to rule-based", exc)
            return self._rule_based_predict(features)

    # ─── Tier 2b: XGBoost detection ──────────────────────────────────────────

    def _xgb_predict(self, features: FeatureSnapshot) -> RegimeSignal:
        """
        XGBoost classifier regime prediction.
        Higher accuracy than rules for labelled historical data.
        """
        try:
            import numpy as np
            obs = np.array([features.to_vector()], dtype=float)
            state = int(self._xgb_model.predict(obs)[0])
            proba = self._xgb_model.predict_proba(obs)[0]
            confidence = float(proba[state])
            regime_str = self._hmm_state_map.get(state, "SIDEWAYS_CHOP")
            regime = RegimeState(regime_str)

            return RegimeSignal(
                symbol=features.symbol,
                regime=regime,
                confidence=confidence,
                method="xgboost",
                timestamp=features.timestamp,
                rolling_vol=features.rolling_vol_20,
                momentum_20=features.momentum_20,
                rsi_14=features.rsi_14,
                hmm_state=state,
            )
        except Exception as exc:
            logger.warning("XGBoost prediction failed: %s — falling back to rule-based", exc)
            return self._rule_based_predict(features)

    # ─── Emit ─────────────────────────────────────────────────────────────────

    async def _emit(self, signal: RegimeSignal) -> None:
        try:
            await publisher.publish("ai.regime", signal.to_dict())
        except Exception:
            pass


# Module-level singleton
regime_detector = RegimeDetector()
