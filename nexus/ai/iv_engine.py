"""
IV Engine — Phase 4.

Tracks implied volatility metrics per symbol:
  - IV Rank (IVR)       : where current IV sits in its 52-week range (0–100)
  - IV Percentile (IVP) : fraction of days IV was below current level (0–100)
  - Term structure slope : short_term_iv - long_term_iv
                           positive = contango (normal), negative = backwardation (fear)
  - Put/call IV skew     : atm_put_iv - atm_call_iv
                           large positive = market pricing in downside fear

These metrics are consumed by:
  - IronCondorStrategy.on_options_chain() — IVR gate check (already in Phase 2)
  - StrategySelector.evaluate()           — regime-adjusted strategy activation
  - Trainer — feature input for regime classifier

Data sources:
  - Live: OptionsChainSnapshot from market.options_chain (IBKR options data)
  - Backtest: synthetic chains from nexus.backtest.options_pricer

Published to: ai.iv_rank  (on every chain update)
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from nexus.bus.publisher import publisher
from nexus.core.schemas import OptionsChainSnapshot

logger = logging.getLogger(__name__)

# Rolling history window for IVR/IVP computation (252 trading days ≈ 1 year)
_IV_HISTORY_WINDOW = 252


@dataclass
class IVSnapshot:
    """Current IV metrics for one underlying symbol."""
    symbol: str
    timestamp: datetime
    atm_iv: float = 0.0           # ATM implied volatility (decimal: 0.20 = 20%)
    iv_rank: float = 50.0         # IVR 0–100
    iv_percentile: float = 50.0   # IVP 0–100
    term_slope: float = 0.0       # short_iv - long_iv (positive = normal contango)
    put_call_skew: float = 0.0    # ATM put IV - ATM call IV (positive = put premium)
    short_term_iv: float = 0.0    # IV of nearest expiry (e.g. ~30 DTE)
    long_term_iv: float = 0.0     # IV of longer expiry (e.g. ~60 DTE)
    chain_size: int = 0           # number of contracts in chain

    @property
    def regime_hint(self) -> str:
        """Human-readable IV regime hint for dashboard display."""
        if self.iv_rank >= 70:
            return "ELEVATED — consider premium selling (Iron Condor, CSP)"
        elif self.iv_rank >= 50:
            return "MODERATE — neutral; favour defined-risk spreads"
        elif self.iv_rank >= 30:
            return "LOW-MODERATE — directional plays; avoid naked premium selling"
        else:
            return "LOW — buy options (cheap); avoid selling premium"

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "timestamp": self.timestamp.isoformat(),
            "atm_iv": round(self.atm_iv, 4),
            "iv_rank": round(self.iv_rank, 1),
            "iv_percentile": round(self.iv_percentile, 1),
            "term_slope": round(self.term_slope, 4),
            "put_call_skew": round(self.put_call_skew, 4),
            "short_term_iv": round(self.short_term_iv, 4),
            "long_term_iv": round(self.long_term_iv, 4),
            "chain_size": self.chain_size,
            "regime_hint": self.regime_hint,
        }


class IVEngine:
    """
    Maintains per-symbol IV history and computes IVR/IVP/term structure.

    Lifecycle:
      - update_from_chain(chain) — call on every market.options_chain event
      - get(symbol)              — latest IVSnapshot
      - snapshot()               — all symbols for dashboard

    IVR formula: (current_iv - iv_52w_low) / (iv_52w_high - iv_52w_low) × 100
    IVP formula: # of days in past year where IV < current_iv / 252 × 100
    """

    def __init__(self) -> None:
        self._latest: dict[str, IVSnapshot] = {}
        # Rolling history of daily ATM IV values per symbol
        self._iv_history: dict[str, deque[float]] = {}

    async def update_from_chain(self, chain: OptionsChainSnapshot) -> IVSnapshot:
        """
        Process a fresh options chain snapshot and compute IV metrics.
        Publishes to ai.iv_rank.
        """
        sym = chain.symbol

        if sym not in self._iv_history:
            self._iv_history[sym] = deque(maxlen=_IV_HISTORY_WINDOW)

        snap = self._compute_snapshot(chain)
        self._latest[sym] = snap

        # Append current ATM IV to rolling history for IVR computation
        if snap.atm_iv > 0:
            self._iv_history[sym].append(snap.atm_iv)
            # Recompute IVR/IVP now that history is updated
            snap = self._apply_historical_metrics(snap, sym)
            self._latest[sym] = snap

        await self._emit(snap)
        return snap

    def get(self, symbol: str) -> IVSnapshot | None:
        return self._latest.get(symbol)

    def get_iv_rank(self, symbol: str) -> float:
        """Return current IVR for symbol, or 50.0 if unknown."""
        snap = self._latest.get(symbol)
        return snap.iv_rank if snap else 50.0

    def get_term_slope(self, symbol: str) -> float:
        """Return current term structure slope, or 0.0 if unknown."""
        snap = self._latest.get(symbol)
        return snap.term_slope if snap else 0.0

    def snapshot(self) -> dict[str, Any]:
        return {sym: snap.to_dict() for sym, snap in self._latest.items()}

    # ─── Internal computation ──────────────────────────────────────────────────

    def _compute_snapshot(self, chain: OptionsChainSnapshot) -> IVSnapshot:
        """Extract IV metrics from a chain snapshot."""
        snap = IVSnapshot(
            symbol=chain.symbol,
            timestamp=chain.timestamp,
            chain_size=len(chain.contracts),
        )

        # ── ATM IV (average of ATM call and put IV) ────────────────────────────
        spot = chain.spot_price
        atm_call = chain.nearest_delta(target_delta=0.50, right="C", min_dte=15, max_dte=60)
        atm_put  = chain.nearest_delta(target_delta=0.50, right="P", min_dte=15, max_dte=60)

        atm_call_iv = atm_call.iv if atm_call and atm_call.iv > 0 else 0.0
        atm_put_iv  = atm_put.iv  if atm_put  and atm_put.iv  > 0 else 0.0

        if atm_call_iv > 0 and atm_put_iv > 0:
            snap.atm_iv = (atm_call_iv + atm_put_iv) / 2.0
        elif atm_call_iv > 0:
            snap.atm_iv = atm_call_iv
        elif atm_put_iv > 0:
            snap.atm_iv = atm_put_iv

        # ── Put/call skew ─────────────────────────────────────────────────────
        if atm_call_iv > 0 and atm_put_iv > 0:
            snap.put_call_skew = atm_put_iv - atm_call_iv

        # ── Term structure slope (short vs long DTE) ──────────────────────────
        short_iv = self._avg_iv_for_dte_range(chain, 20, 40)
        long_iv  = self._avg_iv_for_dte_range(chain, 45, 75)

        snap.short_term_iv = short_iv
        snap.long_term_iv  = long_iv

        if short_iv > 0 and long_iv > 0:
            # Positive = contango (normal); Negative = backwardation (fear / event)
            snap.term_slope = short_iv - long_iv

        # ── IVR/IVP from chain snapshot if provided (live data) ───────────────
        if chain.iv_rank is not None:
            snap.iv_rank = chain.iv_rank
        if chain.iv_percentile is not None:
            snap.iv_percentile = chain.iv_percentile

        return snap

    def _apply_historical_metrics(self, snap: IVSnapshot, symbol: str) -> IVSnapshot:
        """Recompute IVR and IVP from rolling history."""
        history = list(self._iv_history[symbol])
        if len(history) < 5:
            return snap

        current = snap.atm_iv
        iv_low  = min(history)
        iv_high = max(history)

        if iv_high > iv_low:
            snap.iv_rank = (current - iv_low) / (iv_high - iv_low) * 100.0
        else:
            snap.iv_rank = 50.0

        below_count = sum(1 for h in history if h < current)
        snap.iv_percentile = below_count / len(history) * 100.0

        return snap

    @staticmethod
    def _avg_iv_for_dte_range(
        chain: OptionsChainSnapshot,
        min_dte: int,
        max_dte: int,
    ) -> float:
        """Average IV of contracts in a DTE band (ATM ±20% moneyness)."""
        spot = chain.spot_price
        contracts = [
            c for c in chain.contracts
            if min_dte <= c.dte <= max_dte
            and c.iv > 0
            and spot > 0
            and 0.80 <= c.strike / spot <= 1.20   # near ATM only
        ]
        if not contracts:
            return 0.0
        return sum(c.iv for c in contracts) / len(contracts)

    async def _emit(self, snap: IVSnapshot) -> None:
        try:
            await publisher.publish("ai.iv_rank", snap.to_dict())
        except Exception:
            pass


# Module-level singleton
iv_engine = IVEngine()
