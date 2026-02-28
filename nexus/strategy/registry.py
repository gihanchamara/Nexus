"""
Strategy Registry and Runner.

Responsibilities:
  1. Load all ACTIVE strategy configs from MySQL at startup
  2. Dynamically import the strategy class via class_path
  3. Instantiate strategies with their DB-stored parameters
  4. Subscribe strategy instances to the correct event bus channels
  5. Route events (bar, tick, news, options_chain) to the right instances
  6. Support hot-reload of parameters via strategy.param_update bus events
  7. Emit heartbeat telemetry per strategy

Event routing is intentional:
  - on_bar      ← market.bar.*  (all frequencies)
  - on_tick     ← market.tick
  - on_news     ← market.news
  - on_options_chain ← market.options_chain
  - param update ← strategy.param_update
"""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
from typing import Any

from nexus.bus.consumer import EventConsumer
from nexus.bus.publisher import publisher
from nexus.core.config import TradingMode, get_settings
from nexus.core.schemas import (
    BarData,
    ComponentType,
    NewsEvent,
    OptionsChainSnapshot,
    TelemetryEvent,
    TelemetryLevel,
    TickData,
)
from nexus.strategy.base import BaseStrategy

logger = logging.getLogger(__name__)

# Channels the registry listens to in order to route to strategies
_STRATEGY_CHANNELS = [
    "market.bar.5s",
    "market.bar.1m",
    "market.bar.5m",
    "market.bar.15m",
    "market.bar.30m",
    "market.bar.1h",
    "market.bar.1d",
    "market.tick",
    "market.news",
    "market.options_chain",
    "strategy.param_update",
]


def _dynamic_import(class_path: str) -> type[BaseStrategy]:
    """
    Import a strategy class from its dotted path.
    Example: 'nexus.strategy.strategies.ma_crossover.MovingAverageCrossover'
    """
    module_path, class_name = class_path.rsplit(".", 1)
    module = importlib.import_module(module_path)
    cls = getattr(module, class_name)
    if not issubclass(cls, BaseStrategy):
        raise TypeError(f"{class_path} must subclass BaseStrategy")
    return cls


class StrategyRegistry:
    """
    Central registry for all active strategy instances.

    Usage:
        registry = StrategyRegistry()
        await registry.load_strategies(strategy_configs)  # from DB or hardcoded list
        await registry.start()
        # ... runs until stopped
        await registry.stop()
    """

    def __init__(self) -> None:
        self._strategies: dict[str, BaseStrategy] = {}   # strategy_id → instance
        self._consumer = EventConsumer()
        self._running = False
        self._task: asyncio.Task[None] | None = None

    # ─── Loading ──────────────────────────────────────────────────────────────

    def load_strategies(self, configs: list[dict[str, Any]]) -> None:
        """
        Instantiate strategies from config dicts.

        Each config dict must have:
          id         : str   — unique strategy ID
          class_path : str   — dotted import path to the strategy class
          parameters : dict  — strategy parameters (matches DB column 'parameters')
          status     : str   — must be 'ACTIVE' to be loaded

        This is called with data from the MySQL strategy table (via Liquibase schema).
        """
        settings = get_settings()
        mode = TradingMode(settings.nexus_trading_mode)

        for cfg in configs:
            if cfg.get("status", "PAUSED") != "ACTIVE":
                continue
            sid = str(cfg["id"])
            class_path = cfg["class_path"]
            params = cfg.get("parameters") or {}
            if isinstance(params, str):
                params = json.loads(params)

            try:
                cls = _dynamic_import(class_path)
                instance = cls(strategy_id=sid, params=params, mode=mode)
                self._strategies[sid] = instance
                logger.info("Loaded strategy: %s (%s)", sid, class_path)
            except Exception as exc:
                logger.error("Failed to load strategy %s from %s: %s", sid, class_path, exc)

    def register(self, strategy: BaseStrategy) -> None:
        """Register a pre-instantiated strategy (useful for tests and scripts)."""
        self._strategies[strategy.strategy_id] = strategy
        logger.info("Registered strategy: %s", strategy.strategy_id)

    # ─── Lifecycle ────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Connect to event bus and start routing events to strategies."""
        await self._consumer.connect()
        for strategy in self._strategies.values():
            await strategy.on_start()

        self._running = True
        self._task = asyncio.create_task(self._event_loop(), name="strategy-registry")
        logger.info("StrategyRegistry started with %d strategies", len(self._strategies))

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
        for strategy in self._strategies.values():
            await strategy.on_stop()
        await self._consumer.close()
        logger.info("StrategyRegistry stopped")

    # ─── Event routing loop ───────────────────────────────────────────────────

    async def _event_loop(self) -> None:
        """
        Single async loop that reads all relevant channels and
        dispatches events to the correct strategy handler.
        """
        r = self._consumer._require_connection()
        streams: dict[str, str] = {ch: "$" for ch in _STRATEGY_CHANNELS}

        while self._running:
            try:
                results = await r.xread(streams, count=100, block=500)
                if not results:
                    await asyncio.sleep(0)
                    continue

                for stream_name, messages in results:
                    for msg_id, raw in messages:
                        streams[stream_name] = msg_id
                        await self._dispatch(stream_name, raw)

            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("StrategyRegistry event loop error: %s", exc)
                await asyncio.sleep(1)

    async def _dispatch(self, channel: str, raw: dict[str, str]) -> None:
        """Route a raw Redis message to the appropriate strategy handler."""
        try:
            if channel.startswith("market.bar"):
                bar = BarData(**json.loads(raw.get("model", json.dumps(raw))))
                await self._route_bar(bar)

            elif channel == "market.tick":
                tick = TickData(**{k: v for k, v in raw.items()})
                await self._route_tick(tick)

            elif channel == "market.news":
                news = NewsEvent(**{k: v for k, v in raw.items()})
                await self._route_news(news)

            elif channel == "market.options_chain":
                chain = OptionsChainSnapshot.model_validate_json(
                    raw.get("payload", "{}")
                )
                await self._route_options_chain(chain)

            elif channel == "strategy.param_update":
                strategy_id = raw.get("strategy_id", "")
                params_str = raw.get("params", "{}")
                params = json.loads(params_str)
                self.update_params(strategy_id, params)

        except Exception as exc:
            logger.debug("Dispatch error on %s: %s", channel, exc)

    # ─── Per-handler routing ─────────────────────────────────────────────────

    async def _route_bar(self, bar: BarData) -> None:
        tasks = [s.on_bar(bar) for s in self._strategies.values()]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _route_tick(self, tick: TickData) -> None:
        tasks = [s.on_tick(tick) for s in self._strategies.values()]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _route_news(self, news: NewsEvent) -> None:
        tasks = [s.on_news(news) for s in self._strategies.values()]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _route_options_chain(self, chain: OptionsChainSnapshot) -> None:
        tasks = [s.on_options_chain(chain) for s in self._strategies.values()]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    # ─── Hot-reload ───────────────────────────────────────────────────────────

    def update_params(self, strategy_id: str, params: dict[str, Any]) -> None:
        strategy = self._strategies.get(strategy_id)
        if strategy:
            strategy.update_params(params)
        else:
            logger.warning("param_update for unknown strategy_id: %s", strategy_id)

    # ─── Introspection ───────────────────────────────────────────────────────

    def state_snapshot(self) -> dict[str, Any]:
        """All strategy displayable states — consumed by dashboard."""
        return {sid: s.displayable_state for sid, s in self._strategies.items()}

    def list_strategies(self) -> list[str]:
        return list(self._strategies.keys())


# Module-level singleton
registry = StrategyRegistry()
