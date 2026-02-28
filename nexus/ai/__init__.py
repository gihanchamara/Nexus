"""
Nexus AI Engine — Phase 4.

Seven components that make Nexus "AI-first":

  FeatureStore        — real-time rolling features from market bars (no pandas in hot path)
  IVEngine            — IV Rank, IV Percentile, term structure slope, put/call skew
  RegimeDetector      — HMM + rule-based market regime classifier
                        (BULL_TREND | BEAR_TREND | SIDEWAYS_CHOP | HIGH_VOL | CRISIS)
  SentimentAnalyzer   — Anthropic API (claude-haiku-4-5) news sentiment scorer
  StrategySelector    — meta-AI: Regime + IVR + Greeks → activate/deactivate strategies
  SignalEnsemble      — Sharpe-weighted multi-strategy signal aggregation
  PortfolioOptimizer  — Kelly / Mean-Variance / Risk-Parity capital allocation

All components:
  - Consume from Redis Streams (same bus as strategies)
  - Publish to dedicated ai.* channels
  - Degrade gracefully if optional ML deps (hmmlearn, xgboost, anthropic) are absent
  - Never block the event loop (API calls are async; ML inference is microseconds)

Channel map:
  Reads : market.bar.*, market.news, market.options_chain, strategy.signal
  Writes: ai.features, ai.regime, ai.iv_rank, ai.sentiment,
          ai.signal, ai.allocation, strategy.activation
"""

from nexus.ai.features import FeatureSnapshot, FeatureStore
from nexus.ai.iv_engine import IVEngine, IVSnapshot
from nexus.ai.portfolio_optimizer import AllocationResult, PortfolioOptimizer
from nexus.ai.regime import RegimeDetector, RegimeSignal, RegimeState
from nexus.ai.sentiment import SentimentAnalyzer, SentimentResult
from nexus.ai.signal_ensemble import EnsembleSignal, SignalEnsemble
from nexus.ai.strategy_selector import StrategyActivation, StrategySelector

__all__ = [
    "FeatureStore",
    "FeatureSnapshot",
    "IVEngine",
    "IVSnapshot",
    "RegimeDetector",
    "RegimeSignal",
    "RegimeState",
    "SentimentAnalyzer",
    "SentimentResult",
    "StrategySelector",
    "StrategyActivation",
    "SignalEnsemble",
    "EnsembleSignal",
    "PortfolioOptimizer",
    "AllocationResult",
]
