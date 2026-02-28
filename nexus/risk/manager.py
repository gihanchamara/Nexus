"""
Risk Manager — orchestrates all risk checks for every signal.

Order of checks (fast-fail on first breach):
  1. Kill switch (env var NEXUS_KILL_SWITCH=1)
  2. Daily drawdown + portfolio drawdown
  3. Portfolio Greeks limits (delta, gamma, vega, theta)
  4. Position size (equity % or options premium %)

Emits risk.breach and risk.greeks events to the event bus on every check.
The dashboard subscribes to these for real-time risk monitoring.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, date
from typing import Any

from nexus.bus.publisher import publisher
from nexus.core.config import TradingMode, get_settings
from nexus.core.schemas import (
    ComponentType,
    Direction,
    PortfolioGreeks,
    RiskCheckResult,
    Signal,
    TelemetryEvent,
    TelemetryLevel,
)
from nexus.risk.greeks import GreeksMonitor, OptionsPosition
from nexus.risk.limits import DrawdownConfig, GreeksLimitConfig, LimitsChecker, PositionLimitConfig

logger = logging.getLogger(__name__)

COMPONENT_ID = "risk_manager"


class RiskManager:
    """
    Central risk gate. Every signal from every strategy passes through here
    before reaching the OMS.

    Usage:
        risk = RiskManager(portfolio_value=100_000)
        result = await risk.check(signal, current_price=450.0)
        if result.approved:
            ...route to OMS...
    """

    def __init__(
        self,
        initial_portfolio_value: float = 100_000.0,
        position_cfg: PositionLimitConfig | None = None,
        greeks_cfg: GreeksLimitConfig | None = None,
        drawdown_cfg: DrawdownConfig | None = None,
    ) -> None:
        self.portfolio_value = initial_portfolio_value
        self._peak_value = initial_portfolio_value
        self._daily_pnl: float = 0.0
        self._daily_reset_date: date = date.today()

        self.greeks_monitor = GreeksMonitor()
        self._limits = LimitsChecker(position_cfg, greeks_cfg, drawdown_cfg)

    # ─── Main check ───────────────────────────────────────────────────────────

    async def check(
        self,
        signal: Signal,
        current_price: float,
        estimated_delta_change: float = 0.0,
        estimated_vega_change: float = 0.0,
    ) -> RiskCheckResult:
        """
        Run all risk checks for a signal.
        Returns RiskCheckResult.approved=True only if ALL checks pass.
        Emits telemetry regardless of outcome.
        """
        settings = get_settings()

        # ── 1. Kill switch (highest priority) ────────────────────────────────
        if settings.nexus_kill_switch:
            result = RiskCheckResult(
                approved=False,
                reasons=["Kill switch is active (NEXUS_KILL_SWITCH=1)"],
            )
            await self._emit_breach("kill_switch", result.reasons, signal)
            return result

        self._refresh_daily_pnl()
        current_greeks = self.greeks_monitor.portfolio_greeks

        # ── 2. Drawdown limits ────────────────────────────────────────────────
        dd_result = self._limits.check_drawdown(
            self.portfolio_value, self._peak_value, self._daily_pnl
        )
        if not dd_result.approved:
            await self._emit_breach("drawdown", dd_result.reasons, signal)
            return dd_result

        # ── 3. Portfolio Greeks limits ────────────────────────────────────────
        greeks_result = self._limits.check_portfolio_greeks(
            current_greeks, estimated_delta_change, estimated_vega_change
        )
        if not greeks_result.approved:
            await self._emit_breach("greeks", greeks_result.reasons, signal)
            return greeks_result

        # ── 4. Position size ──────────────────────────────────────────────────
        is_options = bool(signal.metadata.get("legs"))
        if is_options:
            premium = abs(signal.metadata.get("net_credit", 0.0))
            qty = signal.suggested_quantity or 1.0
            size_result = self._limits.check_options_premium(
                premium, int(qty), self.portfolio_value
            )
        else:
            qty = signal.suggested_quantity or 0.0
            size_result = self._limits.check_equity_size(
                qty, current_price, self.portfolio_value
            )

        if not size_result.approved:
            await self._emit_breach("position_size", size_result.reasons, signal)
            return size_result

        # ── All passed ────────────────────────────────────────────────────────
        await self._emit_greeks_snapshot(current_greeks)
        logger.debug("[RiskManager] Signal %s approved for %s %s",
                     signal.signal_id, signal.direction.value, signal.symbol)
        return RiskCheckResult(
            approved=True,
            adjusted_quantity=size_result.adjusted_quantity,
        )

    # ─── P&L + portfolio value updates ───────────────────────────────────────

    def update_portfolio_value(self, value: float, realized_pnl_delta: float = 0.0) -> None:
        """Update portfolio value after fills. Called by PaperEngine/LiveBroker."""
        self.portfolio_value = value
        self._peak_value = max(self._peak_value, value)
        self._daily_pnl += realized_pnl_delta

    def _refresh_daily_pnl(self) -> None:
        """Reset daily P&L at the start of each new trading day."""
        today = date.today()
        if today != self._daily_reset_date:
            self._daily_pnl = 0.0
            self._daily_reset_date = today

    # ─── Greeks pass-through ──────────────────────────────────────────────────

    def update_options_position(self, position: OptionsPosition) -> None:
        self.greeks_monitor.update_options(position)

    def update_equity_position(self, symbol: str, qty: float,
                               avg_cost: float, price: float) -> None:
        self.greeks_monitor.update_equity(symbol, qty, avg_cost, price)

    def update_spot(self, symbol: str, price: float) -> None:
        self.greeks_monitor.update_spot(symbol, price)

    def update_iv(self, symbol: str, right: str, strike: float,
                  expiry: str, iv: float) -> None:
        self.greeks_monitor.update_iv(symbol, right, strike, expiry, iv)

    @property
    def portfolio_greeks(self) -> PortfolioGreeks:
        return self.greeks_monitor.portfolio_greeks

    # ─── Telemetry ────────────────────────────────────────────────────────────

    async def _emit_breach(
        self,
        breach_type: str,
        reasons: list[str],
        signal: Signal,
    ) -> None:
        logger.warning("[RiskManager] Breach (%s) for %s %s: %s",
                       breach_type, signal.direction.value, signal.symbol, reasons)
        settings = get_settings()
        event = TelemetryEvent(
            component_id=COMPONENT_ID,
            component_type=ComponentType.RISK_MANAGER,
            channel="risk.breach",
            level=TelemetryLevel.WARN,
            mode=TradingMode(settings.nexus_trading_mode),
            payload={
                "breach_type": breach_type,
                "reasons": reasons,
                "signal_id": signal.signal_id,
                "symbol": signal.symbol,
                "direction": signal.direction.value,
            },
        )
        try:
            await publisher.publish("risk.breach", event.to_stream_dict())
        except Exception:
            pass

    async def _emit_greeks_snapshot(self, greeks: PortfolioGreeks) -> None:
        settings = get_settings()
        event = TelemetryEvent(
            component_id=COMPONENT_ID,
            component_type=ComponentType.RISK_MANAGER,
            channel="risk.greeks",
            level=TelemetryLevel.INFO,
            mode=TradingMode(settings.nexus_trading_mode),
            payload={
                "greeks": greeks.model_dump(),
                "portfolio_value": self.portfolio_value,
                "daily_pnl": self._daily_pnl,
            },
        )
        try:
            await publisher.publish("risk.greeks", event.to_stream_dict())
        except Exception:
            pass

    def snapshot(self) -> dict[str, Any]:
        """Dashboard display: current risk state."""
        greeks = self.portfolio_greeks
        return {
            "portfolio_value": self.portfolio_value,
            "peak_value": self._peak_value,
            "daily_pnl": self._daily_pnl,
            "drawdown_pct": round(
                (self._peak_value - self.portfolio_value) / max(self._peak_value, 1) * 100, 2
            ),
            "portfolio_greeks": greeks.model_dump(),
            "open_options": self.greeks_monitor.open_options_count(),
            "open_equity": self.greeks_monitor.open_equity_count(),
            "kill_switch": get_settings().nexus_kill_switch,
        }
