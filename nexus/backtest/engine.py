"""
Backtest Engine — the core of Phase 3.

Design principle: strategies run IDENTICAL code in backtest and live modes.
The only difference is the publisher: in backtest, BacktestPublisher captures
signals to a list rather than writing them to Redis Streams.

How the mock works:
  BaseStrategy.emit_signal() calls `publisher.publish("strategy.signal", ...)`
  where `publisher` is imported as a module-level singleton in nexus.strategy.base.
  BacktestEngine temporarily replaces nexus.strategy.base.publisher with a
  BacktestPublisher instance. After the run, the original publisher is restored.

Flow per bar:
  1. strategy.on_bar(bar)         — or on_options_chain(chain) for options strats
  2. BacktestPublisher captures emitted signals
  3. FillSimulator → FillResult   — fill at next_bar.open (lookahead-free)
  4. Update position book + equity curve
  5. Record TradeRecord for closed positions

End of run:
  analytics.compute_metrics(equity_curve, trades) → PerformanceMetrics
  Returns BacktestResult with full audit trail.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Type

import nexus.strategy.base as _strategy_base_module
from nexus.backtest.analytics import PerformanceMetrics, compute_metrics
from nexus.backtest.fill_simulator import FillMethod, FillResult, FillSimulator
from nexus.core.config import TradingMode
from nexus.core.schemas import (
    BarData,
    Direction,
    OptionsChainSnapshot,
    Signal,
    TelemetryEvent,
)
from nexus.strategy.base import BaseStrategy

logger = logging.getLogger(__name__)


# ─── Backtest Publisher (Redis-free signal capture) ───────────────────────────

class BacktestPublisher:
    """
    Drop-in replacement for EventPublisher during backtesting.

    Implements the same async interface as nexus.bus.publisher.EventPublisher
    but instead of writing to Redis, captures signals to self.signals.

    All telemetry and heartbeat calls are silently discarded — we only
    care about strategy signals during backtest.
    """

    def __init__(self) -> None:
        self.signals: list[Signal] = []
        self._connected = True

    async def connect(self) -> None:
        pass

    async def close(self) -> None:
        pass

    async def publish(self, channel: str, payload: dict, maxlen: int = 10_000) -> None:
        """Capture strategy.signal payloads. Ignore all other channels."""
        if channel == "strategy.signal":
            try:
                self.signals.append(Signal.model_validate(payload))
            except Exception as exc:
                logger.debug("BacktestPublisher: failed to parse signal: %s", exc)

    async def publish_telemetry(self, event: TelemetryEvent) -> None:
        pass  # telemetry is irrelevant during offline backtest

    async def heartbeat(self, component_id: str) -> None:
        pass

    def drain(self) -> list[Signal]:
        """Return and clear captured signals."""
        signals = list(self.signals)
        self.signals.clear()
        return signals


# ─── Configuration ────────────────────────────────────────────────────────────

@dataclass
class BacktestConfig:
    """
    Full specification for a backtest run.

    strategy_class : BaseStrategy subclass (e.g. MovingAverageCrossover)
    strategy_params: same params dict as used in live trading
    bars           : chronologically ordered list of BarData to replay
    initial_capital: starting cash (default: $100,000)
    fill_method    : how fills are simulated (default: NEXT_BAR_OPEN)
    risk_free_rate : annualised rate for Sharpe calc (default: 5%)
    options_chains : optional pre-built chains for options strategies
                     (built by OptionsChainBuilder from nexus.backtest.data_loader)
    commission_model: 'ibkr_equity' | 'ibkr_options' | 'zero'
    """
    strategy_class: Type[BaseStrategy]
    strategy_params: dict[str, Any]
    bars: list[BarData]
    initial_capital: float = 100_000.0
    fill_method: FillMethod = FillMethod.NEXT_BAR_OPEN
    risk_free_rate: float = 0.05
    options_chains: list[OptionsChainSnapshot] = field(default_factory=list)
    commission_model: str = "ibkr_equity"
    strategy_id: str = "backtest_strategy"


# ─── Trade Record ─────────────────────────────────────────────────────────────

@dataclass
class TradeRecord:
    """A completed round-trip trade (entry + exit)."""
    symbol: str
    direction: Direction           # BUY or SELL (entry direction)
    entry_time: datetime
    exit_time: datetime | None
    entry_price: float
    exit_price: float | None
    quantity: float
    commission: float
    pnl: float                    # realized P&L net of commissions
    signal_id: str = ""
    strategy_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def is_closed(self) -> bool:
        return self.exit_time is not None

    @property
    def duration_bars(self) -> int:
        return self.metadata.get("duration_bars", 0)


# ─── Backtest Result ──────────────────────────────────────────────────────────

@dataclass
class BacktestResult:
    """Complete output of a BacktestEngine.run() call."""
    config: BacktestConfig
    equity_curve: list[tuple[datetime, float]]   # (timestamp, portfolio_value)
    trades: list[TradeRecord]
    metrics: PerformanceMetrics
    signals_emitted: int
    signals_executed: int
    signals_rejected: int
    run_duration_ms: float

    def summary(self) -> dict[str, Any]:
        """Compact summary dict for logging or dashboard display."""
        m = self.metrics
        return {
            "total_return_pct": round(m.total_return * 100, 2),
            "cagr_pct": round(m.cagr * 100, 2),
            "sharpe": round(m.sharpe_ratio, 3),
            "sortino": round(m.sortino_ratio, 3),
            "calmar": round(m.calmar_ratio, 3),
            "max_drawdown_pct": round(m.max_drawdown * 100, 2),
            "win_rate_pct": round(m.win_rate * 100, 1),
            "profit_factor": round(m.profit_factor, 3),
            "total_trades": m.total_trades,
            "signals_emitted": self.signals_emitted,
            "signals_executed": self.signals_executed,
            "run_duration_ms": round(self.run_duration_ms, 1),
        }


# ─── Position Book (in-memory for backtest) ───────────────────────────────────

class _BacktestPosition:
    """Simple long/short position tracker for the backtest engine."""

    def __init__(self) -> None:
        self._positions: dict[str, dict[str, Any]] = {}  # symbol → {qty, avg_cost, entry_time, signal_id}

    def update(self, symbol: str, qty_delta: float, price: float,
               entry_time: datetime, signal_id: str) -> TradeRecord | None:
        """
        Update position. Returns a closed TradeRecord if the position was closed.
        """
        if symbol not in self._positions:
            self._positions[symbol] = {
                "qty": 0.0, "avg_cost": 0.0,
                "entry_time": entry_time, "signal_id": signal_id,
            }

        pos = self._positions[symbol]
        existing_qty = pos["qty"]
        new_qty = existing_qty + qty_delta

        closed_trade: TradeRecord | None = None

        if existing_qty != 0 and (
            (existing_qty > 0 and qty_delta < 0) or
            (existing_qty < 0 and qty_delta > 0)
        ):
            # Position is being reduced or closed
            close_qty = min(abs(qty_delta), abs(existing_qty))
            if existing_qty > 0:
                realized = close_qty * (price - pos["avg_cost"])
                entry_dir = Direction.BUY
            else:
                realized = close_qty * (pos["avg_cost"] - price)
                entry_dir = Direction.SELL

            closed_trade = TradeRecord(
                symbol=symbol,
                direction=entry_dir,
                entry_time=pos["entry_time"],
                exit_time=entry_time,
                entry_price=pos["avg_cost"],
                exit_price=price,
                quantity=close_qty,
                commission=0.0,   # set by caller
                pnl=realized,
                signal_id=pos["signal_id"],
            )

        if abs(new_qty) < 1e-9:
            del self._positions[symbol]
        elif (existing_qty >= 0 and qty_delta > 0) or (existing_qty <= 0 and qty_delta < 0):
            # Adding to position: VWAP
            total_cost = abs(existing_qty) * pos["avg_cost"] + abs(qty_delta) * price
            pos["avg_cost"] = total_cost / abs(new_qty)
            pos["qty"] = new_qty
        else:
            pos["qty"] = new_qty

        return closed_trade

    def open_positions(self) -> dict[str, dict[str, Any]]:
        return dict(self._positions)

    def unrealized_pnl(self, prices: dict[str, float]) -> float:
        total = 0.0
        for sym, pos in self._positions.items():
            price = prices.get(sym, pos["avg_cost"])
            total += pos["qty"] * (price - pos["avg_cost"])
        return total


# ─── Backtest Engine ──────────────────────────────────────────────────────────

class BacktestEngine:
    """
    Event-driven strategy replay engine.

    Uses BacktestPublisher to intercept strategy signals without Redis.
    Compatible with any BaseStrategy subclass — equity or options.

    Example:
        config = BacktestConfig(
            strategy_class=MovingAverageCrossover,
            strategy_params={"symbols": ["AAPL"], "fast_period": 10, "slow_period": 30},
            bars=bars,
        )
        result = await BacktestEngine().run(config)
        print(result.summary())
    """

    async def run(self, config: BacktestConfig) -> BacktestResult:
        """
        Execute a full backtest. Returns a BacktestResult with equity curve,
        trade records, and computed performance metrics.
        """
        t_start = time.monotonic()
        logger.info(
            "[Backtest] Starting: strategy=%s bars=%d capital=%.0f",
            config.strategy_class.__name__, len(config.bars), config.initial_capital,
        )

        # ── Instantiate strategy in BACKTEST mode ─────────────────────────────
        strategy = config.strategy_class(
            strategy_id=config.strategy_id,
            params=config.strategy_params,
            mode=TradingMode.BACKTEST,
        )

        # ── Install BacktestPublisher (replaces Redis publisher) ───────────────
        mock_publisher = BacktestPublisher()
        original_publisher = _strategy_base_module.publisher
        _strategy_base_module.publisher = mock_publisher  # type: ignore[assignment]

        try:
            result = await self._replay(config, strategy, mock_publisher)
        finally:
            # Always restore the real publisher — even if an exception occurs
            _strategy_base_module.publisher = original_publisher

        t_end = time.monotonic()
        result.run_duration_ms = (t_end - t_start) * 1000

        logger.info(
            "[Backtest] Complete in %.1fms — %s",
            result.run_duration_ms, result.summary(),
        )
        return result

    # ─── Core replay loop ─────────────────────────────────────────────────────

    async def _replay(
        self,
        config: BacktestConfig,
        strategy: BaseStrategy,
        mock_publisher: BacktestPublisher,
    ) -> BacktestResult:
        bars = config.bars
        sizer = FillSimulator(commission_model=config.commission_model)
        position_book = _BacktestPosition()

        cash = config.initial_capital
        equity_curve: list[tuple[datetime, float]] = []
        trades: list[TradeRecord] = []
        signals_emitted = 0
        signals_executed = 0
        signals_rejected = 0

        # Build options chain index (timestamp → chain) for fast lookup
        chain_index: dict[datetime, OptionsChainSnapshot] = {
            c.timestamp: c for c in config.options_chains
        }
        # Keep an ordered list of chain timestamps for nearest-time lookup
        chain_timestamps = sorted(chain_index.keys())

        # Track current prices for unrealized P&L
        current_prices: dict[str, float] = {}

        await strategy.on_start()

        for i, bar in enumerate(bars):
            current_prices[bar.symbol] = bar.close
            next_bar = bars[i + 1] if i + 1 < len(bars) else None

            # ── Feed bar to strategy ───────────────────────────────────────────
            await strategy.on_bar(bar)

            # ── Feed matching options chain if available ───────────────────────
            chain = self._nearest_chain(bar.timestamp, chain_index, chain_timestamps)
            if chain and chain.symbol == bar.symbol:
                await strategy.on_options_chain(chain)

            # ── Process captured signals ───────────────────────────────────────
            for signal in mock_publisher.drain():
                signals_emitted += 1

                if signal.direction == Direction.HOLD:
                    continue

                # Determine fill bar (next bar for lookahead-free fills)
                fill_bar = next_bar if config.fill_method == FillMethod.NEXT_BAR_OPEN else bar
                if fill_bar is None:
                    # Last bar — can't fill with next-bar-open
                    signals_rejected += 1
                    continue

                # Simulate fill
                fill_result = sizer.simulate_equity_fill(signal, fill_bar, config.fill_method)
                if not fill_result.success:
                    signals_rejected += 1
                    continue

                # Update position book
                qty_signed = fill_result.quantity if signal.direction == Direction.BUY else -fill_result.quantity
                closed_trade = position_book.update(
                    symbol=signal.symbol,
                    qty_delta=qty_signed,
                    price=fill_result.fill_price,
                    entry_time=fill_result.fill_time,
                    signal_id=signal.signal_id,
                )

                # Update cash
                cash -= qty_signed * fill_result.fill_price + fill_result.commission

                # Record closed trade with commission
                if closed_trade:
                    closed_trade.commission = fill_result.commission
                    closed_trade.pnl -= fill_result.commission
                    closed_trade.strategy_id = config.strategy_id
                    trades.append(closed_trade)

                signals_executed += 1

            # ── Equity curve snapshot ──────────────────────────────────────────
            unrealized = position_book.unrealized_pnl(current_prices)
            portfolio_value = round(cash + unrealized, 2)
            equity_curve.append((bar.timestamp, portfolio_value))

        await strategy.on_stop()

        # ── Close any open positions at last bar price ─────────────────────────
        last_bar_time = bars[-1].timestamp if bars else datetime.utcnow()
        for sym, pos in position_book.open_positions().items():
            price = current_prices.get(sym, pos["avg_cost"])
            qty = pos["qty"]
            commission = sizer.equity_commission(abs(qty), price)
            if qty > 0:
                pnl = qty * (price - pos["avg_cost"]) - commission
            else:
                pnl = abs(qty) * (pos["avg_cost"] - price) - commission
            trades.append(TradeRecord(
                symbol=sym,
                direction=Direction.BUY if qty > 0 else Direction.SELL,
                entry_time=pos["entry_time"],
                exit_time=last_bar_time,
                entry_price=pos["avg_cost"],
                exit_price=price,
                quantity=abs(qty),
                commission=commission,
                pnl=pnl,
                signal_id=pos["signal_id"],
                strategy_id=config.strategy_id,
                metadata={"force_closed": True},
            ))

        # ── Compute metrics ────────────────────────────────────────────────────
        metrics = compute_metrics(
            equity_curve=equity_curve,
            trades=trades,
            initial_capital=config.initial_capital,
            risk_free_rate=config.risk_free_rate,
        )

        return BacktestResult(
            config=config,
            equity_curve=equity_curve,
            trades=trades,
            metrics=metrics,
            signals_emitted=signals_emitted,
            signals_executed=signals_executed,
            signals_rejected=signals_rejected,
            run_duration_ms=0.0,   # set by caller
        )

    # ─── Helpers ──────────────────────────────────────────────────────────────

    def _nearest_chain(
        self,
        bar_time: datetime,
        chain_index: dict[datetime, OptionsChainSnapshot],
        chain_timestamps: list[datetime],
    ) -> OptionsChainSnapshot | None:
        """
        Return the most recent chain whose timestamp is ≤ bar_time.
        Uses binary search for efficiency on large chain lists.
        """
        if not chain_timestamps:
            return None

        # Binary search for largest timestamp ≤ bar_time
        lo, hi = 0, len(chain_timestamps) - 1
        result_idx = -1
        while lo <= hi:
            mid = (lo + hi) // 2
            if chain_timestamps[mid] <= bar_time:
                result_idx = mid
                lo = mid + 1
            else:
                hi = mid - 1

        if result_idx == -1:
            return None
        return chain_index[chain_timestamps[result_idx]]
