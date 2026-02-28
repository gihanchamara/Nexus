"""
Black-Scholes Greeks calculator + portfolio Greeks monitor.

Black-Scholes assumptions: European options, constant IV, no dividends.
Adequate for strategy logic and position monitoring.
For live trading, IBKR provides Greeks directly on each Ticker — use those
when available and fall back to BS for positions without a live quote.

Options multiplier: standard equity options = 100 shares per contract.
All portfolio Greeks are scaled by multiplier × quantity.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from math import exp, log, sqrt

from scipy.stats import norm  # type: ignore[import-untyped]

from nexus.core.schemas import PortfolioGreeks

logger = logging.getLogger(__name__)

# Standard equity options contract multiplier
_CONTRACT_MULTIPLIER = 100


# ─── Black-Scholes core ───────────────────────────────────────────────────────

def bs_d1_d2(S: float, K: float, T: float, r: float, sigma: float) -> tuple[float, float]:
    """Compute d1 and d2 for Black-Scholes formula."""
    d1 = (log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * sqrt(T))
    d2 = d1 - sigma * sqrt(T)
    return d1, d2


def bs_greeks(
    S: float,      # spot price
    K: float,      # strike price
    T: float,      # time to expiry in years (e.g. 30/365)
    r: float,      # risk-free rate (decimal: 0.05 = 5%)
    sigma: float,  # implied volatility (decimal: 0.20 = 20%)
    right: str,    # 'C' for call, 'P' for put
) -> dict[str, float]:
    """
    Compute Black-Scholes Greeks for a single options contract.

    Returns dict with keys: delta, gamma, theta, vega, rho
      theta: per calendar day (annualised theta / 365)
      vega:  per 1% IV change (annualised vega / 100)
      rho:   per 1% rate change (annualised rho / 100)
    """
    # Guard against degenerate inputs
    T = max(T, 1e-6)
    sigma = max(sigma, 1e-6)
    S = max(S, 1e-6)

    d1, d2 = bs_d1_d2(S, K, T, r, sigma)

    gamma = norm.pdf(d1) / (S * sigma * sqrt(T))
    vega  = S * norm.pdf(d1) * sqrt(T) / 100.0  # per 1% IV move

    if right.upper() == "C":
        delta = norm.cdf(d1)
        theta = (
            -S * norm.pdf(d1) * sigma / (2.0 * sqrt(T))
            - r * K * exp(-r * T) * norm.cdf(d2)
        ) / 365.0
        rho = K * T * exp(-r * T) * norm.cdf(d2) / 100.0
    else:  # put
        delta = norm.cdf(d1) - 1.0
        theta = (
            -S * norm.pdf(d1) * sigma / (2.0 * sqrt(T))
            + r * K * exp(-r * T) * norm.cdf(-d2)
        ) / 365.0
        rho = -K * T * exp(-r * T) * norm.cdf(-d2) / 100.0

    return {
        "delta": delta,
        "gamma": gamma,
        "theta": theta,
        "vega":  vega,
        "rho":   rho,
    }


def bs_price(S: float, K: float, T: float, r: float, sigma: float, right: str) -> float:
    """Compute Black-Scholes theoretical price."""
    T = max(T, 1e-6)
    sigma = max(sigma, 1e-6)
    d1, d2 = bs_d1_d2(S, K, T, r, sigma)
    if right.upper() == "C":
        return S * norm.cdf(d1) - K * exp(-r * T) * norm.cdf(d2)
    else:
        return K * exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1)


# ─── Position representation ──────────────────────────────────────────────────

@dataclass
class EquityPosition:
    """A long or short equity (stock) position. Delta = 1.0 per share."""
    symbol: str
    quantity: float          # positive = long, negative = short
    avg_cost: float
    current_price: float = 0.0

    @property
    def delta(self) -> float:
        return self.quantity  # each share has delta of 1


@dataclass
class OptionsPosition:
    """A single options leg with live or BS-computed Greeks."""
    symbol: str              # underlying
    right: str               # 'C' or 'P'
    strike: float
    expiry: str              # 'YYYYMMDD'
    expiry_date: date
    quantity: int            # contracts (positive = long, negative = short)
    avg_cost: float          # premium paid/received per contract
    current_iv: float        # current implied vol (decimal)
    spot_price: float        # current underlying price
    risk_free_rate: float = 0.05
    conid: int | None = None

    @property
    def dte(self) -> int:
        return max(0, (self.expiry_date - date.today()).days)

    @property
    def T(self) -> float:
        """Time to expiry in years."""
        return max(self.dte / 365.0, 1e-6)

    @property
    def greeks(self) -> dict[str, float]:
        """
        Greeks scaled for the actual position.
        sign(quantity) flips delta/theta/etc. for short positions.
        Multiplied by 100 (contract multiplier).
        """
        g = bs_greeks(self.spot_price, self.strike, self.T,
                      self.risk_free_rate, self.current_iv, self.right)
        return {k: v * self.quantity * _CONTRACT_MULTIPLIER for k, v in g.items()}

    @property
    def market_value(self) -> float:
        """Current position market value (positive = asset, negative = liability)."""
        price = bs_price(self.spot_price, self.strike, self.T,
                         self.risk_free_rate, self.current_iv, self.right)
        return price * self.quantity * _CONTRACT_MULTIPLIER

    @property
    def unrealized_pnl(self) -> float:
        price = bs_price(self.spot_price, self.strike, self.T,
                         self.risk_free_rate, self.current_iv, self.right)
        return (price - self.avg_cost) * self.quantity * _CONTRACT_MULTIPLIER


# ─── Portfolio Greeks Monitor ─────────────────────────────────────────────────

class GreeksMonitor:
    """
    Maintains the full options + equity position book and provides
    real-time aggregated portfolio Greeks.

    Called by:
      - OMS PaperEngine on every fill → update_from_fill()
      - Ingestion IBKRConnector on every tick → update_spot()
      - Risk Manager on every check → portfolio_greeks property
    """

    def __init__(self) -> None:
        self._equity: dict[str, EquityPosition] = {}        # symbol → position
        self._options: dict[str, OptionsPosition] = {}      # key → position

    def _options_key(self, symbol: str, right: str, strike: float, expiry: str) -> str:
        return f"{symbol}:{right}:{strike}:{expiry}"

    # ─── Position updates ─────────────────────────────────────────────────────

    def update_equity(self, symbol: str, quantity: float, avg_cost: float,
                      current_price: float = 0.0) -> None:
        """Upsert an equity position."""
        self._equity[symbol] = EquityPosition(
            symbol=symbol, quantity=quantity,
            avg_cost=avg_cost, current_price=current_price,
        )

    def update_options(self, position: OptionsPosition) -> None:
        """Upsert an options position (identified by symbol+right+strike+expiry)."""
        key = self._options_key(position.symbol, position.right,
                                position.strike, position.expiry)
        if position.quantity == 0:
            self._options.pop(key, None)
        else:
            self._options[key] = position

    def update_spot(self, symbol: str, spot_price: float) -> None:
        """Update spot price used for Greeks calculation."""
        if symbol in self._equity:
            self._equity[symbol].current_price = spot_price
        for pos in self._options.values():
            if pos.symbol == symbol:
                pos.spot_price = spot_price

    def update_iv(self, symbol: str, right: str, strike: float,
                  expiry: str, iv: float) -> None:
        """Update IV for a specific options position (e.g. from live quote)."""
        key = self._options_key(symbol, right, strike, expiry)
        if key in self._options:
            self._options[key].current_iv = iv

    def remove_expired(self) -> None:
        """Remove options positions that have expired (DTE = 0)."""
        expired = [k for k, p in self._options.items() if p.dte == 0]
        for k in expired:
            del self._options[k]
        if expired:
            logger.info("Removed %d expired options positions", len(expired))

    # ─── Portfolio Greeks ─────────────────────────────────────────────────────

    @property
    def portfolio_greeks(self) -> PortfolioGreeks:
        """Aggregate Greeks across ALL open positions."""
        delta = gamma = theta = vega = rho = 0.0

        # Equity: each share has delta = 1, all other Greeks = 0
        for pos in self._equity.values():
            delta += pos.delta

        # Options: full Greeks × quantity × multiplier
        for pos in self._options.values():
            g = pos.greeks
            delta += g["delta"]
            gamma += g["gamma"]
            theta += g["theta"]
            vega  += g["vega"]
            rho   += g["rho"]

        return PortfolioGreeks(
            delta=round(delta, 4),
            gamma=round(gamma, 6),
            theta=round(theta, 4),
            vega=round(vega, 4),
            rho=round(rho, 4),
        )

    def position_greeks(self, symbol: str) -> dict[str, float]:
        """Greeks for all positions in a single underlying."""
        result = {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0, "rho": 0.0}
        if symbol in self._equity:
            result["delta"] += self._equity[symbol].delta
        for pos in self._options.values():
            if pos.symbol == symbol:
                for k, v in pos.greeks.items():
                    result[k] += v
        return result

    # ─── Introspection ────────────────────────────────────────────────────────

    def open_options_count(self) -> int:
        return len(self._options)

    def open_equity_count(self) -> int:
        return len(self._equity)

    def snapshot(self) -> dict:
        greeks = self.portfolio_greeks
        return {
            "portfolio_greeks": greeks.model_dump(),
            "open_equity_positions": self.open_equity_count(),
            "open_options_positions": self.open_options_count(),
        }
