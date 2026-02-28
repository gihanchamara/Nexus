"""
Strategy Selector AI — Phase 4.

The "meta-brain" of the Nexus platform. Makes holistic decisions about which
strategy families should be active based on current market conditions.

Inputs (read from ai.* channels):
  - RegimeSignal   (ai.regime)   — BULL/BEAR/SIDEWAYS/HIGH_VOL/CRISIS
  - IVSnapshot     (ai.iv_rank)  — IVR, term structure, put/call skew
  - PortfolioGreeks              — current net delta/gamma/theta/vega exposure
  - active strategy list         — what is currently running

Outputs (published to strategy.activation):
  StrategyActivation:
    activate    — list of strategy IDs to start
    deactivate  — list of strategy IDs to pause
    scale_down  — list of strategy IDs to reduce position size
    rationale   — human-readable explanation

Decision Logic:
  Priority 1 — CRISIS override: deactivate ALL premium-selling, scale down all
  Priority 2 — Greeks limits: if net delta or vega is near limit, restrict
  Priority 3 — Regime + IVR matrix (the main table):

    Regime        │  IVR < 30   │  IVR 30-50  │  IVR > 50
    ──────────────┼─────────────┼─────────────┼────────────────
    BULL_TREND    │ MA_Cross ✓  │ MA_Cross ✓  │ CSP ✓
                  │ BullSpread ✓│ CSP ✓       │ CC ✓
    BEAR_TREND    │ BearSpread ✓│ BearSpread ✓│ BearSpread ✓
                  │             │ PP ✓        │ PP ✓
    SIDEWAYS_CHOP │ Straddle ✓  │ IC ✓        │ IC ✓  (primary)
                  │             │ Strangle ✓  │ Strangle ✓
    HIGH_VOL      │ LongVega ✓  │ IC ✓        │ IC ✓
                  │             │             │ ShortStrangle ✓
    CRISIS        │ CASH ⚠      │ CASH ⚠      │ CASH ⚠

Abbreviations: MA_Cross=MovingAverageCrossover, IC=IronCondor, CSP=CashSecuredPut,
  CC=CoveredCall, PP=ProtectivePut, BullSpread=BullCallSpread, BearSpread=BearPutSpread

Published to: strategy.activation (consumed by StrategyRegistry)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from nexus.ai.iv_engine import IVSnapshot
from nexus.ai.regime import RegimeSignal, RegimeState
from nexus.bus.publisher import publisher
from nexus.core.schemas import PortfolioGreeks

logger = logging.getLogger(__name__)

# Greek limits that trigger defensive mode (mirrors risk/limits.py defaults)
_MAX_NET_DELTA_DEFENSIVE = 400.0    # approach the 500 hard limit → start reducing
_MAX_VEGA_DEFENSIVE = 4000.0        # approach the 5000 hard limit


@dataclass
class StrategyActivation:
    """
    Activation instructions published to strategy.activation channel.
    StrategyRegistry consumes this and calls activate/deactivate on instances.
    """
    activate: list[str] = field(default_factory=list)      # strategy_ids to start
    deactivate: list[str] = field(default_factory=list)    # strategy_ids to pause
    scale_down: list[str] = field(default_factory=list)    # reduce position sizes
    regime: str = "UNKNOWN"
    iv_rank: float = 50.0
    rationale: str = ""
    timestamp: datetime = field(default_factory=datetime.utcnow)

    def to_dict(self) -> dict[str, Any]:
        return {
            "activate": self.activate,
            "deactivate": self.deactivate,
            "scale_down": self.scale_down,
            "regime": self.regime,
            "iv_rank": round(self.iv_rank, 1),
            "rationale": self.rationale,
            "timestamp": self.timestamp.isoformat(),
        }


class StrategySelector:
    """
    Rule-based strategy activation engine with optional ML enhancement.

    The selector is called periodically (typically once per bar on a primary
    instrument like SPY) rather than on every bar for every symbol.

    It evaluates the combined state of:
      - current market regime
      - implied volatility rank
      - current portfolio Greeks exposure
    and produces a StrategyActivation directive for the registry.
    """

    def __init__(self) -> None:
        # Strategy ID → strategy family tag (set by operator at startup)
        self._strategy_tags: dict[str, list[str]] = {}
        self._last_activation: StrategyActivation | None = None
        self._evaluation_count: int = 0

    def register_strategy(self, strategy_id: str, tags: list[str]) -> None:
        """
        Register a strategy with its family tags for activation routing.

        Tags map strategies to regime suitability.
        Example tags: ['equity', 'trend_following', 'ma_crossover']
                      ['options', 'premium_selling', 'iron_condor']
                      ['options', 'directional', 'bull_call_spread']

        Called at startup by the application layer.
        """
        self._strategy_tags[strategy_id] = [t.lower() for t in tags]
        logger.debug("StrategySelector: registered %s with tags %s", strategy_id, tags)

    async def evaluate(
        self,
        regime: RegimeSignal,
        iv_snap: IVSnapshot | None,
        greeks: PortfolioGreeks | None = None,
    ) -> StrategyActivation:
        """
        Main evaluation entry point. Call once per bar on the primary instrument.

        Returns a StrategyActivation and publishes it to strategy.activation.
        """
        self._evaluation_count += 1
        iv_rank = iv_snap.iv_rank if iv_snap else 50.0

        activation = self._decide(regime, iv_rank, greeks)

        self._last_activation = activation
        await self._emit(activation)

        logger.info(
            "[StrategySelector] %s IVR=%.0f → activate=%s deactivate=%s",
            regime.regime.value, iv_rank, activation.activate, activation.deactivate,
        )
        return activation

    # ─── Core decision logic ──────────────────────────────────────────────────

    def _decide(
        self,
        regime: RegimeSignal,
        iv_rank: float,
        greeks: PortfolioGreeks | None,
    ) -> StrategyActivation:
        """Priority-ordered decision table."""
        act = StrategyActivation(
            regime=regime.regime.value,
            iv_rank=iv_rank,
        )

        # ── Priority 1: CRISIS override ───────────────────────────────────────
        if regime.regime == RegimeState.CRISIS:
            act.deactivate = self._by_tags(["premium_selling", "trend_following", "options"])
            act.rationale = (
                f"CRISIS regime (vol={regime.rolling_vol:.1%}): "
                "all risk-on strategies deactivated; move to defensive"
            )
            return act

        # ── Priority 2: Greeks-driven constraint ──────────────────────────────
        greeks_warning = ""
        if greeks:
            if abs(greeks.delta) > _MAX_NET_DELTA_DEFENSIVE:
                act.scale_down.extend(self._by_tags(["equity", "directional"]))
                greeks_warning = f"net_delta={greeks.delta:.0f} near limit; "
            if abs(greeks.vega) > _MAX_VEGA_DEFENSIVE:
                act.scale_down.extend(self._by_tags(["premium_selling"]))
                greeks_warning += f"vega={greeks.vega:.0f} near limit; "

        # ── Priority 3: Regime × IVR matrix ──────────────────────────────────
        if regime.regime == RegimeState.BULL_TREND:
            if iv_rank >= 50:
                # High IVR bull: sell premium against long equity
                act.activate = self._by_tags(["covered_call", "cash_secured_put"])
                act.deactivate = self._by_tags(["bear_spread", "long_put"])
                act.rationale = f"BULL + IVR={iv_rank:.0f} → sell covered premium"
            else:
                # Low IVR bull: trend-follow, buy cheap upside
                act.activate = self._by_tags(["trend_following", "bull_spread", "ma_crossover"])
                act.deactivate = self._by_tags(["iron_condor", "short_strangle"])
                act.rationale = f"BULL + IVR={iv_rank:.0f} → trend-follow; IV too cheap to sell"

        elif regime.regime == RegimeState.BEAR_TREND:
            if iv_rank >= 50:
                # High IVR bear: protective puts are cheap(ish), sell call spreads
                act.activate = self._by_tags(["bear_spread", "protective_put"])
                act.deactivate = self._by_tags(["trend_following", "covered_call"])
                act.rationale = f"BEAR + IVR={iv_rank:.0f} → bearish premium strategies"
            else:
                # Low IVR bear: buying puts is cheap
                act.activate = self._by_tags(["bear_spread", "long_put"])
                act.deactivate = self._by_tags(["premium_selling", "ma_crossover"])
                act.rationale = f"BEAR + IVR={iv_rank:.0f} → buy cheap downside protection"

        elif regime.regime == RegimeState.SIDEWAYS_CHOP:
            if iv_rank >= 50:
                # Classic Iron Condor / Strangle territory
                act.activate = self._by_tags(["iron_condor", "short_strangle", "premium_selling"])
                act.deactivate = self._by_tags(["trend_following", "ma_crossover"])
                act.rationale = (
                    f"SIDEWAYS + IVR={iv_rank:.0f} → "
                    "ideal premium-selling regime (Iron Condor priority)"
                )
            else:
                # Low IV sideways: define risk with debit spreads or wait
                act.activate = self._by_tags(["iron_condor"])
                act.scale_down = self._by_tags(["premium_selling"])
                act.rationale = f"SIDEWAYS + IVR={iv_rank:.0f} → sideways but IV low; reduce size"

        elif regime.regime == RegimeState.HIGH_VOL:
            if iv_rank >= 60:
                # Elevated vol + high IVR: perfect for premium selling (collect inflated IV)
                act.activate = self._by_tags(["iron_condor", "short_strangle"])
                act.scale_down = self._by_tags(["trend_following"])
                act.rationale = (
                    f"HIGH_VOL + IVR={iv_rank:.0f} → "
                    "sell inflated premium; reduce directional exposure"
                )
            else:
                # High vol but IV not yet elevated: wait for IV to spike
                act.scale_down = self._by_tags(["premium_selling", "trend_following"])
                act.rationale = (
                    f"HIGH_VOL + IVR={iv_rank:.0f} → "
                    "vol elevated but IV hasn't spiked; scale down all"
                )
        else:
            # UNKNOWN or other
            act.rationale = f"regime={regime.regime.value}; no change"

        if greeks_warning:
            act.rationale = greeks_warning + act.rationale

        # Deduplicate lists
        act.activate   = list(dict.fromkeys(act.activate))
        act.deactivate = list(dict.fromkeys(act.deactivate))
        act.scale_down = list(dict.fromkeys(act.scale_down))

        # Strategies can't be in activate AND deactivate
        act.activate = [s for s in act.activate if s not in act.deactivate]

        return act

    # ─── Tag-based strategy lookup ────────────────────────────────────────────

    def _by_tags(self, tags: list[str]) -> list[str]:
        """Return strategy IDs that have ANY of the given tags."""
        tags_lower = [t.lower() for t in tags]
        return [
            sid for sid, stags in self._strategy_tags.items()
            if any(t in stags for t in tags_lower)
        ]

    # ─── Introspection ────────────────────────────────────────────────────────

    def snapshot(self) -> dict[str, Any]:
        return {
            "last_activation": self._last_activation.to_dict() if self._last_activation else {},
            "evaluation_count": self._evaluation_count,
            "registered_strategies": self._strategy_tags,
        }

    async def _emit(self, activation: StrategyActivation) -> None:
        try:
            await publisher.publish("strategy.activation", activation.to_dict())
        except Exception:
            pass


# Module-level singleton
strategy_selector = StrategySelector()
