"""
Position Sizer.

Translates a signal's confidence into a concrete quantity (shares or contracts).
Multiple sizing methods supported — configured per strategy in parameters.

All methods respect:
  - Signal.confidence (0–1): scales quantity proportionally
  - Portfolio value: expressed as % of total capital
  - Risk Manager may further reduce via adjusted_quantity

Sizing methods:
  fixed_pct       — fixed % of portfolio value per trade  (simplest)
  kelly           — Kelly Criterion (volatility-adjusted optimal fraction)
  atr_based       — ATR-derived stop → position size (risk-per-trade model)
  options_premium — max premium % of portfolio (for options premium selling)
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class PositionSizer:
    """
    Stateless sizer — all methods are pure functions of their inputs.
    Used by the SignalRouter to compute quantity before OrderBuilder.
    """

    # ─── Equity sizing ────────────────────────────────────────────────────────

    @staticmethod
    def fixed_pct(
        portfolio_value: float,
        price: float,
        risk_pct: float = 0.02,      # 2% of portfolio per trade
        confidence: float = 1.0,
        min_qty: float = 1.0,
    ) -> float:
        """
        Allocate a fixed % of portfolio, scaled by signal confidence.
        Example: 100k portfolio, 2% risk, price=$450, conf=0.8
          → 100k × 2% × 0.8 / 450 = 3.5 → 3 shares
        """
        if price <= 0 or portfolio_value <= 0:
            return 0.0
        capital = portfolio_value * risk_pct * confidence
        qty = capital / price
        return max(min_qty, round(qty, 0))

    @staticmethod
    def kelly(
        portfolio_value: float,
        price: float,
        win_rate: float,
        avg_win: float,      # average win as fraction of price (e.g. 0.05 = 5%)
        avg_loss: float,     # average loss as fraction of price (e.g. 0.02 = 2%)
        max_fraction: float = 0.20,   # cap Kelly at 20% of portfolio
        confidence: float = 1.0,
    ) -> float:
        """
        Kelly Criterion: f* = (p × b - q) / b
          p = win probability, q = 1-p, b = avg_win / avg_loss

        Confidence scales the Kelly fraction (half-Kelly at confidence=0.5).
        Fraction is capped at max_fraction to prevent over-betting.
        """
        if avg_loss <= 0 or price <= 0:
            return 0.0
        b = avg_win / avg_loss
        q = 1.0 - win_rate
        kelly_f = max(0.0, (win_rate * b - q) / b)
        fraction = min(kelly_f * confidence, max_fraction)
        capital = portfolio_value * fraction
        return max(1.0, round(capital / price, 0))

    @staticmethod
    def atr_based(
        portfolio_value: float,
        price: float,
        atr: float,            # Average True Range in price units
        stop_atr_multiple: float = 2.0,    # stop = 2 × ATR below entry
        risk_per_trade_pct: float = 0.01,  # risk 1% of portfolio per trade
        confidence: float = 1.0,
    ) -> float:
        """
        Position size so that a stop-loss at (stop_atr_multiple × ATR)
        from entry risks exactly risk_per_trade_pct of portfolio.

        qty = (portfolio × risk_pct × confidence) / (ATR × stop_multiple)
        """
        if atr <= 0 or price <= 0:
            return 0.0
        risk_amount = portfolio_value * risk_per_trade_pct * confidence
        stop_distance = atr * stop_atr_multiple
        qty = risk_amount / stop_distance
        return max(1.0, round(qty, 0))

    # ─── Options sizing ───────────────────────────────────────────────────────

    @staticmethod
    def options_premium_pct(
        portfolio_value: float,
        net_premium_per_contract: float,   # net credit/debit per contract (in dollars)
        max_premium_pct: float = 0.03,     # max 3% of portfolio in options premium
        confidence: float = 1.0,
        min_contracts: int = 1,
    ) -> int:
        """
        For options premium strategies (selling condors, CSPs, etc.).
        Limits total premium exposure to max_premium_pct of portfolio.

        Net credit strategies: max_premium_pct applied to max possible loss
        (wing_width × 100 × contracts).
        """
        if net_premium_per_contract <= 0 or portfolio_value <= 0:
            return min_contracts
        max_capital = portfolio_value * max_premium_pct * confidence
        contracts = int(max_capital / (net_premium_per_contract * 100))
        return max(min_contracts, contracts)

    @staticmethod
    def confidence_scaled(base_quantity: float, confidence: float,
                          min_qty: float = 1.0) -> float:
        """
        Scale any base quantity by signal confidence.
        confidence > 0.7 → full size
        confidence 0.5–0.7 → proportional
        confidence < 0.5 → min size
        """
        if confidence >= 0.7:
            return max(min_qty, round(base_quantity, 0))
        elif confidence >= 0.5:
            scaled = base_quantity * (confidence / 0.7)
            return max(min_qty, round(scaled, 0))
        else:
            return min_qty
