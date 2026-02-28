"""
Historical Options Pricer — Phase 3.

Constructs synthetic OptionsChainSnapshot objects from historical price data
using Black-Scholes pricing and a simplified IV surface model.

This module enables options strategy backtesting without requiring historical
options data (which is expensive and often unavailable for retail).

Key components:
  HistoricalVolatility — rolling HV and IV Rank computation from close prices
  IVSurface           — simplified IV skew model (put skew + term structure)
  PricerChainBuilder  — assembles full OptionsChainSnapshot with BS Greeks

IV Surface Model (simplified):
  Base IV  = historical_volatility × vol_premium (HV × 1.15 approximates IV)
  Put skew = OTM puts carry higher IV (negative moneyness = higher IV)
  Call skew= OTM calls carry slightly higher IV (positive moneyness)
  Term     = near-term IV is elevated vs. longer-term (VIX term structure)

Limitations (documented for user):
  - No dividend adjustment (adequate for index ETFs; underestimates ITM calls for div payers)
  - Simplified skew model (real skew varies by regime)
  - IV = HV × premium factor (ignores events, earnings)
  For backtesting purposes these are acceptable approximations.

DB class reference: None (pure computation, no DB access)
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from math import exp, log, sqrt

from nexus.core.schemas import OptionsChainSnapshot, OptionsContract
from nexus.risk.greeks import bs_greeks, bs_price

logger = logging.getLogger(__name__)

# Typical ratio of IV to historical vol (IV premium / "fear factor")
_HV_TO_IV_PREMIUM = 1.15

# Put skew coefficients (empirically derived from SPX term structure)
_PUT_SKEW_COEFF = 0.5    # additional IV per unit of negative moneyness
_CALL_SKEW_COEFF = 0.2   # additional IV per unit of positive moneyness

# Near-term IV elevation (VIX term structure: 0DTE > 30DTE > 90DTE)
_NEAR_TERM_DAYS = 20     # below this DTE, elevate IV
_NEAR_TERM_BUMP = 0.05   # +5% IV for near-term options


# ─── Historical Volatility ────────────────────────────────────────────────────

class HistoricalVolatility:
    """
    Compute rolling historical volatility (HV) from daily close prices.

    HV is the annualised standard deviation of log daily returns.
    This is the standard realised volatility measure used in options pricing.
    """

    @staticmethod
    def rolling(closes: list[float], window: int = 20) -> list[float]:
        """
        Compute rolling HV for each bar.

        Returns a list the same length as `closes`.
        The first (window) values are 0.0 (insufficient history).

        Args:
            closes: list of close prices in chronological order
            window: lookback window in bars (default 20 = ~1 trading month)

        Returns:
            list of annualised HV values (decimal: 0.20 = 20%)
        """
        hv: list[float] = [0.0] * len(closes)
        if len(closes) < 2:
            return hv

        # Compute log returns
        log_returns = [
            log(closes[i] / closes[i - 1])
            for i in range(1, len(closes))
            if closes[i - 1] > 0 and closes[i] > 0
        ]

        # Prepend 0 to align with close prices
        log_returns = [0.0] + log_returns

        for i in range(window, len(closes)):
            window_returns = log_returns[i - window + 1: i + 1]
            n = len(window_returns)
            if n < 2:
                continue
            mean = sum(window_returns) / n
            variance = sum((r - mean) ** 2 for r in window_returns) / (n - 1)
            hv[i] = sqrt(variance * 252)   # annualise: √(daily_var × 252)

        return hv

    @staticmethod
    def iv_rank(hv_series: list[float], window: int = 252) -> list[float]:
        """
        Compute IV Rank (IVR) from a HV series.

        IVR = (current_hv - hv_low) / (hv_high - hv_low) × 100

        Uses the rolling 52-week (252 bar) range.

        Returns:
            list of IVR values 0–100 (same length as hv_series)
        """
        ivr: list[float] = [50.0] * len(hv_series)  # default neutral

        for i in range(window, len(hv_series)):
            window_hv = [h for h in hv_series[i - window: i] if h > 0]
            if not window_hv:
                continue
            hv_low = min(window_hv)
            hv_high = max(window_hv)
            current = hv_series[i]
            if hv_high > hv_low:
                ivr[i] = (current - hv_low) / (hv_high - hv_low) * 100.0
            else:
                ivr[i] = 50.0

        return ivr

    @staticmethod
    def exponential(closes: list[float], decay: float = 0.94) -> list[float]:
        """
        EWMA historical volatility (RiskMetrics model).

        More responsive to recent vol spikes than rolling HV.
        decay = 0.94 is the RiskMetrics daily parameter.

        Returns annualised EWMA volatility aligned with close prices.
        """
        hv: list[float] = [0.0] * len(closes)
        if len(closes) < 2:
            return hv

        var = (closes[1] / closes[0] - 1.0) ** 2 if closes[0] > 0 else 0.0
        for i in range(1, len(closes)):
            if closes[i - 1] > 0:
                ret = log(closes[i] / closes[i - 1])
                var = decay * var + (1 - decay) * ret ** 2
            hv[i] = sqrt(var * 252)

        return hv


# ─── IV Surface Model ─────────────────────────────────────────────────────────

class IVSurface:
    """
    Simplified implied volatility surface for synthetic chain construction.

    Models the two most important real-world features:
      1. Put skew (OTM puts trade at higher IV than ATM — "vol skew")
      2. Near-term term structure (short-dated options often have elevated IV)

    The base IV = hist_vol × HV_TO_IV_PREMIUM.
    """

    @staticmethod
    def iv_for_contract(
        spot: float,
        strike: float,
        dte: int,
        hist_vol: float,
        right: str,
    ) -> float:
        """
        Compute implied volatility for a single contract.

        Args:
            spot     : current spot price
            strike   : option strike
            dte      : calendar days to expiry
            hist_vol : base historical volatility (annualised decimal)
            right    : 'C' or 'P'

        Returns:
            Implied volatility as a decimal (e.g. 0.20 = 20%)
        """
        # Base IV = HV × premium factor
        base_iv = hist_vol * _HV_TO_IV_PREMIUM

        # Moneyness: log(K/S) — negative = OTM put / ITM call
        if spot <= 0:
            return base_iv
        moneyness = log(strike / spot)

        # Apply skew based on right and moneyness
        if right == "P":
            # OTM puts (moneyness < 0) → higher IV
            skew = max(0.0, -moneyness) * _PUT_SKEW_COEFF
        else:
            # OTM calls (moneyness > 0) → slightly higher IV
            skew = max(0.0, moneyness) * _CALL_SKEW_COEFF

        iv = base_iv + skew

        # Near-term elevation
        if dte < _NEAR_TERM_DAYS:
            near_term_factor = 1.0 - (dte / _NEAR_TERM_DAYS)
            iv += near_term_factor * _NEAR_TERM_BUMP

        # Floor IV at 5% (avoid degenerate options)
        return max(0.05, round(iv, 4))


# ─── Chain Builder ────────────────────────────────────────────────────────────

class PricerChainBuilder:
    """
    Assembles a full OptionsChainSnapshot for all strikes and expiries.

    Called by OptionsChainBuilder in data_loader.py.
    Directly uses bs_greeks() and bs_price() from nexus.risk.greeks.
    """

    @staticmethod
    def build_chain(
        symbol: str,
        timestamp: datetime,
        spot: float,
        strikes: list[float],
        expiry_dates: list[date],
        hist_vol: float,
        iv_rank: float,
        risk_free_rate: float = 0.05,
        iv_skew: bool = True,
        spread_pct: float = 0.04,
    ) -> OptionsChainSnapshot:
        """
        Build a complete synthetic options chain.

        For each (strike, expiry, right) combination:
          1. Compute IV from IVSurface model
          2. Compute BS price and Greeks
          3. Simulate bid/ask spread around mid

        Args:
            symbol        : underlying symbol
            timestamp     : bar timestamp (when this chain is "as of")
            spot          : current spot price
            strikes       : list of strike prices to include
            expiry_dates  : list of expiry dates
            hist_vol      : annualised historical volatility (decimal)
            iv_rank       : IV Rank 0–100 for this bar
            risk_free_rate: annualised risk-free rate
            iv_skew       : apply IVSurface skew model (True = realistic)
            spread_pct    : bid-ask spread as fraction of mid (default 4%)

        Returns:
            OptionsChainSnapshot with all contracts priced and Greeks computed
        """
        today = timestamp.date() if isinstance(timestamp, datetime) else timestamp
        contracts: list[OptionsContract] = []

        for expiry_date in expiry_dates:
            dte = max(1, (expiry_date - today).days)
            T = dte / 365.0
            expiry_str = expiry_date.strftime("%Y%m%d")

            for strike in strikes:
                if strike <= 0:
                    continue
                for right in ("C", "P"):
                    iv = (
                        IVSurface.iv_for_contract(spot, strike, dte, hist_vol, right)
                        if iv_skew
                        else hist_vol * _HV_TO_IV_PREMIUM
                    )

                    # Black-Scholes price and Greeks
                    mid = bs_price(spot, strike, T, risk_free_rate, iv, right)
                    if mid <= 0.001:
                        continue   # worthless / deep OTM — skip

                    greeks = bs_greeks(spot, strike, T, risk_free_rate, iv, right)

                    # Simulate bid-ask spread
                    half_spread = mid * (spread_pct / 2.0)
                    bid = max(0.01, round(mid - half_spread, 2))
                    ask = round(mid + half_spread, 2)

                    contracts.append(OptionsContract(
                        symbol=symbol,
                        right=right,
                        strike=round(strike, 2),
                        expiry=expiry_str,
                        expiry_date=expiry_date,
                        dte=dte,
                        bid=bid,
                        ask=ask,
                        iv=round(iv, 4),
                        delta=round(greeks["delta"], 4),
                        gamma=round(greeks["gamma"], 6),
                        theta=round(greeks["theta"], 4),
                        vega=round(greeks["vega"], 4),
                        rho=round(greeks["rho"], 4),
                    ))

        chain = OptionsChainSnapshot(
            symbol=symbol,
            spot_price=spot,
            timestamp=timestamp,
            contracts=contracts,
            iv_rank=round(iv_rank, 1),
            iv_percentile=round(iv_rank, 1),  # simplified: IVP ≈ IVR for synthetic chains
        )

        logger.debug(
            "Built chain: %s @ %.2f, %d contracts, %d expiries, IVR=%.0f",
            symbol, spot, len(contracts), len(expiry_dates), iv_rank,
        )
        return chain


# ─── Convenience function ─────────────────────────────────────────────────────

def build_synthetic_chain(
    symbol: str,
    spot: float,
    timestamp: datetime,
    hist_vol: float,
    strike_count: int = 10,
    strike_spacing: float = 5.0,
    expiry_dte_list: list[int] | None = None,
    risk_free_rate: float = 0.05,
    iv_rank: float = 50.0,
    iv_skew: bool = True,
) -> OptionsChainSnapshot:
    """
    One-shot builder: generate a single synthetic chain from spot + HV.

    Useful for manual testing and strategy development.

    Example:
        chain = build_synthetic_chain("SPY", spot=450.0, timestamp=datetime.now(),
                                      hist_vol=0.18, iv_rank=65.0)
    """
    if expiry_dte_list is None:
        expiry_dte_list = [30, 60]

    today = timestamp.date() if isinstance(timestamp, datetime) else timestamp
    atm = round(round(spot / strike_spacing) * strike_spacing, 2)
    strikes = [
        atm + (j - strike_count) * strike_spacing
        for j in range(2 * strike_count + 1)
        if atm + (j - strike_count) * strike_spacing > 0
    ]
    from nexus.backtest.data_loader import _business_day_offset
    expiry_dates = [_business_day_offset(today, dte) for dte in expiry_dte_list]

    return PricerChainBuilder.build_chain(
        symbol=symbol,
        timestamp=timestamp,
        spot=spot,
        strikes=strikes,
        expiry_dates=expiry_dates,
        hist_vol=hist_vol,
        iv_rank=iv_rank,
        risk_free_rate=risk_free_rate,
        iv_skew=iv_skew,
    )
