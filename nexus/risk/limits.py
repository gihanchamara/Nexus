"""
Position and portfolio limit checks.

Called by the Risk Manager before every order submission.
Each check returns a RiskCheckResult (approved=True/False + reason list).

Configurable via strategy parameters or platform-level config.
All limits are soft-configurable — no hardcoded magic numbers.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from nexus.core.schemas import PortfolioGreeks, RiskCheckResult

logger = logging.getLogger(__name__)


@dataclass
class PositionLimitConfig:
    """Per-strategy or platform-level position sizing limits."""
    max_position_pct: float = 0.10     # max % of portfolio per symbol (10%)
    max_options_premium_pct: float = 0.05  # max premium % of portfolio per options trade (5%)
    max_open_positions: int = 20        # max concurrent open positions
    max_contracts_per_expiry: int = 10  # max option contracts per single expiry
    max_legs_per_order: int = 4         # max legs in a single combo order (Iron Condor = 4)


@dataclass
class GreeksLimitConfig:
    """Portfolio-level Greeks exposure limits."""
    max_net_delta: float = 500.0        # max absolute net delta (500 shares equivalent)
    max_gamma: float = 50.0             # max absolute gamma
    max_vega_exposure: float = 5000.0   # max absolute vega ($ per 1% IV move)
    min_theta: float = -500.0           # min daily theta (don't pay more than $500/day in decay)
    max_theta: float | None = None      # no upper bound on theta income by default


@dataclass
class DrawdownConfig:
    """Drawdown and daily loss protection."""
    max_portfolio_dd_pct: float = 0.10  # max portfolio drawdown (10% of initial capital)
    max_daily_loss_pct: float = 0.03    # max daily loss (3% of portfolio value)
    max_consecutive_losses: int = 5     # pause strategy after N consecutive losing trades


class LimitsChecker:
    """
    Stateless limit checker — checks proposed orders against configured limits.
    Called synchronously within the risk manager check flow.
    """

    def __init__(
        self,
        position_cfg: PositionLimitConfig | None = None,
        greeks_cfg: GreeksLimitConfig | None = None,
        drawdown_cfg: DrawdownConfig | None = None,
    ) -> None:
        self.pos = position_cfg or PositionLimitConfig()
        self.greeks = greeks_cfg or GreeksLimitConfig()
        self.dd = drawdown_cfg or DrawdownConfig()

    # ─── Position size ────────────────────────────────────────────────────────

    def check_equity_size(
        self,
        proposed_qty: float,
        price: float,
        portfolio_value: float,
    ) -> RiskCheckResult:
        """Ensure an equity order doesn't exceed max position % of portfolio."""
        if portfolio_value <= 0:
            return RiskCheckResult(approved=False, reasons=["Portfolio value is zero or unknown"])

        trade_value = abs(proposed_qty) * price
        pct = trade_value / portfolio_value
        if pct > self.pos.max_position_pct:
            return RiskCheckResult(
                approved=False,
                reasons=[
                    f"Equity position {pct:.1%} exceeds max {self.pos.max_position_pct:.1%} of portfolio"
                ],
                adjusted_quantity=round(
                    (self.pos.max_position_pct * portfolio_value) / max(price, 0.01), 0
                ),
            )
        return RiskCheckResult(approved=True)

    def check_options_premium(
        self,
        net_premium: float,
        contracts: int,
        portfolio_value: float,
    ) -> RiskCheckResult:
        """Ensure options premium cost doesn't exceed max % of portfolio."""
        if portfolio_value <= 0:
            return RiskCheckResult(approved=False, reasons=["Portfolio value is zero"])

        total_premium = abs(net_premium) * contracts * 100
        pct = total_premium / portfolio_value
        if pct > self.pos.max_options_premium_pct:
            return RiskCheckResult(
                approved=False,
                reasons=[
                    f"Options premium {pct:.1%} exceeds max {self.pos.max_options_premium_pct:.1%} of portfolio"
                ],
            )
        return RiskCheckResult(approved=True)

    # ─── Greeks limits ────────────────────────────────────────────────────────

    def check_portfolio_greeks(
        self,
        current: PortfolioGreeks,
        delta_change: float = 0.0,
        vega_change: float = 0.0,
    ) -> RiskCheckResult:
        """
        Check if adding delta_change and vega_change would breach Greeks limits.
        Call this BEFORE the order is filled to pre-check the impact.
        """
        reasons: list[str] = []
        prospective_delta = current.delta + delta_change
        prospective_vega  = current.vega + vega_change

        if abs(prospective_delta) > self.greeks.max_net_delta:
            reasons.append(
                f"Net delta {prospective_delta:.1f} would exceed limit ±{self.greeks.max_net_delta:.1f}"
            )
        if abs(prospective_vega) > self.greeks.max_vega_exposure:
            reasons.append(
                f"Vega exposure ${prospective_vega:.0f} would exceed limit ${self.greeks.max_vega_exposure:.0f}"
            )
        if abs(current.gamma) > self.greeks.max_gamma:
            reasons.append(
                f"Gamma {current.gamma:.2f} exceeds limit {self.greeks.max_gamma:.2f} — high near-expiry risk"
            )
        if current.theta < self.greeks.min_theta:
            reasons.append(
                f"Daily theta ${current.theta:.0f} below min ${self.greeks.min_theta:.0f}"
            )

        return RiskCheckResult(approved=not reasons, reasons=reasons)

    # ─── Drawdown ─────────────────────────────────────────────────────────────

    def check_drawdown(
        self,
        portfolio_value: float,
        peak_value: float,
        daily_pnl: float,
    ) -> RiskCheckResult:
        """
        Check if portfolio is within drawdown and daily loss limits.
        Call at the start of each trading session and before large orders.
        """
        reasons: list[str] = []

        if peak_value > 0:
            dd_pct = (peak_value - portfolio_value) / peak_value
            if dd_pct > self.dd.max_portfolio_dd_pct:
                reasons.append(
                    f"Portfolio drawdown {dd_pct:.1%} exceeds max {self.dd.max_portfolio_dd_pct:.1%}"
                )

        if portfolio_value > 0:
            daily_loss_pct = abs(daily_pnl) / portfolio_value
            if daily_pnl < 0 and daily_loss_pct > self.dd.max_daily_loss_pct:
                reasons.append(
                    f"Daily loss {daily_loss_pct:.1%} exceeds max {self.dd.max_daily_loss_pct:.1%}"
                )

        return RiskCheckResult(approved=not reasons, reasons=reasons)

    # ─── Convenience: run all checks ─────────────────────────────────────────

    def check_all(
        self,
        portfolio_value: float,
        peak_value: float,
        daily_pnl: float,
        current_greeks: PortfolioGreeks,
    ) -> RiskCheckResult:
        """Run drawdown + Greeks limits. Returns first failure or overall approved."""
        checks = [
            self.check_drawdown(portfolio_value, peak_value, daily_pnl),
            self.check_portfolio_greeks(current_greeks),
        ]
        reasons = [r for c in checks for r in c.reasons]
        return RiskCheckResult(approved=not reasons, reasons=reasons)
