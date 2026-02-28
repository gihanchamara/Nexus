"""
Strategy Abstract Base Class.

Every strategy — equity or options — implements this interface.
The platform calls:
  on_bar()            — new OHLCV bar for any subscribed symbol + frequency
  on_tick()           — live tick (optional; override for tick-level strategies)
  on_news()           — news event (optional; override for news-driven strategies)
  on_options_chain()  — options chain snapshot (override for all options strategies)

Strategies produce signals via emit_signal(), which publishes to the event bus.

Displayable parameters:
  Every strategy exposes self.params — a plain dict loaded from MySQL.
  The UI reads and edits these at runtime (hot-reload via update_params()).
  Strategies must document their params in the class docstring.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any

from nexus.bus.publisher import publisher
from nexus.core.config import TradingMode, get_settings
from nexus.core.schemas import (
    BarData,
    ComponentType,
    NewsEvent,
    OptionsChainSnapshot,
    Signal,
    TelemetryEvent,
    TelemetryLevel,
    TickData,
)

logger = logging.getLogger(__name__)


class BaseStrategy(ABC):
    """
    Abstract base for all Nexus strategies.

    Subclasses MUST implement on_bar().
    Subclasses SHOULD implement on_options_chain() if trading options.
    """

    def __init__(
        self,
        strategy_id: str,
        params: dict[str, Any],
        mode: TradingMode | None = None,
    ) -> None:
        self.strategy_id = strategy_id
        self._params: dict[str, Any] = dict(params)
        self.mode: TradingMode = mode or TradingMode(get_settings().nexus_trading_mode)
        self._active: bool = True
        self._signal_count: int = 0
        self._last_signal_time: datetime | None = None

    # ─── Abstract interface ───────────────────────────────────────────────────

    @abstractmethod
    async def on_bar(self, bar: BarData) -> None:
        """Called on every new OHLCV bar. Filter by symbol and frequency inside."""

    # ─── Optional overrides ───────────────────────────────────────────────────

    async def on_tick(self, tick: TickData) -> None:
        """Called on every live tick. Override for tick-level precision."""

    async def on_news(self, news: NewsEvent) -> None:
        """Called on every tagged news event. Override for news-driven strategies."""

    async def on_options_chain(self, chain: OptionsChainSnapshot) -> None:
        """Called on every options chain snapshot. Override for options strategies."""

    # ─── Signal emission ──────────────────────────────────────────────────────

    async def emit_signal(self, signal: Signal) -> None:
        """
        Publish a signal to the event bus (strategy.signal channel).
        The OMS subscribes to this channel and routes signals through
        risk checks → sizing → order building → paper/live execution.
        """
        signal.strategy_id = self.strategy_id
        signal.mode = self.mode
        await publisher.publish("strategy.signal", signal.model_dump(mode="json"))
        self._signal_count += 1
        self._last_signal_time = datetime.utcnow()
        logger.info(
            "[%s] Signal emitted: %s %s conf=%.2f",
            self.strategy_id, signal.direction.value, signal.symbol, signal.confidence,
        )
        await self._emit_telemetry(
            TelemetryLevel.INFO,
            {
                "event": "signal_emitted",
                "signal_id": signal.signal_id,
                "symbol": signal.symbol,
                "direction": signal.direction.value,
                "confidence": signal.confidence,
                "total_signals": self._signal_count,
            },
        )

    # ─── Parameter management ─────────────────────────────────────────────────

    def update_params(self, new_params: dict[str, Any]) -> None:
        """Hot-reload parameters from UI or DB. Called by registry on param_update events."""
        old = dict(self._params)
        self._params.update(new_params)
        logger.info("[%s] Params updated: %s → %s", self.strategy_id, old, self._params)

    @property
    def params(self) -> dict[str, Any]:
        """Current parameters as stored in MySQL. Displayed and editable in UI."""
        return dict(self._params)

    # ─── State display ────────────────────────────────────────────────────────

    @property
    def displayable_state(self) -> dict[str, Any]:
        """
        Current strategy state snapshot for the UI dashboard.
        Override to add strategy-specific indicators, positions, etc.
        """
        return {
            "strategy_id": self.strategy_id,
            "active": self._active,
            "mode": self.mode.value,
            "signal_count": self._signal_count,
            "last_signal_time": self._last_signal_time.isoformat() if self._last_signal_time else None,
            "params": self.params,
        }

    # ─── Lifecycle ────────────────────────────────────────────────────────────

    async def on_start(self) -> None:
        """Called once when the strategy is started. Override for one-time setup."""
        await self._emit_telemetry(TelemetryLevel.INFO, {"event": "strategy_started"})

    async def on_stop(self) -> None:
        """Called once when the strategy is stopped."""
        self._active = False
        await self._emit_telemetry(TelemetryLevel.INFO, {"event": "strategy_stopped"})

    # ─── Telemetry helper ─────────────────────────────────────────────────────

    async def _emit_telemetry(self, level: TelemetryLevel, payload: dict[str, Any]) -> None:
        event = TelemetryEvent(
            component_id=self.strategy_id,
            component_type=ComponentType.STRATEGY,
            channel="telemetry.strategy",
            level=level,
            mode=self.mode,
            payload=payload,
        )
        try:
            await publisher.publish_telemetry(event)
        except Exception:
            pass  # telemetry failures must never affect strategy execution
