"""
Signal Router — the OMS entry point.

Subscribes to strategy.signal channel on the event bus.
For every signal received:
  1. Kill switch check
  2. Risk Manager check (drawdown + Greeks + position limits)
  3. Position sizing
  4. Order construction (single-leg or multi-leg)
  5. Route to Paper Engine (or Live Broker when mode=LIVE)

The router holds references to all downstream components and
coordinates the full order lifecycle in one async flow.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from nexus.bus.consumer import EventConsumer
from nexus.bus.publisher import publisher
from nexus.core.config import TradingMode, get_settings
from nexus.core.schemas import (
    ComponentType,
    Direction,
    Signal,
    TelemetryEvent,
    TelemetryLevel,
)
from nexus.oms.order_builder import OrderBuilder
from nexus.oms.paper_engine import PaperEngine
from nexus.oms.sizer import PositionSizer
from nexus.risk.manager import RiskManager

logger = logging.getLogger(__name__)

COMPONENT_ID = "signal_router"


class SignalRouter:
    """
    Async event loop that consumes strategy.signal and routes to execution.

    Usage:
        router = SignalRouter(risk_manager, paper_engine)
        await router.start()
        # runs until stopped
        await router.stop()
    """

    def __init__(
        self,
        risk_manager: RiskManager,
        paper_engine: PaperEngine,
        sizing_method: str = "fixed_pct",
        sizing_params: dict[str, Any] | None = None,
        source_channel: str = "strategy.signal",   # Phase 4: set to "ai.signal" when ensemble is active
        use_sentiment: bool = False,               # Phase 4: apply sentiment confidence modifier
    ) -> None:
        self._risk = risk_manager
        self._paper = paper_engine
        self._builder = OrderBuilder()
        self._sizer = PositionSizer()
        self._sizing_method = sizing_method
        self._sizing_params = sizing_params or {}
        self._source_channel = source_channel
        self._use_sentiment = use_sentiment

        self._consumer = EventConsumer()
        self._running = False
        self._task: asyncio.Task[None] | None = None
        self._processed: int = 0
        self._approved: int = 0
        self._rejected: int = 0

    async def start(self) -> None:
        await self._consumer.connect()
        # Create consumer group so signals are processed exactly once
        group = "oms-router"
        await self._consumer.ensure_group(self._source_channel, group)
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="oms-router")
        logger.info(
            "SignalRouter started (mode=%s, source=%s, sentiment=%s)",
            get_settings().nexus_trading_mode, self._source_channel, self._use_sentiment,
        )

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
        await self._consumer.close()

    # ─── Main processing loop ─────────────────────────────────────────────────

    async def _loop(self) -> None:
        async for msg_id, raw in self._consumer.read_group(
            self._source_channel, "oms-router", "router-worker-1"
        ):
            try:
                await self._process(raw)
                await self._consumer.ack(self._source_channel, "oms-router", msg_id)
                self._processed += 1
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("SignalRouter processing error: %s", exc, exc_info=True)

    async def _process(self, raw: dict[str, str]) -> None:
        """Full pipeline: signal → risk → size → build → execute."""
        settings = get_settings()

        # ── Parse signal ──────────────────────────────────────────────────────
        try:
            payload_str = raw.get("payload", raw.get("model", "{}"))
            if isinstance(payload_str, str):
                data = json.loads(payload_str)
            else:
                data = {k: v for k, v in raw.items()}
            signal = Signal.model_validate(data)
        except Exception as exc:
            logger.warning("Failed to parse signal: %s — raw: %s", exc, raw)
            return

        logger.debug("[Router] Signal received: %s %s %s conf=%.2f",
                     signal.signal_id, signal.direction.value,
                     signal.symbol, signal.confidence)

        # ── HOLD signals are no-ops ───────────────────────────────────────────
        if signal.direction == Direction.HOLD:
            return

        # ── Phase 4: Sentiment confidence modifier ────────────────────────────
        if self._use_sentiment:
            try:
                from nexus.ai.sentiment import sentiment_analyzer
                multiplier = sentiment_analyzer.confidence_multiplier(
                    signal.symbol, signal.direction.value
                )
                signal.confidence = min(1.0, signal.confidence * multiplier)
                logger.debug("[Router] Sentiment modifier %.2f → conf=%.2f", multiplier, signal.confidence)
            except Exception:
                pass  # sentiment failures never block order flow

        # ── Kill switch (fast path — no risk manager overhead) ────────────────
        if settings.nexus_kill_switch:
            await self._emit_telemetry(
                TelemetryLevel.WARN,
                {"event": "signal_blocked", "reason": "kill_switch", "signal_id": signal.signal_id},
            )
            self._rejected += 1
            return

        # ── Current price lookup (needed for risk check + equity sizing) ──────
        current_price = self._paper._current_prices.get(signal.symbol, 0.0)

        # ── Risk check ────────────────────────────────────────────────────────
        risk_result = await self._risk.check(signal, current_price)
        if not risk_result.approved:
            self._rejected += 1
            await self._emit_telemetry(
                TelemetryLevel.WARN,
                {
                    "event": "signal_rejected",
                    "signal_id": signal.signal_id,
                    "symbol": signal.symbol,
                    "reasons": risk_result.reasons,
                },
            )
            return

        # ── Position sizing ───────────────────────────────────────────────────
        quantity = self._compute_quantity(signal, current_price, risk_result.adjusted_quantity)
        if quantity <= 0:
            logger.warning("[Router] Computed zero quantity for signal %s", signal.signal_id)
            return

        # ── Build order ───────────────────────────────────────────────────────
        order = self._builder.build(signal, quantity)

        # ── Execute (paper or live) ───────────────────────────────────────────
        mode = TradingMode(settings.nexus_trading_mode)
        if mode == TradingMode.PAPER:
            fills = await self._paper.submit(order)
        elif mode == TradingMode.LIVE:
            # LIVE mode guard: placeholder for live broker integration (Phase 5)
            logger.error("[Router] LIVE mode not yet enabled — routing to paper as fallback")
            fills = await self._paper.submit(order)
        else:
            logger.warning("[Router] Unknown mode %s — skipping", mode)
            return

        self._approved += 1

        # ── Update risk manager with new position ─────────────────────────────
        if fills:
            self._risk.update_portfolio_value(self._paper.portfolio_value)

        await self._emit_telemetry(
            TelemetryLevel.INFO,
            {
                "event": "signal_executed",
                "signal_id": signal.signal_id,
                "symbol": signal.symbol,
                "quantity": quantity,
                "fills": len(fills),
                "portfolio_value": self._paper.portfolio_value,
            },
        )

    # ─── Sizing dispatch ──────────────────────────────────────────────────────

    def _compute_quantity(
        self,
        signal: Signal,
        current_price: float,
        adjusted_quantity: float | None,
    ) -> float:
        """Delegate to PositionSizer based on configured method."""
        # Risk manager override
        if adjusted_quantity is not None:
            return adjusted_quantity

        # Strategy suggested quantity (explicit)
        if signal.suggested_quantity:
            return PositionSizer.confidence_scaled(
                signal.suggested_quantity, signal.confidence
            )

        portfolio_value = self._paper.portfolio_value
        params = self._sizing_params
        conf = signal.confidence

        if self._sizing_method == "fixed_pct":
            return PositionSizer.fixed_pct(
                portfolio_value, current_price,
                risk_pct=params.get("risk_pct", 0.02),
                confidence=conf,
            )
        elif self._sizing_method == "kelly":
            return PositionSizer.kelly(
                portfolio_value, current_price,
                win_rate=params.get("win_rate", 0.55),
                avg_win=params.get("avg_win", 0.04),
                avg_loss=params.get("avg_loss", 0.02),
                confidence=conf,
            )
        elif self._sizing_method == "atr_based":
            return PositionSizer.atr_based(
                portfolio_value, current_price,
                atr=params.get("atr", current_price * 0.015),
                confidence=conf,
            )
        else:
            return PositionSizer.fixed_pct(portfolio_value, current_price, confidence=conf)

    # ─── Telemetry ────────────────────────────────────────────────────────────

    async def _emit_telemetry(self, level: TelemetryLevel, payload: dict) -> None:
        settings = get_settings()
        event = TelemetryEvent(
            component_id=COMPONENT_ID,
            component_type=ComponentType.ORDER_MANAGER,
            channel="telemetry.oms",
            level=level,
            mode=TradingMode(settings.nexus_trading_mode),
            payload=payload,
        )
        try:
            await publisher.publish_telemetry(event)
        except Exception:
            pass

    def snapshot(self) -> dict[str, Any]:
        return {
            "processed": self._processed,
            "approved": self._approved,
            "rejected": self._rejected,
            "approval_rate": (
                round(self._approved / self._processed, 3) if self._processed else 0
            ),
            "sizing_method": self._sizing_method,
            "mode": get_settings().nexus_trading_mode,
            "source_channel": self._source_channel,
            "sentiment_enabled": self._use_sentiment,
        }
