"""
Nexus Backtesting Engine — Phase 3.

Provides event-driven strategy replay using the same strategy code
that runs in live and paper trading — zero code duplication.

Public API:
    BacktestEngine      — main orchestrator
    BacktestConfig      — configuration dataclass
    BacktestResult      — structured result with metrics + equity curve
    DataLoader          — historical bar loaders (CSV, Parquet, TimescaleDB)
    OptionsChainBuilder — synthetic options chain construction from HV
    FillSimulator       — fill simulation (equity + options)
    PerformanceMetrics  — Sharpe, Sortino, Calmar, drawdown, etc.
    WalkForwardOptimizer — rolling in-sample/OOS parameter optimization
"""

from nexus.backtest.analytics import PerformanceMetrics, compute_metrics
from nexus.backtest.data_loader import DataLoader, OptionsChainBuilder
from nexus.backtest.engine import BacktestConfig, BacktestEngine, BacktestResult
from nexus.backtest.fill_simulator import FillMethod, FillSimulator
from nexus.backtest.walk_forward import WalkForwardConfig, WalkForwardOptimizer

__all__ = [
    "BacktestEngine",
    "BacktestConfig",
    "BacktestResult",
    "DataLoader",
    "OptionsChainBuilder",
    "FillMethod",
    "FillSimulator",
    "PerformanceMetrics",
    "compute_metrics",
    "WalkForwardConfig",
    "WalkForwardOptimizer",
]
