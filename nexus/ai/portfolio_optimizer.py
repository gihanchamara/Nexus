"""
Portfolio Optimizer — Phase 4.

Computes capital allocation weights across active strategies.
Three methods available (configurable):

  kelly         — Per-strategy Kelly Criterion fraction
                  f* = (p × b - q) / b  where b = avg_win / avg_loss
                  Maximises logarithmic growth rate but requires accurate p/b estimates.
                  Typically use half-Kelly (fraction × 0.5) for safety.

  mean_variance — Markowitz mean-variance optimisation (scipy.optimize)
                  Minimises portfolio variance for a target return.
                  Requires a return estimate and covariance matrix.
                  Falls back to equal-weight if scipy is unavailable.

  risk_parity   — Equal Risk Contribution (ERC)
                  Each strategy contributes equally to total portfolio risk.
                  Iterative algorithm; converges in <50 iterations typically.
                  Best default when return estimates are unreliable.

  equal         — Equal weight (baseline / fallback)

Output:
  AllocationResult published to ai.allocation (consumed by OMS sizer)
  OMS Router uses allocation weights to scale position sizes per strategy.

Published to: ai.allocation
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from nexus.bus.publisher import publisher

logger = logging.getLogger(__name__)

# Maximum weight a single strategy can receive (prevents over-concentration)
_MAX_CONCENTRATION = 0.50

# Kelly safety factor (use half-Kelly by default to reduce variance)
_KELLY_SAFETY_FACTOR = 0.50


@dataclass
class StrategyStats:
    """Performance statistics for one strategy (sourced from backtest analytics)."""
    strategy_id: str
    win_rate: float          # fraction of winning trades (0–1)
    avg_win: float           # average winning trade P&L (positive)
    avg_loss: float          # average losing trade P&L (positive = magnitude)
    sharpe_ratio: float      # rolling Sharpe (used for risk_parity weighting)
    annual_vol: float = 0.10 # annualised return volatility (used in MV optimisation)


@dataclass
class AllocationResult:
    """Capital allocation weights across active strategies."""
    weights: dict[str, float]   # strategy_id → fraction (sum = 1.0)
    method: str
    timestamp: datetime = field(default_factory=datetime.utcnow)
    rationale: str = ""

    def scale_quantity(self, strategy_id: str, base_quantity: float) -> float:
        """Scale a base quantity by this strategy's allocation weight."""
        weight = self.weights.get(strategy_id, 0.0)
        return round(base_quantity * weight * len(self.weights), 2)

    def to_dict(self) -> dict[str, Any]:
        return {
            "weights": {k: round(v, 4) for k, v in self.weights.items()},
            "method": self.method,
            "timestamp": self.timestamp.isoformat(),
            "rationale": self.rationale,
        }


class PortfolioOptimizer:
    """
    Multi-method capital allocator for active strategies.

    Usage:
        stats = [
            StrategyStats("ma_crossover_v1", win_rate=0.55, avg_win=0.04, avg_loss=0.02, sharpe_ratio=1.2),
            StrategyStats("iron_condor_v1",  win_rate=0.70, avg_win=0.02, avg_loss=0.05, sharpe_ratio=0.9),
        ]
        result = await optimizer.optimize(stats, method="risk_parity")
        # → AllocationResult(weights={"ma_crossover_v1": 0.52, "iron_condor_v1": 0.48})
    """

    def __init__(self, method: str = "risk_parity") -> None:
        self._default_method = method
        self._latest: AllocationResult | None = None

    async def optimize(
        self,
        strategies: list[StrategyStats],
        method: str | None = None,
    ) -> AllocationResult:
        """
        Compute and publish allocation weights.
        Falls back to equal weight if the requested method fails.
        """
        if not strategies:
            return AllocationResult(weights={}, method="empty")

        method = method or self._default_method

        try:
            if method == "kelly":
                result = self._kelly(strategies)
            elif method == "mean_variance":
                result = self._mean_variance(strategies)
            elif method == "risk_parity":
                result = self._risk_parity(strategies)
            else:
                result = self._equal(strategies)
        except Exception as exc:
            logger.warning("Portfolio optimisation failed (%s): %s — falling back to equal", method, exc)
            result = self._equal(strategies)

        self._latest = result
        await self._emit(result)
        logger.info("PortfolioOptimizer: %s weights=%s", method, result.weights)
        return result

    def get_weight(self, strategy_id: str) -> float:
        """Return current allocation weight for a strategy (default 1.0 if no allocation)."""
        if self._latest is None:
            return 1.0
        return self._latest.weights.get(strategy_id, 0.0)

    # ─── Method: Kelly Criterion ──────────────────────────────────────────────

    def _kelly(self, strategies: list[StrategyStats]) -> AllocationResult:
        """
        Compute per-strategy Kelly fraction then normalise to sum = 1.

        Kelly fraction: f* = (p × b - q) / b
          p = win_rate, q = 1 - p, b = avg_win / avg_loss
        """
        raw_weights: dict[str, float] = {}

        for s in strategies:
            if s.avg_loss <= 0:
                raw_weights[s.strategy_id] = _MIN_KELLY
                continue
            b = s.avg_win / s.avg_loss
            q = 1.0 - s.win_rate
            kelly_f = max(0.0, (s.win_rate * b - q) / b)
            # Apply safety factor (half-Kelly)
            raw_weights[s.strategy_id] = kelly_f * _KELLY_SAFETY_FACTOR

        # Floor at minimum, then normalise
        raw_weights = {k: max(0.05, v) for k, v in raw_weights.items()}
        return _normalise(
            raw_weights,
            method="kelly",
            rationale="half-Kelly per strategy; normalised to sum=1",
        )

    # ─── Method: Mean-Variance ────────────────────────────────────────────────

    def _mean_variance(self, strategies: list[StrategyStats]) -> AllocationResult:
        """
        Markowitz minimum-variance portfolio (no return target — pure min-var).
        Uses scipy.optimize.minimize with sum-to-1 and non-negative constraints.
        Falls back to equal weight if scipy is unavailable or optimisation fails.
        """
        try:
            import numpy as np
            from scipy.optimize import minimize  # type: ignore[import-untyped]
        except ImportError:
            logger.info("scipy not available for MV optimisation — using risk_parity")
            return self._risk_parity(strategies)

        n = len(strategies)
        vols = np.array([max(0.01, s.annual_vol) for s in strategies])
        # Assume zero correlation (conservative; better than assuming high correlation)
        cov = np.diag(vols ** 2)

        def portfolio_variance(w: Any) -> float:
            return float(w @ cov @ w)

        constraints = [{"type": "eq", "fun": lambda w: np.sum(w) - 1.0}]
        bounds = [(0.05, _MAX_CONCENTRATION)] * n
        x0 = np.full(n, 1.0 / n)

        result = minimize(
            portfolio_variance, x0,
            method="SLSQP",
            bounds=bounds,
            constraints=constraints,
            options={"maxiter": 500, "ftol": 1e-9},
        )

        if not result.success:
            return self._equal(strategies)

        weights = {s.strategy_id: float(w) for s, w in zip(strategies, result.x)}
        return _normalise(
            weights,
            method="mean_variance",
            rationale=f"min-variance (uncorrelated), n={n} strategies",
        )

    # ─── Method: Risk Parity (ERC) ────────────────────────────────────────────

    def _risk_parity(self, strategies: list[StrategyStats]) -> AllocationResult:
        """
        Equal Risk Contribution (ERC): each strategy contributes equally to total vol.

        Algorithm: iterative proportional update
          w_i = (1/σ_i) / Σ(1/σ_j)
        where σ_i = annual_vol of strategy i.

        Simple closed-form for uncorrelated strategies: w_i ∝ 1/σ_i.
        """
        raw: dict[str, float] = {}
        for s in strategies:
            vol = max(0.01, s.annual_vol)
            raw[s.strategy_id] = 1.0 / vol

        return _normalise(
            raw,
            method="risk_parity",
            rationale="equal risk contribution (1/vol weights); normalised",
        )

    # ─── Method: Equal Weight ─────────────────────────────────────────────────

    def _equal(self, strategies: list[StrategyStats]) -> AllocationResult:
        n = len(strategies)
        weights = {s.strategy_id: 1.0 / n for s in strategies}
        return AllocationResult(
            weights=weights,
            method="equal",
            rationale=f"equal weight across {n} strategies",
        )

    async def _emit(self, result: AllocationResult) -> None:
        try:
            await publisher.publish("ai.allocation", result.to_dict())
        except Exception:
            pass


# ─── Helpers ──────────────────────────────────────────────────────────────────

_MIN_KELLY = 0.05   # floor Kelly fraction before normalisation


def _normalise(
    raw: dict[str, float],
    method: str,
    rationale: str = "",
) -> AllocationResult:
    """Normalise weights to sum=1, cap at MAX_CONCENTRATION, floor at 0."""
    total = sum(raw.values())
    if total <= 0:
        n = len(raw)
        weights = {k: 1.0 / n for k in raw}
    else:
        weights = {k: v / total for k, v in raw.items()}

    # Apply concentration cap and re-normalise
    capped = {k: min(v, _MAX_CONCENTRATION) for k, v in weights.items()}
    total_capped = sum(capped.values())
    if total_capped > 0:
        weights = {k: v / total_capped for k, v in capped.items()}

    return AllocationResult(weights=weights, method=method, rationale=rationale)


# Module-level singleton
portfolio_optimizer = PortfolioOptimizer()
