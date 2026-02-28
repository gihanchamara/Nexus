"""
Paper Trading Engine.

Simulates order fills using live market data.
Emits IDENTICAL events to the live broker — zero code difference upstream.

Fill simulation rules:
  Equity single-leg : fill at last traded price (or mid if no last)
  Options single-leg: fill at (bid + ask) / 2  (mid-price)
  Multi-leg combo   : fill each leg at mid-price; net credit/debit is the sum

Commission model (IBKR schedule, simplified):
  Equity : $0.005/share, min $1.00, max 1% of trade value
  Options: $0.65/contract, min $1.00

Position tracking:
  Equity positions: shares × avg_cost
  Options positions: contracts × avg_premium × 100 (multiplier)
  Multi-leg: tracked as individual legs (each leg is a separate position)
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from nexus.bus.publisher import publisher
from nexus.core.config import TradingMode, get_settings
from nexus.core.schemas import (
    ComponentType,
    Direction,
    OrderEvent,
    OrderRequest,
    OrderStatus,
    OrderType,
    TelemetryEvent,
    TelemetryLevel,
)
from nexus.oms.order_builder import ComboOrder

logger = logging.getLogger(__name__)

_CONTRACT_MULTIPLIER = 100


# ─── Position Book ────────────────────────────────────────────────────────────

@dataclass
class PaperPosition:
    """A single tracked position in the paper portfolio."""
    symbol: str
    quantity: float             # positive = long, negative = short
    avg_cost: float             # per share or per contract (pre-multiplier)
    is_options: bool = False
    right: str = ""             # 'C' or 'P' for options
    strike: float = 0.0
    expiry: str = ""
    realized_pnl: float = 0.0
    open_time: datetime = field(default_factory=datetime.utcnow)

    @property
    def multiplier(self) -> int:
        return _CONTRACT_MULTIPLIER if self.is_options else 1

    def unrealized_pnl(self, current_price: float) -> float:
        if self.quantity > 0:   # long
            return self.quantity * (current_price - self.avg_cost) * self.multiplier
        else:                   # short
            return abs(self.quantity) * (self.avg_cost - current_price) * self.multiplier

    def market_value(self, current_price: float) -> float:
        return self.quantity * current_price * self.multiplier


class PositionBook:
    """In-memory position ledger for the paper engine."""

    def __init__(self) -> None:
        self._positions: dict[str, PaperPosition] = {}  # key → position
        self._realized_pnl: float = 0.0

    def _key(self, symbol: str, right: str = "", strike: float = 0.0,
             expiry: str = "") -> str:
        if right:
            return f"{symbol}:{right}:{strike}:{expiry}"
        return symbol

    def update(self, symbol: str, qty_delta: float, fill_price: float,
               is_options: bool = False, right: str = "", strike: float = 0.0,
               expiry: str = "") -> None:
        """Update (or create) a position after a fill."""
        key = self._key(symbol, right, strike, expiry)
        if key not in self._positions:
            self._positions[key] = PaperPosition(
                symbol=symbol, quantity=0.0, avg_cost=fill_price,
                is_options=is_options, right=right, strike=strike, expiry=expiry,
            )

        pos = self._positions[key]
        existing_qty = pos.quantity
        new_qty = existing_qty + qty_delta

        if new_qty == 0:
            # Position fully closed — realize P&L
            if existing_qty > 0:
                realized = existing_qty * (fill_price - pos.avg_cost) * pos.multiplier
            else:
                realized = abs(existing_qty) * (pos.avg_cost - fill_price) * pos.multiplier
            self._realized_pnl += realized
            pos.realized_pnl += realized
            del self._positions[key]
        elif (existing_qty > 0 and qty_delta > 0) or (existing_qty < 0 and qty_delta < 0):
            # Adding to position: VWAP avg cost
            total_cost = abs(existing_qty) * pos.avg_cost + abs(qty_delta) * fill_price
            pos.avg_cost = total_cost / abs(new_qty)
            pos.quantity = new_qty
        else:
            # Partial close
            pos.quantity = new_qty

    def get(self, symbol: str, right: str = "", strike: float = 0.0,
            expiry: str = "") -> PaperPosition | None:
        return self._positions.get(self._key(symbol, right, strike, expiry))

    def all_positions(self) -> list[PaperPosition]:
        return list(self._positions.values())

    def unrealized_pnl(self, prices: dict[str, float]) -> float:
        total = 0.0
        for pos in self._positions.values():
            price = prices.get(pos.symbol, 0.0)
            if price > 0:
                total += pos.unrealized_pnl(price)
        return total

    @property
    def realized_pnl(self) -> float:
        return self._realized_pnl


# ─── Commission Model ─────────────────────────────────────────────────────────

def ibkr_equity_commission(qty: float, price: float) -> float:
    commission = abs(qty) * 0.005
    commission = max(commission, 1.00)
    commission = min(commission, abs(qty) * price * 0.01)
    return round(commission, 4)


def ibkr_options_commission(contracts: int) -> float:
    commission = abs(contracts) * 0.65
    return max(commission, 1.00)


# ─── Paper Engine ─────────────────────────────────────────────────────────────

class PaperEngine:
    """
    Simulates trade execution for paper trading mode.

    Usage:
        engine = PaperEngine(initial_capital=100_000)
        await engine.submit(order, current_prices={'AAPL': 175.50})
    """

    COMPONENT_ID = "paper_engine"

    def __init__(self, initial_capital: float = 100_000.0) -> None:
        self._capital = initial_capital           # available cash
        self._initial_capital = initial_capital
        self._positions = PositionBook()
        self._current_prices: dict[str, float] = {}
        self._order_counter: int = 0

    # ─── Price feed ───────────────────────────────────────────────────────────

    def update_price(self, symbol: str, price: float) -> None:
        """Called by the ingestion layer to keep prices current."""
        self._current_prices[symbol] = price

    def update_prices(self, prices: dict[str, float]) -> None:
        self._current_prices.update(prices)

    # ─── Order submission ─────────────────────────────────────────────────────

    async def submit(
        self,
        order: OrderRequest | ComboOrder,
        prices: dict[str, float] | None = None,
    ) -> list[OrderEvent]:
        """
        Simulate order submission and immediate fill.
        Returns list of OrderEvents emitted (one per leg for combos).
        """
        if prices:
            self.update_prices(prices)

        if isinstance(order, ComboOrder):
            return await self._fill_combo(order)
        else:
            return [await self._fill_single(order)]

    # ─── Single-leg fill ──────────────────────────────────────────────────────

    async def _fill_single(self, order: OrderRequest) -> OrderEvent:
        self._order_counter += 1
        order_id = self._order_counter

        fill_price = self._get_fill_price(order.symbol, order.limit_price)
        if fill_price is None:
            return await self._reject(order_id, order.symbol,
                                      order.side, order.quantity, "No price available")

        commission = ibkr_equity_commission(order.quantity, fill_price)
        qty_signed = order.quantity if order.side == Direction.BUY else -order.quantity
        self._positions.update(order.symbol, qty_signed, fill_price)
        self._capital -= (qty_signed * fill_price + commission)

        event = OrderEvent(
            order_id=order_id,
            symbol=order.symbol,
            side=order.side,
            order_type=order.order_type,
            quantity=order.quantity,
            limit_price=order.limit_price,
            status=OrderStatus.FILLED,
            filled_qty=order.quantity,
            avg_fill_price=fill_price,
            commission=commission,
            mode=TradingMode.PAPER,
        )
        await self._emit_order_event(event)
        logger.info("[Paper] FILL %s %s %g @ %.2f comm=%.2f",
                    order.side.value, order.symbol, order.quantity, fill_price, commission)
        return event

    # ─── Multi-leg combo fill ─────────────────────────────────────────────────

    async def _fill_combo(self, combo: ComboOrder) -> list[OrderEvent]:
        """Fill each leg independently at mid-price. Net P&L = sum of legs."""
        events: list[OrderEvent] = []
        total_commission = 0.0

        for leg in combo.legs:
            self._order_counter += 1
            leg_price = leg.mid if leg.mid > 0 else self._current_prices.get(leg.symbol, 0.0)
            if leg_price <= 0:
                logger.warning("[Paper] No price for options leg %s %s %.0f %s",
                               leg.right, leg.symbol, leg.strike, leg.expiry)
                continue

            commission = ibkr_options_commission(leg.quantity * combo.quantity)
            qty_signed = (leg.quantity * combo.quantity
                          if leg.action == "BUY"
                          else -leg.quantity * combo.quantity)

            # Update options position book
            self._positions.update(
                symbol=combo.symbol,
                qty_delta=qty_signed,
                fill_price=leg_price,
                is_options=True,
                right=leg.right,
                strike=leg.strike,
                expiry=leg.expiry,
            )

            # Credit received (SELL) adds to capital; debit (BUY) reduces it
            net_cash = (-qty_signed) * leg_price * _CONTRACT_MULTIPLIER - commission
            self._capital += net_cash
            total_commission += commission

            side = Direction.BUY if leg.action == "BUY" else Direction.SELL
            event = OrderEvent(
                order_id=self._order_counter,
                symbol=combo.symbol,
                side=side,
                order_type=OrderType.LMT,
                quantity=abs(qty_signed),
                limit_price=leg_price,
                status=OrderStatus.FILLED,
                filled_qty=abs(qty_signed),
                avg_fill_price=leg_price,
                commission=commission,
                mode=TradingMode.PAPER,
            )
            events.append(event)

        for event in events:
            await self._emit_order_event(event)

        net_credit = combo.net_premium
        logger.info("[Paper] COMBO FILL %s: %d legs, net=%+.2f, total_comm=%.2f",
                    combo.symbol, len(combo.legs), net_credit, total_commission)
        return events

    # ─── Helpers ──────────────────────────────────────────────────────────────

    def _get_fill_price(self, symbol: str, limit_price: float | None) -> float | None:
        """Determine fill price: limit or current market price."""
        market_price = self._current_prices.get(symbol)
        if market_price and market_price > 0:
            return market_price
        if limit_price and limit_price > 0:
            return limit_price
        return None

    async def _reject(self, order_id: int, symbol: str,
                      side: Direction, qty: float, reason: str) -> OrderEvent:
        logger.warning("[Paper] ORDER REJECTED %s %s %g: %s", side.value, symbol, qty, reason)
        event = OrderEvent(
            order_id=order_id, symbol=symbol, side=side,
            order_type=OrderType.MKT, quantity=qty,
            status=OrderStatus.REJECTED, mode=TradingMode.PAPER,
        )
        await self._emit_order_event(event)
        return event

    async def _emit_order_event(self, event: OrderEvent) -> None:
        await publisher.publish("order.filled", event.model_dump(mode="json"))
        settings = get_settings()
        tel = TelemetryEvent(
            component_id=self.COMPONENT_ID,
            component_type=ComponentType.PAPER_ENGINE,
            channel="telemetry.oms",
            level=TelemetryLevel.INFO,
            mode=TradingMode(settings.nexus_trading_mode),
            payload={
                "order_id": event.order_id,
                "symbol": event.symbol,
                "side": event.side.value,
                "status": event.status.value,
                "fill_price": event.avg_fill_price,
                "commission": event.commission,
                "portfolio_value": self.portfolio_value,
                "available_capital": self._capital,
            },
        )
        await publisher.publish_telemetry(tel)

    # ─── Portfolio snapshot ───────────────────────────────────────────────────

    @property
    def portfolio_value(self) -> float:
        """Total portfolio value: cash + unrealized mark-to-market."""
        unrealized = self._positions.unrealized_pnl(self._current_prices)
        return round(self._capital + unrealized, 2)

    def snapshot(self) -> dict[str, Any]:
        """Full portfolio snapshot for the dashboard."""
        return {
            "initial_capital": self._initial_capital,
            "available_capital": round(self._capital, 2),
            "portfolio_value": self.portfolio_value,
            "total_pnl": round(self.portfolio_value - self._initial_capital, 2),
            "realized_pnl": round(self._positions.realized_pnl, 2),
            "unrealized_pnl": round(
                self._positions.unrealized_pnl(self._current_prices), 2
            ),
            "positions": [
                {
                    "symbol": p.symbol,
                    "quantity": p.quantity,
                    "avg_cost": round(p.avg_cost, 4),
                    "is_options": p.is_options,
                    "right": p.right,
                    "strike": p.strike,
                    "expiry": p.expiry,
                }
                for p in self._positions.all_positions()
            ],
        }
