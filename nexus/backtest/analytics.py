"""
Performance Analytics — Phase 3.

Computes professional-grade performance metrics from a backtest equity curve
and trade list. Pure numpy — no pandas dependency in the hot path.

Metrics computed:
  Return metrics:
    total_return    — (final - initial) / initial
    cagr            — compound annual growth rate
    avg_daily_return

  Risk-adjusted:
    sharpe_ratio    — (mean excess return) / std × √252
    sortino_ratio   — (mean excess return) / downside_std × √252
    calmar_ratio    — CAGR / max_drawdown

  Drawdown:
    max_drawdown        — maximum peak-to-trough decline
    max_drawdown_duration — longest time from peak to recovery (in bars)
    avg_drawdown

  Trade statistics:
    total_trades, winning_trades, losing_trades
    win_rate        — winning / total
    profit_factor   — gross_profit / abs(gross_loss)
    avg_trade_pnl   — mean P&L per trade (net of commission)
    avg_winner      — mean P&L of winning trades
    avg_loser       — mean P&L of losing trades
    best_trade / worst_trade
    avg_trade_duration (in bars)

  Turnover:
    total_commission    — total commissions paid
    commission_drag_pct — commissions as % of initial capital

Dependencies: numpy only (already in pyproject.toml)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from nexus.backtest.engine import TradeRecord

logger = logging.getLogger(__name__)


# ─── Performance Metrics Dataclass ────────────────────────────────────────────

@dataclass
class PerformanceMetrics:
    """
    Complete set of performance metrics for a backtest run.
    All rate metrics are expressed as decimals (e.g. 0.15 = 15%).
    """
    # ── Return metrics ──────────────────────────────────────────────────────
    total_return: float = 0.0         # (final - initial) / initial
    cagr: float = 0.0                 # compound annual growth rate
    avg_daily_return: float = 0.0     # mean daily return of equity curve

    # ── Risk-adjusted metrics ───────────────────────────────────────────────
    sharpe_ratio: float = 0.0         # annualised Sharpe (excess return / vol)
    sortino_ratio: float = 0.0        # annualised Sortino (penalises downside only)
    calmar_ratio: float = 0.0         # CAGR / max_drawdown

    # ── Drawdown metrics ────────────────────────────────────────────────────
    max_drawdown: float = 0.0         # maximum peak-to-trough (as positive fraction)
    max_drawdown_duration: int = 0    # longest bars from peak to recovery
    avg_drawdown: float = 0.0         # average drawdown across all DD periods

    # ── Volatility ──────────────────────────────────────────────────────────
    annual_volatility: float = 0.0    # annualised std of daily returns
    downside_volatility: float = 0.0  # std of negative daily returns only

    # ── Trade statistics ────────────────────────────────────────────────────
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    win_rate: float = 0.0             # winning / total
    profit_factor: float = 0.0        # gross_profit / abs(gross_loss)
    avg_trade_pnl: float = 0.0        # mean P&L per trade (net of commission)
    avg_winner: float = 0.0           # mean P&L of winning trades
    avg_loser: float = 0.0            # mean P&L of losing trades (negative)
    best_trade: float = 0.0           # largest single-trade P&L
    worst_trade: float = 0.0          # largest single-trade loss (negative)
    avg_trade_duration: float = 0.0   # average bars held per trade

    # ── Cost metrics ────────────────────────────────────────────────────────
    total_commission: float = 0.0
    commission_drag_pct: float = 0.0  # commissions / initial_capital

    # ── Period info ─────────────────────────────────────────────────────────
    start_date: datetime | None = None
    end_date: datetime | None = None
    total_bars: int = 0
    calendar_days: float = 0.0

    def to_dict(self) -> dict:
        """Flat dict representation for logging, JSON, dashboard display."""
        return {
            "total_return_pct": round(self.total_return * 100, 2),
            "cagr_pct": round(self.cagr * 100, 2),
            "sharpe_ratio": round(self.sharpe_ratio, 3),
            "sortino_ratio": round(self.sortino_ratio, 3),
            "calmar_ratio": round(self.calmar_ratio, 3),
            "max_drawdown_pct": round(self.max_drawdown * 100, 2),
            "max_drawdown_duration_bars": self.max_drawdown_duration,
            "annual_volatility_pct": round(self.annual_volatility * 100, 2),
            "win_rate_pct": round(self.win_rate * 100, 1),
            "profit_factor": round(self.profit_factor, 3),
            "total_trades": self.total_trades,
            "winning_trades": self.winning_trades,
            "avg_trade_pnl": round(self.avg_trade_pnl, 2),
            "best_trade": round(self.best_trade, 2),
            "worst_trade": round(self.worst_trade, 2),
            "total_commission": round(self.total_commission, 2),
            "commission_drag_pct": round(self.commission_drag_pct * 100, 3),
            "total_bars": self.total_bars,
            "calendar_days": round(self.calendar_days, 0),
        }


# ─── Core computation function ────────────────────────────────────────────────

def compute_metrics(
    equity_curve: list[tuple[datetime, float]],
    trades: list[TradeRecord],
    initial_capital: float,
    risk_free_rate: float = 0.05,
) -> PerformanceMetrics:
    """
    Compute all performance metrics from an equity curve and trade list.

    Args:
        equity_curve   : list of (timestamp, portfolio_value) tuples, chronological
        trades         : list of TradeRecord from the backtest engine
        initial_capital: starting capital (used for normalisation)
        risk_free_rate : annualised risk-free rate (default 5%)

    Returns:
        PerformanceMetrics dataclass with all metrics computed
    """
    m = PerformanceMetrics()

    if not equity_curve:
        return m

    m.total_bars = len(equity_curve)
    m.start_date = equity_curve[0][0]
    m.end_date = equity_curve[-1][0]

    if m.start_date and m.end_date:
        m.calendar_days = (m.end_date - m.start_date).days

    values = np.array([v for _, v in equity_curve], dtype=float)
    final_value = values[-1]

    # ── Return metrics ────────────────────────────────────────────────────────
    m.total_return = (final_value - initial_capital) / initial_capital if initial_capital > 0 else 0.0

    years = m.calendar_days / 365.0
    if years > 0 and initial_capital > 0 and final_value > 0:
        m.cagr = (final_value / initial_capital) ** (1.0 / years) - 1.0

    # Daily returns from equity curve (log returns for robustness)
    daily_returns = np.diff(values) / values[:-1]
    daily_returns = daily_returns[np.isfinite(daily_returns)]  # guard NaN/Inf

    if len(daily_returns) > 0:
        m.avg_daily_return = float(np.mean(daily_returns))

    # ── Volatility ────────────────────────────────────────────────────────────
    if len(daily_returns) > 1:
        m.annual_volatility = float(np.std(daily_returns, ddof=1) * np.sqrt(252))
        downside = daily_returns[daily_returns < 0]
        m.downside_volatility = float(np.std(downside, ddof=1) * np.sqrt(252)) if len(downside) > 1 else 0.0

    # ── Sharpe ratio ──────────────────────────────────────────────────────────
    daily_rf = risk_free_rate / 252.0
    if len(daily_returns) > 1 and m.annual_volatility > 0:
        excess_returns = daily_returns - daily_rf
        m.sharpe_ratio = float(np.mean(excess_returns) / np.std(excess_returns, ddof=1) * np.sqrt(252))

    # ── Sortino ratio ─────────────────────────────────────────────────────────
    if len(daily_returns) > 1 and m.downside_volatility > 0:
        m.sortino_ratio = float((m.avg_daily_return - daily_rf) * 252 / m.downside_volatility)

    # ── Drawdown analysis ─────────────────────────────────────────────────────
    dd_metrics = _compute_drawdown(values)
    m.max_drawdown = dd_metrics["max_drawdown"]
    m.max_drawdown_duration = dd_metrics["max_duration"]
    m.avg_drawdown = dd_metrics["avg_drawdown"]

    # ── Calmar ratio ──────────────────────────────────────────────────────────
    if m.max_drawdown > 0:
        m.calmar_ratio = m.cagr / m.max_drawdown

    # ── Trade statistics ──────────────────────────────────────────────────────
    if trades:
        _compute_trade_stats(m, trades, initial_capital)

    return m


# ─── Drawdown helpers ─────────────────────────────────────────────────────────

def _compute_drawdown(values: np.ndarray) -> dict:
    """
    Compute drawdown series and summary stats.

    Algorithm: running maximum (peak), drawdown = (peak - value) / peak.
    Tracks duration as consecutive bars below previous peak.
    """
    n = len(values)
    if n == 0:
        return {"max_drawdown": 0.0, "max_duration": 0, "avg_drawdown": 0.0}

    running_max = np.maximum.accumulate(values)
    # Avoid division by zero
    safe_max = np.where(running_max > 0, running_max, 1.0)
    drawdowns = (running_max - values) / safe_max

    max_dd = float(np.max(drawdowns))

    # Duration analysis
    in_drawdown = drawdowns > 1e-6
    max_duration = 0
    current_duration = 0
    all_durations: list[int] = []

    for dd_flag in in_drawdown:
        if dd_flag:
            current_duration += 1
        else:
            if current_duration > 0:
                all_durations.append(current_duration)
            current_duration = 0
    if current_duration > 0:
        all_durations.append(current_duration)

    if all_durations:
        max_duration = max(all_durations)

    non_zero_dd = drawdowns[drawdowns > 1e-6]
    avg_dd = float(np.mean(non_zero_dd)) if len(non_zero_dd) > 0 else 0.0

    return {
        "max_drawdown": max_dd,
        "max_duration": max_duration,
        "avg_drawdown": avg_dd,
        "series": drawdowns.tolist(),
    }


def _compute_trade_stats(
    m: PerformanceMetrics,
    trades: list[TradeRecord],
    initial_capital: float,
) -> None:
    """Compute trade-level statistics and write them into the metrics object."""
    m.total_trades = len(trades)
    pnls = [t.pnl for t in trades]
    commissions = [t.commission for t in trades]

    m.total_commission = sum(commissions)
    m.commission_drag_pct = m.total_commission / initial_capital if initial_capital > 0 else 0.0

    winners = [p for p in pnls if p > 0]
    losers  = [p for p in pnls if p <= 0]

    m.winning_trades = len(winners)
    m.losing_trades  = len(losers)
    m.win_rate = m.winning_trades / m.total_trades if m.total_trades > 0 else 0.0

    m.avg_trade_pnl = sum(pnls) / len(pnls) if pnls else 0.0
    m.best_trade  = max(pnls) if pnls else 0.0
    m.worst_trade = min(pnls) if pnls else 0.0

    m.avg_winner = sum(winners) / len(winners) if winners else 0.0
    m.avg_loser  = sum(losers)  / len(losers)  if losers  else 0.0

    gross_profit = sum(winners)
    gross_loss   = abs(sum(losers))
    m.profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")

    # Duration (use metadata if available, otherwise estimate from timestamps)
    durations: list[float] = []
    for t in trades:
        dur = t.metadata.get("duration_bars")
        if dur is not None:
            durations.append(float(dur))
        elif t.entry_time and t.exit_time:
            delta = (t.exit_time - t.entry_time).total_seconds()
            durations.append(delta / 86400.0)  # convert to days

    m.avg_trade_duration = sum(durations) / len(durations) if durations else 0.0


# ─── Equity curve utilities ────────────────────────────────────────────────────

def drawdown_series(equity_curve: list[tuple[datetime, float]]) -> list[tuple[datetime, float]]:
    """
    Return the full drawdown time series as (timestamp, drawdown_fraction) pairs.
    Useful for plotting in the dashboard.
    """
    if not equity_curve:
        return []
    values = np.array([v for _, v in equity_curve], dtype=float)
    times  = [t for t, _ in equity_curve]
    dd_metrics = _compute_drawdown(values)
    series = dd_metrics.get("series", [])
    return list(zip(times, series))


def rolling_sharpe(
    equity_curve: list[tuple[datetime, float]],
    window: int = 60,
    risk_free_rate: float = 0.05,
) -> list[tuple[datetime, float]]:
    """
    Rolling Sharpe ratio over a sliding window (default 60 bars).
    Returns (timestamp, sharpe) pairs aligned with equity_curve.
    """
    if len(equity_curve) < window + 1:
        return []

    values = np.array([v for _, v in equity_curve], dtype=float)
    times  = [t for t, _ in equity_curve]
    daily_rf = risk_free_rate / 252.0
    returns = np.diff(values) / values[:-1]

    result: list[tuple[datetime, float]] = []
    for i in range(window, len(returns) + 1):
        window_returns = returns[i - window: i]
        if len(window_returns) < 2:
            continue
        std = np.std(window_returns, ddof=1)
        if std > 0:
            sharpe = float((np.mean(window_returns) - daily_rf) / std * np.sqrt(252))
        else:
            sharpe = 0.0
        result.append((times[i], sharpe))

    return result
