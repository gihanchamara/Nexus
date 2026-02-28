"""
Iron Condor — income options strategy.

Sells an OTM call spread + OTM put spread simultaneously.
Profits when the underlying stays within the short strikes at expiry.

Entry logic (ALL conditions must be true):
  1. IV Rank (IVR) > ivr_threshold  — premium is expensive, good time to sell
  2. No existing condor position for this symbol
  3. Suitable expiry found in dte_min..dte_max range
  4. Options with target delta found on both call and put sides

Exit logic (checked on every bar and options chain update):
  - Profit target: when position value drops to (1 - profit_target_pct) of credit
  - Stop loss: when position value reaches stop_loss_multiplier × credit
  - Expiry management: auto-close at T-5 days (managed by ExpiryManager separately)

Structure (4 legs):
  SELL  OTM call  (short_delta ≈ 0.20)  ← short strike
  BUY   OTM call  (short_strike + wing_width)  ← long strike (protection)
  SELL  OTM put   (short_delta ≈ -0.20) ← short strike
  BUY   OTM put   (short_strike - wing_width)  ← long strike (protection)

Max profit = net credit received
Max loss   = (wing_width × 100) - net_credit  (per contract)

Displayable parameters (editable in UI, hot-reloadable):
  symbol             : str   — underlying (e.g. 'SPY', 'QQQ', 'AAPL')
  dte_min            : int   — min days to expiry at entry (default: 25)
  dte_max            : int   — max days to expiry at entry (default: 50)
  short_delta        : float — target delta of short legs (default: 0.20)
  wing_width         : int   — strikes between short and long leg (default: 5)
  ivr_threshold      : float — min IVR for entry (default: 50.0)
  contracts          : int   — number of condors (default: 1)
  profit_target_pct  : float — close at this % of max profit (default: 0.50)
  stop_loss_mult     : float — close when loss = this × credit (default: 2.0)

DB class_path: nexus.strategy.strategies.iron_condor.IronCondorStrategy
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from nexus.core.schemas import (
    BarData,
    Direction,
    OptionsChainSnapshot,
    OptionsContract,
    Signal,
    TelemetryLevel,
)
from nexus.strategy.base import BaseStrategy

logger = logging.getLogger(__name__)


class OpenCondor:
    """Tracks a live iron condor position."""

    def __init__(
        self,
        legs: list[dict],
        entry_credit: float,
        expiry: str,
        profit_target: float,
        stop_loss: float,
    ) -> None:
        self.legs = legs
        self.entry_credit = entry_credit
        self.expiry = expiry
        self.profit_target = profit_target   # credit × profit_target_pct
        self.stop_loss = stop_loss           # credit × stop_loss_mult
        self.entry_time = datetime.utcnow()

    @property
    def short_call_strike(self) -> float:
        return next(l["strike"] for l in self.legs if l["action"] == "SELL" and l["right"] == "C")

    @property
    def short_put_strike(self) -> float:
        return next(l["strike"] for l in self.legs if l["action"] == "SELL" and l["right"] == "P")


class IronCondorStrategy(BaseStrategy):
    """
    Iron Condor income strategy for high-IVR environments.
    Designed for index ETFs (SPY, QQQ, IWM) or high-liquidity stocks.
    """

    def __init__(self, strategy_id: str, params: dict[str, Any], **kwargs: Any) -> None:
        super().__init__(strategy_id, params, **kwargs)
        self._apply_params()
        self._open_condor: OpenCondor | None = None
        self._last_chain: OptionsChainSnapshot | None = None
        self._last_spot: float = 0.0

    def _apply_params(self) -> None:
        self.symbol: str = self._params.get("symbol", "SPY")
        self.dte_min: int = int(self._params.get("dte_min", 25))
        self.dte_max: int = int(self._params.get("dte_max", 50))
        self.short_delta: float = float(self._params.get("short_delta", 0.20))
        self.wing_width: int = int(self._params.get("wing_width", 5))
        self.ivr_threshold: float = float(self._params.get("ivr_threshold", 50.0))
        self.contracts: int = int(self._params.get("contracts", 1))
        self.profit_target_pct: float = float(self._params.get("profit_target_pct", 0.50))
        self.stop_loss_mult: float = float(self._params.get("stop_loss_mult", 2.0))

    def update_params(self, new_params: dict[str, Any]) -> None:
        super().update_params(new_params)
        self._apply_params()

    # ─── Bar handler — used for spot price tracking and exit checks ──────────

    async def on_bar(self, bar: BarData) -> None:
        if bar.symbol != self.symbol:
            return
        self._last_spot = bar.close
        if self._open_condor:
            await self._check_price_exit(bar.close)

    # ─── Options chain handler — main entry + chain-based exit ───────────────

    async def on_options_chain(self, chain: OptionsChainSnapshot) -> None:
        if chain.symbol != self.symbol:
            return
        self._last_chain = chain
        self._last_spot = chain.spot_price

        # Priority 1: check exit on live chain prices
        if self._open_condor:
            await self._check_chain_exit(chain)
            return

        # Priority 2: try to enter a new condor
        await self._attempt_entry(chain)

    # ─── Entry logic ──────────────────────────────────────────────────────────

    async def _attempt_entry(self, chain: OptionsChainSnapshot) -> None:
        ivr = chain.iv_rank or 0.0

        if ivr < self.ivr_threshold:
            await self._emit_telemetry(TelemetryLevel.DEBUG, {
                "event": "entry_skipped",
                "reason": f"IVR {ivr:.1f} < threshold {self.ivr_threshold}",
                "ivr": ivr,
                "spot": chain.spot_price,
            })
            return

        legs = self._select_strikes(chain)
        if not legs:
            await self._emit_telemetry(TelemetryLevel.WARN, {
                "event": "entry_skipped",
                "reason": "Could not find suitable strikes",
                "ivr": ivr,
                "spot": chain.spot_price,
                "dte_range": f"{self.dte_min}–{self.dte_max}",
            })
            return

        net_credit = self._compute_net_credit(legs)
        if net_credit <= 0:
            await self._emit_telemetry(TelemetryLevel.WARN, {
                "event": "entry_skipped",
                "reason": f"Could not achieve net credit (got {net_credit:.2f})",
            })
            return

        max_loss_per_contract = (self.wing_width * 100) - (net_credit * 100)
        profit_target = net_credit * self.profit_target_pct
        stop_loss = net_credit * self.stop_loss_mult

        await self.emit_signal(Signal(
            symbol=self.symbol,
            direction=Direction.SELL,   # net short (selling premium)
            confidence=min(1.0, ivr / 100.0),
            strategy_id=self.strategy_id,
            mode=self.mode,
            suggested_quantity=float(self.contracts),
            metadata={
                "strategy_type": "iron_condor",
                "legs": legs,
                "net_credit": round(net_credit, 2),
                "max_profit": round(net_credit * 100 * self.contracts, 2),
                "max_loss": round(max_loss_per_contract * self.contracts, 2),
                "break_even_upper": round(legs[0]["strike"] + net_credit, 2),  # short call + credit
                "break_even_lower": round(legs[2]["strike"] - net_credit, 2),  # short put - credit
                "ivr": ivr,
                "dte": legs[0].get("dte", 0),
                "profit_target": round(profit_target, 2),
                "stop_loss": round(stop_loss, 2),
            },
        ))

        # Track open condor (confirmed after OMS fills)
        # In production this would be updated from order.filled event
        self._open_condor = OpenCondor(
            legs=legs,
            entry_credit=net_credit,
            expiry=legs[0]["expiry"],
            profit_target=profit_target,
            stop_loss=stop_loss,
        )

        await self._emit_telemetry(TelemetryLevel.INFO, {
            "event": "condor_entered",
            "ivr": ivr,
            "net_credit": round(net_credit, 2),
            "short_call": legs[0]["strike"],
            "short_put": legs[2]["strike"],
            "expiry": legs[0]["expiry"],
        })

    # ─── Strike selection ─────────────────────────────────────────────────────

    def _select_strikes(self, chain: OptionsChainSnapshot) -> list[dict] | None:
        """
        Find the four strikes forming the iron condor.
        Returns None if any required contract is not found.
        """
        # Short call: nearest to target delta OTM
        short_call = chain.nearest_delta(self.short_delta, "C", self.dte_min, self.dte_max)
        if not short_call:
            return None

        # Short put: nearest to -target_delta (puts have negative delta)
        short_put = chain.nearest_delta(-self.short_delta, "P", self.dte_min, self.dte_max)
        if not short_put:
            return None

        # Use same expiry for all legs (classic condor)
        expiry = short_call.expiry

        # Long call: wing_width strikes above short call
        long_call = chain.nearest_strike(
            short_call.strike + self.wing_width, "C", expiry
        )
        if not long_call:
            return None

        # Long put: wing_width strikes below short put
        long_put = chain.nearest_strike(
            short_put.strike - self.wing_width, "P", expiry
        )
        if not long_put:
            return None

        # Sanity check: put spread must be below call spread
        if short_put.strike >= short_call.strike:
            logger.warning("Iron Condor: put short strike >= call short strike (inverted spread)")
            return None

        def leg_dict(contract: OptionsContract, action: str) -> dict:
            return {
                "action": action,
                "right": contract.right,
                "strike": contract.strike,
                "expiry": contract.expiry,
                "dte": contract.dte,
                "quantity": self.contracts,
                "bid": contract.bid,
                "ask": contract.ask,
                "mid": round(contract.mid, 2),
                "conid": contract.conid,
            }

        return [
            leg_dict(short_call, "SELL"),
            leg_dict(long_call, "BUY"),
            leg_dict(short_put, "SELL"),
            leg_dict(long_put, "BUY"),
        ]

    def _compute_net_credit(self, legs: list[dict]) -> float:
        """Net credit per contract. SELL legs generate credit, BUY legs cost premium."""
        net = 0.0
        for leg in legs:
            mid = leg.get("mid", 0.0)
            if leg["action"] == "SELL":
                net += mid
            else:
                net -= mid
        return round(net, 2)

    # ─── Exit logic ───────────────────────────────────────────────────────────

    async def _check_price_exit(self, spot: float) -> None:
        """Simple price-based exit: close if spot breaches short strikes by margin."""
        if not self._open_condor:
            return
        condor = self._open_condor
        # Emit a warning if spot is within 1% of a short strike
        call_dist = (condor.short_call_strike - spot) / spot
        put_dist = (spot - condor.short_put_strike) / spot
        if call_dist < 0.01 or put_dist < 0.01:
            await self._emit_telemetry(TelemetryLevel.WARN, {
                "event": "condor_at_risk",
                "spot": spot,
                "short_call": condor.short_call_strike,
                "short_put": condor.short_put_strike,
                "call_distance_pct": round(call_dist * 100, 2),
                "put_distance_pct": round(put_dist * 100, 2),
            })

    async def _check_chain_exit(self, chain: OptionsChainSnapshot) -> None:
        """
        Chain-based exit: compute current value of condor and check
        profit target / stop loss.
        """
        if not self._open_condor:
            return
        condor = self._open_condor

        # Compute current value of condor (what it would cost to close)
        current_value = self._current_condor_value(chain, condor)
        if current_value is None:
            return

        # Cost to close = current_value (BUY back what we sold)
        remaining_credit = condor.entry_credit - current_value

        if remaining_credit >= condor.profit_target:
            await self._emit_close_signal(chain, "profit_target",
                                          f"Credit retained: {remaining_credit:.2f}")
        elif current_value >= condor.stop_loss:
            await self._emit_close_signal(chain, "stop_loss",
                                          f"Loss: {current_value - condor.entry_credit:.2f}")

    def _current_condor_value(
        self, chain: OptionsChainSnapshot, condor: OpenCondor
    ) -> float | None:
        """Estimate current cost to close the condor (buy back all sold options)."""
        total = 0.0
        for leg in condor.legs:
            contract = chain.nearest_strike(leg["strike"], leg["right"], condor.expiry)
            if not contract:
                return None
            if leg["action"] == "SELL":
                total += contract.mid   # cost to buy back
            else:
                total -= contract.mid  # credit to sell back protection
        return max(0.0, round(total, 2))

    async def _emit_close_signal(
        self, chain: OptionsChainSnapshot, reason: str, detail: str
    ) -> None:
        """Emit a BUY signal to close the condor (reverse all legs)."""
        if not self._open_condor:
            return
        condor = self._open_condor

        # Reverse each leg to close
        close_legs = [
            {**leg, "action": "BUY" if leg["action"] == "SELL" else "SELL"}
            for leg in condor.legs
        ]

        await self.emit_signal(Signal(
            symbol=self.symbol,
            direction=Direction.BUY,   # closing net-short position
            confidence=1.0,
            strategy_id=self.strategy_id,
            mode=self.mode,
            suggested_quantity=float(self.contracts),
            metadata={
                "strategy_type": "iron_condor_close",
                "legs": close_legs,
                "close_reason": reason,
                "close_detail": detail,
            },
        ))

        await self._emit_telemetry(TelemetryLevel.INFO, {
            "event": "condor_close_signal",
            "reason": reason,
            "detail": detail,
        })
        self._open_condor = None

    # ─── Displayable state ────────────────────────────────────────────────────

    @property
    def displayable_state(self) -> dict[str, Any]:
        state = super().displayable_state
        state.update({
            "symbol": self.symbol,
            "ivr_threshold": self.ivr_threshold,
            "open_condor": {
                "expiry": self._open_condor.expiry,
                "entry_credit": self._open_condor.entry_credit,
                "short_call": self._open_condor.short_call_strike,
                "short_put": self._open_condor.short_put_strike,
                "profit_target": self._open_condor.profit_target,
                "stop_loss": self._open_condor.stop_loss,
                "entry_time": self._open_condor.entry_time.isoformat(),
            } if self._open_condor else None,
            "last_spot": self._last_spot,
            "last_chain_time": (
                self._last_chain.timestamp.isoformat() if self._last_chain else None
            ),
        })
        return state
