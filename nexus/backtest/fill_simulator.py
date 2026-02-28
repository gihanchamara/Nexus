"""
Fill Simulator — Phase 3.

Simulates realistic trade fills for the backtest engine.

Equity fill methods (configurable via FillMethod):
  NEXT_BAR_OPEN — fill at next bar's open (lookahead-free; most conservative)
  CLOSE         — fill at current bar's close (slightly optimistic)
  VWAP          — fill at bar's VWAP/average (realistic for large orders)
  MID_PRICE     — fill at (high + low) / 2 (used for intraday; rare)

Options fill:
  All options fills use mid-price from the synthetic chain snapshot.
  Multi-leg fills (Iron Condor, Spread) iterate over each leg.

Commission model:
  Identical to PaperEngine to ensure consistent cost simulation:
  Equity : $0.005/share, min $1.00, max 1% of trade value
  Options: $0.65/contract, min $1.00

Slippage model:
  Not explicitly modelled (next-bar-open already incorporates gap risk).
  For options, the bid-ask spread in the synthetic chain is the implicit slippage.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from nexus.core.schemas import BarData, Direction, OptionsChainSnapshot, Signal

logger = logging.getLogger(__name__)


class FillMethod(str, Enum):
    """How equity fills are simulated in backtesting."""
    NEXT_BAR_OPEN = "next_bar_open"   # next bar open — lookahead-free
    CLOSE         = "close"           # same-bar close — slightly optimistic
    VWAP          = "vwap"            # same-bar VWAP/average
    MID_PRICE     = "mid"             # (high + low) / 2


@dataclass
class FillResult:
    """Result of a single simulated fill."""
    success: bool
    fill_price: float = 0.0
    fill_time: datetime = field(default_factory=datetime.utcnow)
    quantity: float = 0.0
    commission: float = 0.0
    reason: str = ""                   # rejection reason if not success
    leg_fills: list[dict[str, Any]] = field(default_factory=list)  # for multi-leg

    @property
    def net_cost(self) -> float:
        """Total cost including commission (positive = debit, negative = credit)."""
        return self.fill_price * self.quantity + self.commission


class FillSimulator:
    """
    Stateless fill simulator. All methods are pure functions of their inputs.

    Used by BacktestEngine to route signals to fills.
    """

    def __init__(self, commission_model: str = "ibkr_equity") -> None:
        self._commission_model = commission_model

    # ─── Equity fills ─────────────────────────────────────────────────────────

    def simulate_equity_fill(
        self,
        signal: Signal,
        bar: BarData,
        method: FillMethod = FillMethod.NEXT_BAR_OPEN,
    ) -> FillResult:
        """
        Simulate a single-leg equity fill from a signal + bar.

        Args:
            signal : the strategy signal (BUY or SELL, quantity)
            bar    : the bar at which the fill occurs (next bar for NEXT_BAR_OPEN)
            method : fill method (default: NEXT_BAR_OPEN)

        Returns:
            FillResult with fill_price, quantity, commission
        """
        quantity = signal.suggested_quantity or 1.0

        fill_price = self._equity_price(bar, method)
        if fill_price <= 0:
            return FillResult(
                success=False,
                reason=f"No valid price for fill method {method.value}",
            )

        commission = self.equity_commission(quantity, fill_price)

        return FillResult(
            success=True,
            fill_price=round(fill_price, 4),
            fill_time=bar.timestamp,
            quantity=quantity,
            commission=commission,
        )

    def simulate_options_fill(
        self,
        signal: Signal,
        chain: OptionsChainSnapshot | None,
        method: FillMethod = FillMethod.CLOSE,
    ) -> FillResult:
        """
        Simulate a multi-leg options fill using the synthetic chain.

        For a multi-leg signal (Iron Condor, Spread, etc.), metadata['legs']
        contains the leg specifications. Each leg is filled at mid-price from
        the synthetic chain.

        Args:
            signal : strategy signal with metadata['legs'] for multi-leg orders
            chain  : synthetic OptionsChainSnapshot for mid-price lookup
            method : ignored for options (always mid-price)

        Returns:
            FillResult with net_credit/debit as fill_price and all leg fills
        """
        legs_raw = signal.metadata.get("legs", [])

        if not legs_raw:
            # Single-leg options fill (long call, long put)
            return self._fill_single_option(signal, chain)

        # Multi-leg combo fill
        return self._fill_combo(signal, legs_raw, chain)

    # ─── Private fill helpers ─────────────────────────────────────────────────

    def _equity_price(self, bar: BarData, method: FillMethod) -> float:
        """Extract the fill price from a bar based on method."""
        if method == FillMethod.NEXT_BAR_OPEN:
            return bar.open
        elif method == FillMethod.CLOSE:
            return bar.close
        elif method == FillMethod.VWAP:
            return bar.average if bar.average > 0 else (bar.high + bar.low + bar.close) / 3.0
        elif method == FillMethod.MID_PRICE:
            return (bar.high + bar.low) / 2.0
        return bar.close

    def _fill_single_option(
        self,
        signal: Signal,
        chain: OptionsChainSnapshot | None,
    ) -> FillResult:
        """Fill a single options leg at mid-price."""
        if chain is None:
            return FillResult(success=False, reason="No options chain available")

        right = signal.metadata.get("right")
        strike = float(signal.metadata.get("strike", 0.0))
        expiry = str(signal.metadata.get("expiry", ""))

        if not right or not strike or not expiry:
            return FillResult(success=False, reason="Missing options metadata (right/strike/expiry)")

        contract = chain.nearest_strike(strike, right, expiry)
        if contract is None:
            return FillResult(
                success=False,
                reason=f"No chain contract for {right} {strike} {expiry}",
            )

        mid = contract.mid
        if mid <= 0:
            return FillResult(success=False, reason="Zero mid-price for options contract")

        quantity = signal.suggested_quantity or 1.0
        commission = self.options_commission(int(quantity))

        return FillResult(
            success=True,
            fill_price=round(mid, 2),
            fill_time=chain.timestamp,
            quantity=quantity,
            commission=commission,
            leg_fills=[{
                "right": right, "strike": strike, "expiry": expiry,
                "action": signal.direction.value, "mid": mid,
            }],
        )

    def _fill_combo(
        self,
        signal: Signal,
        legs_raw: list[dict[str, Any]],
        chain: OptionsChainSnapshot | None,
    ) -> FillResult:
        """Fill a multi-leg options combo (Iron Condor, spread, etc.) at mid-price."""
        net_premium = 0.0
        total_commission = 0.0
        leg_fills: list[dict[str, Any]] = []

        for leg in legs_raw:
            right = leg.get("right", "")
            strike = float(leg.get("strike", 0.0))
            expiry = str(leg.get("expiry", ""))
            action = leg.get("action", "BUY")
            qty = int(leg.get("quantity", signal.suggested_quantity or 1))

            # Try to find mid from chain first; fall back to leg bid/ask
            mid: float | None = None
            if chain is not None:
                contract = chain.nearest_strike(strike, right, expiry)
                if contract:
                    mid = contract.mid

            if mid is None or mid <= 0:
                # Fall back to bid/ask from signal metadata
                bid = float(leg.get("bid", 0.0))
                ask = float(leg.get("ask", 0.0))
                mid = (bid + ask) / 2.0 if bid and ask else float(leg.get("mid", 0.0))

            if mid <= 0:
                logger.warning(
                    "[FillSimulator] No mid-price for leg %s %s %.0f %s — skipping",
                    action, right, strike, expiry,
                )
                continue

            commission = self.options_commission(qty)
            total_commission += commission

            # Net premium: SELL legs receive premium, BUY legs pay
            if action.upper() == "SELL":
                net_premium += mid * qty
            else:
                net_premium -= mid * qty

            leg_fills.append({
                "action": action, "right": right, "strike": strike,
                "expiry": expiry, "quantity": qty, "mid": round(mid, 2),
            })

        if not leg_fills:
            return FillResult(
                success=False,
                reason="All combo legs failed to fill",
            )

        fill_time = chain.timestamp if chain else datetime.utcnow()

        return FillResult(
            success=True,
            fill_price=round(net_premium, 2),   # positive = net credit, negative = net debit
            fill_time=fill_time,
            quantity=float(signal.suggested_quantity or 1),
            commission=total_commission,
            leg_fills=leg_fills,
        )

    # ─── Commission model (matches PaperEngine exactly) ───────────────────────

    def equity_commission(self, qty: float, price: float = 0.0) -> float:
        """
        IBKR equity commission: $0.005/share, min $1.00, max 1% of trade value.
        Matches nexus.oms.paper_engine.ibkr_equity_commission exactly.
        """
        if self._commission_model == "zero":
            return 0.0
        commission = abs(qty) * 0.005
        commission = max(commission, 1.00)
        if price > 0:
            commission = min(commission, abs(qty) * price * 0.01)
        return round(commission, 4)

    def options_commission(self, contracts: int) -> float:
        """
        IBKR options commission: $0.65/contract, min $1.00.
        Matches nexus.oms.paper_engine.ibkr_options_commission exactly.
        """
        if self._commission_model == "zero":
            return 0.0
        commission = abs(contracts) * 0.65
        return round(max(commission, 1.00), 4)
