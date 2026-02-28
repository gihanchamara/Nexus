"""
Order Builder.

Translates a Signal + quantity into concrete OrderRequest(s).

Supports:
  Single-leg  — equity BUY/SELL, long call/put
  Multi-leg   — any combo using OptionsLeg list in Signal.metadata['legs']
                Iron Condor (4 legs), Spread (2 legs), Calendar (2 legs), etc.
  Bracket     — entry + stop-loss + take-profit

Options rules enforced here:
  - Options orders ALWAYS use LMT (never MKT) — market orders on options
    have unacceptably wide fills due to low liquidity.
  - Multi-leg orders build an IBKR-compatible ComboOrder description
    (used by paper engine and live broker identically).
  - Net credit iron condors: limit price = net credit (submit as negative
    for BUY of the combo, which is the IBKR convention).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from nexus.core.schemas import (
    Direction,
    OptionsLeg,
    OrderRequest,
    OrderType,
    Signal,
)

logger = logging.getLogger(__name__)

# Maximum slippage allowed on options multi-leg orders (as % of mid)
_OPTIONS_SLIPPAGE_PCT = 0.02   # 2%


@dataclass
class ComboOrder:
    """
    Multi-leg options order (IBKR BAG / combo order).

    For live trading, this maps to ib_insync Contract(secType='BAG')
    with a ComboLeg for each leg.

    For paper trading, each leg is filled independently at mid-price.
    """
    symbol: str
    legs: list[OptionsLeg]
    limit_price: float        # net credit (negative) or debit (positive)
    quantity: int = 1
    time_in_force: str = "DAY"
    strategy_id: str | None = None
    signal_id: str | None = None
    session_id: int | None = None

    @property
    def is_credit(self) -> bool:
        return self.limit_price < 0

    @property
    def net_premium(self) -> float:
        """Recalculate net from current leg mids (for verification)."""
        net = 0.0
        for leg in self.legs:
            if leg.action == "SELL":
                net += leg.mid
            else:
                net -= leg.mid
        return net

    def to_display(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "type": "multi_leg",
            "legs": [
                {
                    "action": l.action,
                    "right": l.right,
                    "strike": l.strike,
                    "expiry": l.expiry,
                    "qty": l.quantity,
                    "mid": l.mid,
                }
                for l in self.legs
            ],
            "limit_price": self.limit_price,
            "quantity": self.quantity,
            "net_credit": -self.limit_price if self.is_credit else 0.0,
        }


class OrderBuilder:
    """
    Builds single-leg OrderRequests or multi-leg ComboOrders from a Signal.

    The type of order is determined by Signal.metadata:
      - metadata empty or strategy_type='equity' → single-leg OrderRequest
      - metadata['legs'] present → ComboOrder (multi-leg options)
      - metadata['bracket'] present → bracket OrderRequest
    """

    def build(
        self,
        signal: Signal,
        quantity: float,
    ) -> OrderRequest | ComboOrder:
        """
        Main entry point. Returns either an OrderRequest or ComboOrder.
        The Router and PaperEngine both handle both types.
        """
        legs = signal.metadata.get("legs", [])
        if legs:
            return self._build_combo(signal, legs, quantity)
        elif signal.metadata.get("bracket"):
            return self._build_bracket(signal, quantity)
        else:
            return self._build_single(signal, quantity)

    # ─── Single-leg (equity, long call, long put) ─────────────────────────────

    def _build_single(self, signal: Signal, quantity: float) -> OrderRequest:
        """
        Standard single-leg order.
        Equity: MKT order (fast fill, tight spreads).
        Options single-leg (long call/put): LMT at mid-price.
        """
        is_options = signal.metadata.get("right") in ("C", "P")
        order_type = OrderType.LMT if is_options else OrderType.MKT
        limit_price: float | None = None

        if is_options:
            bid = signal.metadata.get("bid", 0.0)
            ask = signal.metadata.get("ask", 0.0)
            if bid and ask:
                limit_price = round((bid + ask) / 2.0, 2)

        return OrderRequest(
            symbol=signal.symbol,
            side=signal.direction,
            order_type=order_type,
            quantity=quantity,
            limit_price=limit_price,
            time_in_force=signal.metadata.get("time_in_force", "DAY"),
            strategy_id=signal.strategy_id,
            signal_id=signal.signal_id,
            session_id=signal.metadata.get("session_id"),
        )

    # ─── Bracket (entry + stop + target) ─────────────────────────────────────

    def _build_bracket(self, signal: Signal, quantity: float) -> OrderRequest:
        """Bracket order: fills as LMT entry + attached stop and target."""
        bracket_cfg = signal.metadata.get("bracket", {})
        return OrderRequest(
            symbol=signal.symbol,
            side=signal.direction,
            order_type=OrderType.BRACKET,
            quantity=quantity,
            limit_price=bracket_cfg.get("entry_price"),
            stop_price=bracket_cfg.get("stop_price"),
            time_in_force="GTC",
            strategy_id=signal.strategy_id,
            signal_id=signal.signal_id,
        )

    # ─── Multi-leg combo (Iron Condor, Spread, Calendar, etc.) ───────────────

    def _build_combo(
        self,
        signal: Signal,
        raw_legs: list[dict[str, Any]],
        quantity: float,
    ) -> ComboOrder:
        """
        Build a multi-leg ComboOrder from Signal.metadata['legs'].

        Each leg dict must have: action, right, strike, expiry, quantity, bid, ask
        (optionally conid).

        Limit price = net credit (negative) or debit (positive).
        Adds a small offset toward our favour to improve fill probability:
          credit spread: limit = net_credit - slippage  (slightly worse for us)
          debit spread:  limit = net_debit + slippage
        """
        legs: list[OptionsLeg] = []
        net = 0.0

        for raw in raw_legs:
            leg = OptionsLeg(
                action=raw["action"],
                right=raw["right"],
                strike=float(raw["strike"]),
                expiry=str(raw["expiry"]),
                quantity=int(raw.get("quantity", quantity)),
                conid=raw.get("conid"),
                bid=float(raw.get("bid", 0.0)),
                ask=float(raw.get("ask", 0.0)),
            )
            legs.append(leg)
            # Net from our perspective: credit = positive, debit = negative
            if leg.action == "SELL":
                net += leg.mid
            else:
                net -= leg.mid

        # Apply slippage offset
        slippage = abs(net) * _OPTIONS_SLIPPAGE_PCT
        if net > 0:  # net credit — submit slightly lower to get filled
            limit_price = round(net - slippage, 2)
        else:        # net debit — submit slightly higher to get filled
            limit_price = round(net + slippage, 2)

        strategy_type = signal.metadata.get("strategy_type", "multi_leg")
        logger.info(
            "[OrderBuilder] %s combo: %d legs, net=%+.2f, limit=%+.2f",
            strategy_type, len(legs), net, limit_price,
        )

        return ComboOrder(
            symbol=signal.symbol,
            legs=legs,
            limit_price=limit_price,
            quantity=int(quantity),
            strategy_id=signal.strategy_id,
            signal_id=signal.signal_id,
            session_id=signal.metadata.get("session_id"),
        )
