"""
Moving Average Crossover — sample equity strategy.

BUY  when fast MA crosses ABOVE slow MA  (golden cross)
SELL when fast MA crosses BELOW slow MA  (death cross)

Uses a pure deque-based rolling window — no pandas overhead in the hot path.
Signal is only emitted on the crossover bar (state change), not on every bar.

Displayable parameters (editable in UI, hot-reloadable):
  symbols       : list[str]   — symbols to trade (e.g. ['AAPL', 'SPY'])
  fast_period   : int         — fast MA lookback bars (default: 10)
  slow_period   : int         — slow MA lookback bars (default: 30)
  bar_frequency : str         — '1m' | '5m' | '1h' | '1d' (default: '1d')
  quantity      : int         — shares per trade (default: 100)
  min_volume    : int         — ignore bars with volume below this (default: 0)

DB class_path: nexus.strategy.strategies.ma_crossover.MovingAverageCrossover
"""

from __future__ import annotations

import logging
from collections import deque
from typing import Any

from nexus.core.schemas import BarData, Direction, Signal, TelemetryLevel
from nexus.strategy.base import BaseStrategy

logger = logging.getLogger(__name__)


class MovingAverageCrossover(BaseStrategy):
    """
    Simple moving average crossover for equity instruments.
    Demonstrates the BaseStrategy ABC for equity strategies.
    """

    def __init__(self, strategy_id: str, params: dict[str, Any], **kwargs: Any) -> None:
        super().__init__(strategy_id, params, **kwargs)
        self._apply_params()

    def _apply_params(self) -> None:
        """Parse and apply current params. Called on init and hot-reload."""
        self.symbols: list[str] = self._params.get("symbols", [])
        self.fast_period: int = int(self._params.get("fast_period", 10))
        self.slow_period: int = int(self._params.get("slow_period", 30))
        self.bar_frequency: str = self._params.get("bar_frequency", "1d")
        self.quantity: int = int(self._params.get("quantity", 100))
        self.min_volume: int = int(self._params.get("min_volume", 0))

        # Rolling price windows: sized to slow_period + 1 (need previous bar too)
        self._prices: dict[str, deque[float]] = {
            s: deque(maxlen=self.slow_period + 2) for s in self.symbols
        }
        self._last_direction: dict[str, Direction | None] = {s: None for s in self.symbols}

    def update_params(self, new_params: dict[str, Any]) -> None:
        super().update_params(new_params)
        self._apply_params()   # rebuild windows when params change

    # ─── Core logic ───────────────────────────────────────────────────────────

    async def on_bar(self, bar: BarData) -> None:
        if bar.symbol not in self.symbols:
            return
        if bar.frequency != self.bar_frequency:
            return
        if self.min_volume > 0 and bar.volume < self.min_volume:
            return

        prices = self._prices[bar.symbol]
        prices.append(bar.close)

        # Need at least slow_period + 1 bars to detect a crossover
        if len(prices) < self.slow_period + 1:
            return

        price_list = list(prices)

        # Current bar MAs
        fast_ma = sum(price_list[-self.fast_period:]) / self.fast_period
        slow_ma = sum(price_list[-self.slow_period:]) / self.slow_period

        # Previous bar MAs (one bar earlier)
        prev_fast = sum(price_list[-self.fast_period - 1:-1]) / self.fast_period
        prev_slow = sum(price_list[-self.slow_period - 1:-1]) / self.slow_period

        # Golden cross: fast crosses above slow
        if prev_fast <= prev_slow and fast_ma > slow_ma:
            if self._last_direction[bar.symbol] != Direction.BUY:
                self._last_direction[bar.symbol] = Direction.BUY
                await self.emit_signal(Signal(
                    symbol=bar.symbol,
                    direction=Direction.BUY,
                    confidence=self._compute_confidence(fast_ma, slow_ma),
                    strategy_id=self.strategy_id,
                    mode=self.mode,
                    suggested_quantity=float(self.quantity),
                    metadata={
                        "trigger": "golden_cross",
                        "fast_ma": round(fast_ma, 4),
                        "slow_ma": round(slow_ma, 4),
                        "bar_close": bar.close,
                        "fast_period": self.fast_period,
                        "slow_period": self.slow_period,
                    },
                ))

        # Death cross: fast crosses below slow
        elif prev_fast >= prev_slow and fast_ma < slow_ma:
            if self._last_direction[bar.symbol] != Direction.SELL:
                self._last_direction[bar.symbol] = Direction.SELL
                await self.emit_signal(Signal(
                    symbol=bar.symbol,
                    direction=Direction.SELL,
                    confidence=self._compute_confidence(slow_ma, fast_ma),
                    strategy_id=self.strategy_id,
                    mode=self.mode,
                    suggested_quantity=float(self.quantity),
                    metadata={
                        "trigger": "death_cross",
                        "fast_ma": round(fast_ma, 4),
                        "slow_ma": round(slow_ma, 4),
                        "bar_close": bar.close,
                        "fast_period": self.fast_period,
                        "slow_period": self.slow_period,
                    },
                ))

    def _compute_confidence(self, larger_ma: float, smaller_ma: float) -> float:
        """
        Confidence = % separation between MAs, capped at 1.0.
        A wider separation = stronger trend = higher confidence.
        """
        if smaller_ma <= 0:
            return 0.5
        separation = (larger_ma - smaller_ma) / smaller_ma
        return min(1.0, 0.5 + separation * 10)

    # ─── Displayable state ────────────────────────────────────────────────────

    @property
    def displayable_state(self) -> dict[str, Any]:
        state = super().displayable_state
        ma_state: dict[str, Any] = {}
        for symbol, prices in self._prices.items():
            if len(prices) >= self.slow_period:
                pl = list(prices)
                ma_state[symbol] = {
                    "fast_ma": round(sum(pl[-self.fast_period:]) / self.fast_period, 4),
                    "slow_ma": round(sum(pl[-self.slow_period:]) / self.slow_period, 4),
                    "bars_loaded": len(pl),
                    "last_signal": self._last_direction[symbol].value
                    if self._last_direction[symbol] else "NONE",
                }
            else:
                ma_state[symbol] = {
                    "bars_loaded": len(prices),
                    "bars_needed": self.slow_period + 1,
                    "status": "warming_up",
                }
        state["moving_averages"] = ma_state
        return state
