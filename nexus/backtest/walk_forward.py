"""
Walk-Forward Optimizer — Phase 3.

Prevents in-sample overfitting by evaluating strategy parameters on
unseen (out-of-sample) data. The gold standard for strategy validation.

Method:
  1. Split data into rolling windows: [train_start, train_end] → [test_start, test_end]
  2. Grid search over param_grid on the training window
  3. Apply best params to test window → OOS BacktestResult
  4. Advance window by step_size
  5. Aggregate all OOS results → combined metrics + stability report

Parameter stability score:
  Measures how consistent optimal parameters are across windows.
  High variance in optimal params = fragile strategy (overfit to specific regime).
  Low variance = robust strategy (params work across market conditions).

Output:
  WalkForwardResult:
    windows         — list of per-window results (train + OOS metrics)
    oos_metrics     — combined OOS PerformanceMetrics
    best_params     — most frequently selected params across all windows
    param_stability — per-param coefficient of variation (lower = more stable)

Usage:
    optimizer = WalkForwardOptimizer()
    result = await optimizer.run(
        strategy_class=MovingAverageCrossover,
        all_bars=bars,
        param_grid={"fast_period": [5, 10, 20], "slow_period": [30, 50, 100]},
        wf_config=WalkForwardConfig(train_pct=0.7, step_pct=0.1),
    )
    print(result.summary())
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from itertools import product
from typing import Any, Type

from nexus.backtest.analytics import PerformanceMetrics, compute_metrics
from nexus.backtest.engine import BacktestConfig, BacktestEngine, BacktestResult
from nexus.backtest.fill_simulator import FillMethod
from nexus.core.schemas import BarData, OptionsChainSnapshot
from nexus.strategy.base import BaseStrategy

logger = logging.getLogger(__name__)


# ─── Configuration ────────────────────────────────────────────────────────────

@dataclass
class WalkForwardConfig:
    """
    Walk-Forward Optimizer configuration.

    train_pct     : fraction of each window used for training (default 0.70 = 70%)
    step_pct      : how far the window advances each iteration (default 0.10 = 10%)
    min_trades    : discard windows with fewer than this many OOS trades
                    (avoid statistics on too-small samples)
    optimize_on   : metric to maximise during grid search (default 'sharpe_ratio')
                    Valid: 'sharpe_ratio' | 'sortino_ratio' | 'calmar_ratio' |
                           'total_return' | 'profit_factor'
    initial_capital  : starting capital for each sub-backtest
    fill_method      : fill simulation method
    risk_free_rate   : for Sharpe/Sortino computation
    max_parallel     : max concurrent backtests (grid search parallelism)
    """
    train_pct: float = 0.70
    step_pct: float = 0.10
    min_trades: int = 5
    optimize_on: str = "sharpe_ratio"
    initial_capital: float = 100_000.0
    fill_method: FillMethod = FillMethod.NEXT_BAR_OPEN
    risk_free_rate: float = 0.05
    max_parallel: int = 8           # max concurrent asyncio tasks for grid search


# ─── Per-window result ────────────────────────────────────────────────────────

@dataclass
class WindowResult:
    """Result for a single train/test walk-forward window."""
    window_index: int
    train_start: datetime
    train_end: datetime
    test_start: datetime
    test_end: datetime
    train_bars: int
    test_bars: int
    best_params: dict[str, Any]
    best_is_score: float         # in-sample score of best params
    oos_result: BacktestResult   # out-of-sample backtest with best params
    is_valid: bool               # True if OOS had enough trades


# ─── Walk-Forward Result ──────────────────────────────────────────────────────

@dataclass
class WalkForwardResult:
    """
    Aggregated result of a complete walk-forward optimization.
    """
    config: WalkForwardConfig
    param_grid: dict[str, list[Any]]
    windows: list[WindowResult]
    oos_metrics: PerformanceMetrics          # combined OOS equity curve metrics
    best_params: dict[str, Any]             # most-selected params across windows
    param_stability: dict[str, float]       # per-param CV (lower = more stable)
    oos_equity_curve: list[tuple[datetime, float]]
    oos_trades: list[Any]                   # all OOS TradeRecord objects

    def summary(self) -> dict[str, Any]:
        m = self.oos_metrics
        return {
            "windows_run": len(self.windows),
            "windows_valid": sum(1 for w in self.windows if w.is_valid),
            "oos_total_return_pct": round(m.total_return * 100, 2),
            "oos_sharpe": round(m.sharpe_ratio, 3),
            "oos_sortino": round(m.sortino_ratio, 3),
            "oos_max_drawdown_pct": round(m.max_drawdown * 100, 2),
            "oos_win_rate_pct": round(m.win_rate * 100, 1),
            "oos_profit_factor": round(m.profit_factor, 3),
            "oos_total_trades": m.total_trades,
            "best_params": self.best_params,
            "param_stability": {k: round(v, 3) for k, v in self.param_stability.items()},
        }


# ─── Walk-Forward Optimizer ───────────────────────────────────────────────────

class WalkForwardOptimizer:
    """
    Rolling walk-forward optimizer for BaseStrategy subclasses.

    Key design:
      - Each grid search IS parallel (asyncio.gather with semaphore)
      - Windows are processed sequentially (to maintain temporal order in OOS curve)
      - BacktestEngine is reused across runs (stateless)
    """

    def __init__(self) -> None:
        self._engine = BacktestEngine()

    async def run(
        self,
        strategy_class: Type[BaseStrategy],
        all_bars: list[BarData],
        param_grid: dict[str, list[Any]],
        wf_config: WalkForwardConfig | None = None,
        base_params: dict[str, Any] | None = None,
        options_chains: list[OptionsChainSnapshot] | None = None,
    ) -> WalkForwardResult:
        """
        Run a complete walk-forward optimization.

        Args:
            strategy_class  : BaseStrategy subclass to optimize
            all_bars        : full historical bar dataset (sorted chronological)
            param_grid      : parameters to search over, e.g.:
                              {"fast_period": [5, 10, 20], "slow_period": [30, 50]}
                              All combinations are evaluated (grid search).
            wf_config       : WalkForwardConfig (defaults to 70/30 split, 10% step)
            base_params     : fixed parameters not in the grid (merged with grid params)
            options_chains  : optional pre-built options chains for options strategies

        Returns:
            WalkForwardResult with aggregated OOS metrics and stability analysis
        """
        if wf_config is None:
            wf_config = WalkForwardConfig()

        if not all_bars:
            raise ValueError("all_bars cannot be empty")

        base_params = base_params or {}
        options_chains = options_chains or []

        # Generate all windows
        windows_spec = self._generate_windows(all_bars, wf_config)
        if not windows_spec:
            raise ValueError(
                f"Not enough bars ({len(all_bars)}) to form any walk-forward windows. "
                f"Need at least {int(1.0 / wf_config.step_pct)} bars."
            )

        logger.info(
            "[WalkForward] Starting: %d windows, grid=%s, optimize_on=%s",
            len(windows_spec), {k: len(v) for k, v in param_grid.items()},
            wf_config.optimize_on,
        )

        window_results: list[WindowResult] = []
        all_oos_curve: list[tuple[datetime, float]] = []
        all_oos_trades: list[Any] = []
        selected_params_history: list[dict[str, Any]] = []

        for i, (train_bars, test_bars) in enumerate(windows_spec):
            logger.info(
                "[WalkForward] Window %d/%d: train=%d bars, test=%d bars",
                i + 1, len(windows_spec), len(train_bars), len(test_bars),
            )

            # ── Grid search on training window ────────────────────────────────
            best_params, best_is_score = await self._grid_search(
                strategy_class=strategy_class,
                bars=train_bars,
                param_grid=param_grid,
                base_params=base_params,
                wf_config=wf_config,
                options_chains=self._filter_chains(options_chains, train_bars),
            )

            # ── OOS evaluation with best params ───────────────────────────────
            merged_params = {**base_params, **best_params}
            oos_config = BacktestConfig(
                strategy_class=strategy_class,
                strategy_params=merged_params,
                bars=test_bars,
                initial_capital=wf_config.initial_capital,
                fill_method=wf_config.fill_method,
                risk_free_rate=wf_config.risk_free_rate,
                options_chains=self._filter_chains(options_chains, test_bars),
                strategy_id=f"wf_window_{i}",
            )
            oos_result = await self._engine.run(oos_config)

            is_valid = oos_result.metrics.total_trades >= wf_config.min_trades

            window_result = WindowResult(
                window_index=i,
                train_start=train_bars[0].timestamp,
                train_end=train_bars[-1].timestamp,
                test_start=test_bars[0].timestamp,
                test_end=test_bars[-1].timestamp,
                train_bars=len(train_bars),
                test_bars=len(test_bars),
                best_params=best_params,
                best_is_score=best_is_score,
                oos_result=oos_result,
                is_valid=is_valid,
            )
            window_results.append(window_result)

            if is_valid:
                selected_params_history.append(best_params)
                all_oos_curve.extend(oos_result.equity_curve)
                all_oos_trades.extend(oos_result.trades)

            logger.info(
                "[WalkForward] Window %d: best=%s IS_score=%.3f OOS_trades=%d valid=%s",
                i + 1, best_params, best_is_score,
                oos_result.metrics.total_trades, is_valid,
            )

        # ── Aggregate OOS results ─────────────────────────────────────────────
        all_oos_curve.sort(key=lambda x: x[0])

        oos_metrics = compute_metrics(
            equity_curve=all_oos_curve,
            trades=all_oos_trades,
            initial_capital=wf_config.initial_capital,
            risk_free_rate=wf_config.risk_free_rate,
        )

        best_params = self._most_selected_params(selected_params_history, param_grid)
        param_stability = self._compute_stability(selected_params_history, param_grid)

        result = WalkForwardResult(
            config=wf_config,
            param_grid=param_grid,
            windows=window_results,
            oos_metrics=oos_metrics,
            best_params=best_params,
            param_stability=param_stability,
            oos_equity_curve=all_oos_curve,
            oos_trades=all_oos_trades,
        )

        logger.info("[WalkForward] Complete: %s", result.summary())
        return result

    # ─── Grid search ──────────────────────────────────────────────────────────

    async def _grid_search(
        self,
        strategy_class: Type[BaseStrategy],
        bars: list[BarData],
        param_grid: dict[str, list[Any]],
        base_params: dict[str, Any],
        wf_config: WalkForwardConfig,
        options_chains: list[OptionsChainSnapshot],
    ) -> tuple[dict[str, Any], float]:
        """
        Run a full grid search over param_grid on the training bars.
        Returns (best_params, best_score).
        """
        keys = list(param_grid.keys())
        combinations = list(product(*[param_grid[k] for k in keys]))

        if not combinations:
            return base_params, 0.0

        semaphore = asyncio.Semaphore(wf_config.max_parallel)

        async def run_one(combo: tuple) -> tuple[dict, float]:
            params = {**base_params, **dict(zip(keys, combo))}
            async with semaphore:
                config = BacktestConfig(
                    strategy_class=strategy_class,
                    strategy_params=params,
                    bars=bars,
                    initial_capital=wf_config.initial_capital,
                    fill_method=wf_config.fill_method,
                    risk_free_rate=wf_config.risk_free_rate,
                    options_chains=options_chains,
                    strategy_id="wf_grid_search",
                )
                result = await self._engine.run(config)
                score = self._extract_score(result.metrics, wf_config.optimize_on)
            return params, score

        tasks = [run_one(combo) for combo in combinations]
        all_results = await asyncio.gather(*tasks, return_exceptions=True)

        best_params = base_params
        best_score = float("-inf")

        for r in all_results:
            if isinstance(r, Exception):
                logger.warning("[WalkForward] Grid search task failed: %s", r)
                continue
            params, score = r
            if score > best_score:
                best_score = score
                best_params = params

        return best_params, best_score

    # ─── Helpers ──────────────────────────────────────────────────────────────

    def _generate_windows(
        self,
        bars: list[BarData],
        config: WalkForwardConfig,
    ) -> list[tuple[list[BarData], list[BarData]]]:
        """
        Generate (train_bars, test_bars) tuples for all walk-forward windows.

        Window layout:
          [|---- train (train_pct) ----|---- test (1-train_pct) ----|]
                                                [|---- train ----|---- test ----|]  ← step forward
        """
        n = len(bars)
        step_size = max(1, int(n * config.step_pct))
        total_window = n   # use full history, advance by step_pct

        windows: list[tuple[list[BarData], list[BarData]]] = []

        # First train/test split covers all data
        # Each subsequent window advances by step_pct of total bars
        window_size = n

        start_idx = 0
        while start_idx + step_size < n:
            available = bars[start_idx:]
            if len(available) < 10:
                break

            train_end = int(len(available) * config.train_pct)
            train_bars = available[:train_end]
            test_bars  = available[train_end:]

            if len(train_bars) < 5 or len(test_bars) < 5:
                break

            windows.append((train_bars, test_bars))
            start_idx += step_size

        return windows

    @staticmethod
    def _extract_score(metrics: PerformanceMetrics, metric_name: str) -> float:
        """Extract the optimization target metric value."""
        value = getattr(metrics, metric_name, 0.0)
        # Guard against inf/nan
        if value != value or value == float("inf") or value == float("-inf"):
            return 0.0
        return float(value)

    @staticmethod
    def _filter_chains(
        chains: list[OptionsChainSnapshot],
        bars: list[BarData],
    ) -> list[OptionsChainSnapshot]:
        """Filter options chains to only those within the bar date range."""
        if not chains or not bars:
            return []
        start = bars[0].timestamp
        end   = bars[-1].timestamp
        return [c for c in chains if start <= c.timestamp <= end]

    @staticmethod
    def _most_selected_params(
        history: list[dict[str, Any]],
        param_grid: dict[str, list[Any]],
    ) -> dict[str, Any]:
        """
        Find the most-frequently selected parameter combination across windows.
        Falls back to grid midpoint if no history.
        """
        if not history:
            return {k: v[len(v) // 2] for k, v in param_grid.items()}

        # Count occurrences of each param set
        from collections import Counter
        frozen = [tuple(sorted(d.items())) for d in history]
        most_common_tuple = Counter(frozen).most_common(1)[0][0]
        return dict(most_common_tuple)

    @staticmethod
    def _compute_stability(
        history: list[dict[str, Any]],
        param_grid: dict[str, list[Any]],
    ) -> dict[str, float]:
        """
        Compute per-parameter coefficient of variation (CV = std/mean) across windows.

        CV interpretation:
          < 0.20 → highly stable (good sign)
          0.20–0.50 → moderately stable
          > 0.50 → unstable (overfit warning)
        """
        if len(history) < 2:
            return {k: 0.0 for k in param_grid}

        import statistics

        stability: dict[str, float] = {}
        for key in param_grid:
            values = [float(d[key]) for d in history if key in d and isinstance(d[key], (int, float))]
            if len(values) < 2:
                stability[key] = 0.0
                continue
            mean = statistics.mean(values)
            stdev = statistics.stdev(values)
            stability[key] = stdev / mean if mean != 0 else 0.0

        return stability


# ─── Convenience function ─────────────────────────────────────────────────────

async def optimize(
    strategy_class: Type[BaseStrategy],
    bars: list[BarData],
    param_grid: dict[str, list[Any]],
    base_params: dict[str, Any] | None = None,
    train_pct: float = 0.70,
    optimize_on: str = "sharpe_ratio",
    initial_capital: float = 100_000.0,
) -> WalkForwardResult:
    """
    One-shot walk-forward optimization with sensible defaults.

    Example:
        result = await optimize(
            strategy_class=MovingAverageCrossover,
            bars=bars,
            param_grid={"fast_period": [5, 10, 20], "slow_period": [30, 50, 100]},
            base_params={"symbols": ["AAPL"], "bar_frequency": "1d"},
        )
        print(result.summary())
    """
    optimizer = WalkForwardOptimizer()
    config = WalkForwardConfig(
        train_pct=train_pct,
        optimize_on=optimize_on,
        initial_capital=initial_capital,
    )
    return await optimizer.run(
        strategy_class=strategy_class,
        all_bars=bars,
        param_grid=param_grid,
        wf_config=config,
        base_params=base_params,
    )
