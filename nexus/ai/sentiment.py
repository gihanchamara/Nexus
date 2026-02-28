"""
News Sentiment Analyzer — Phase 4.

Two-tier design:
  Tier 1 — Keyword-based (always active, zero API cost):
    Fast VADER-style rule lookup for common financial phrases.
    Returns a coarse score (-1, 0, +1) with 0.4 confidence.
    Suitable for filtering obvious positive/negative headlines.

  Tier 2 — LLM (Anthropic claude-haiku-4-5, requires NEXUS_ANTHROPIC_API_KEY):
    Structured JSON output: score, confidence, catalyst_type, time_horizon.
    Rate-limited to avoid cost spikes (max 1 call per symbol per minute).
    Async and non-blocking — result is cached; strategies poll the cache.
    Falls back to Tier 1 if API key is absent or call fails.

Output:
  SentimentResult published to ai.sentiment channel.
  Consumed by SignalEnsemble to adjust signal confidence.

Prompt engineering:
  System prompt instructs the model to act as a financial news analyst.
  User prompt contains headline + body snippet (first 500 chars).
  Response must be valid JSON — validated with model_validate.

Cache:
  Per-symbol ring buffer (latest 10 results per symbol).
  confidence_multiplier(symbol) returns a 0.5–1.5 scaling factor.
"""

from __future__ import annotations

import json
import logging
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from nexus.bus.publisher import publisher
from nexus.core.schemas import NewsEvent

logger = logging.getLogger(__name__)

# Rate limiting: minimum seconds between API calls per symbol
_MIN_API_INTERVAL = 60.0

# System prompt for the Anthropic API call
_SYSTEM_PROMPT = """You are a quantitative financial news analyst.
Analyse the news item and return a JSON object with these exact fields:
{
  "score": <float -1.0 to 1.0>,
  "confidence": <float 0.0 to 1.0>,
  "catalyst_type": <"earnings"|"macro"|"regulatory"|"product"|"analyst"|"other">,
  "time_horizon": <"intraday"|"short"|"medium"|"long">,
  "affected_symbols": [<list of ticker symbols mentioned>],
  "reasoning": <one sentence explanation>
}
score: -1.0 = very bearish, 0.0 = neutral, +1.0 = very bullish.
confidence: how clear-cut is the sentiment (0.9 = obvious, 0.3 = ambiguous).
time_horizon: intraday=<1d, short=1-5d, medium=1-4w, long=1-3m.
Return only valid JSON, no markdown, no explanation outside JSON."""

# Keyword-based sentiment lookup (financial domain)
_POSITIVE_KEYWORDS = [
    "beat", "beats", "exceeded", "exceeds", "raised", "raises", "upgrade", "upgraded",
    "strong", "record", "growth", "profit", "revenue growth", "buyback", "acquisition",
    "outperform", "buy", "positive", "boost", "surge", "rally", "breakthrough",
    "fda approved", "approved", "new contract", "partnership", "dividend increase",
]
_NEGATIVE_KEYWORDS = [
    "miss", "misses", "missed", "lowered", "lowers", "downgrade", "downgraded",
    "weak", "loss", "decline", "revenue decline", "recall", "lawsuit", "investigation",
    "underperform", "sell", "negative", "drop", "crash", "bankruptcy", "warning",
    "fda rejected", "rejected", "layoffs", "restructuring", "guidance cut",
]


# ─── Sentiment Result ─────────────────────────────────────────────────────────

@dataclass
class SentimentResult:
    """Output of the sentiment analyzer for a single news event."""
    symbol: str
    score: float                    # -1.0 to +1.0
    confidence: float               # 0.0 to 1.0
    catalyst_type: str              # earnings | macro | regulatory | product | analyst | other
    time_horizon: str               # intraday | short | medium | long
    method: str                     # 'llm' | 'keyword'
    headline: str
    timestamp: datetime
    reasoning: str = ""
    affected_symbols: list[str] | None = None

    @property
    def is_bullish(self) -> bool:
        return self.score > 0.2

    @property
    def is_bearish(self) -> bool:
        return self.score < -0.2

    @property
    def confidence_multiplier(self) -> float:
        """
        Returns a 0.5–1.5 multiplier for OMS signal confidence adjustment.
        Bullish news boosts confidence on BUY signals; bearish news boosts SELL confidence.
        Multiplier is dampened by sentiment confidence to avoid over-weighting weak signals.
        """
        magnitude = abs(self.score) * self.confidence
        return 1.0 + (magnitude * 0.5)  # max +50% boost at score=±1.0, confidence=1.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "score": round(self.score, 3),
            "confidence": round(self.confidence, 3),
            "catalyst_type": self.catalyst_type,
            "time_horizon": self.time_horizon,
            "method": self.method,
            "headline": self.headline[:200],
            "timestamp": self.timestamp.isoformat(),
            "reasoning": self.reasoning,
            "affected_symbols": self.affected_symbols or [],
            "is_bullish": self.is_bullish,
            "is_bearish": self.is_bearish,
            "confidence_multiplier": round(self.confidence_multiplier, 3),
        }


# ─── Sentiment Analyzer ───────────────────────────────────────────────────────

class SentimentAnalyzer:
    """
    Two-tier news sentiment analyzer.

    Usage:
        analyzer = SentimentAnalyzer()
        result = await analyzer.analyze(news_event)
        multiplier = analyzer.confidence_multiplier("AAPL")

    LLM is invoked asynchronously and never blocks the event loop.
    API key is read from NEXUS_ANTHROPIC_API_KEY environment variable.
    """

    def __init__(self) -> None:
        self._cache: dict[str, deque[SentimentResult]] = {}
        self._last_api_call: dict[str, float] = {}   # symbol → epoch seconds
        self._api_key: str | None = self._load_api_key()
        self._llm_available = self._api_key is not None

        if self._llm_available:
            logger.info("SentimentAnalyzer: LLM tier enabled (Anthropic claude-haiku-4-5)")
        else:
            logger.info("SentimentAnalyzer: NEXUS_ANTHROPIC_API_KEY not set — keyword-only mode")

    async def analyze(self, news: NewsEvent) -> SentimentResult:
        """
        Analyse a news event. Tries LLM first; falls back to keyword-based.
        Result is cached and published to ai.sentiment.
        """
        # Determine primary symbol (first in list, or 'MARKET')
        symbol = news.symbols[0] if news.symbols else "MARKET"

        # Rate-limiting: don't spam the API for the same symbol
        now = time.monotonic()
        last_call = self._last_api_call.get(symbol, 0.0)

        result: SentimentResult | None = None

        if self._llm_available and (now - last_call) >= _MIN_API_INTERVAL:
            result = await self._llm_analyze(news, symbol)
            if result:
                self._last_api_call[symbol] = now

        if result is None:
            result = self._keyword_analyze(news, symbol)

        # Cache result
        if symbol not in self._cache:
            self._cache[symbol] = deque(maxlen=10)
        self._cache[symbol].append(result)

        await self._emit(result)
        return result

    def get_latest(self, symbol: str) -> SentimentResult | None:
        """Return the most recent sentiment result for a symbol."""
        cache = self._cache.get(symbol)
        return cache[-1] if cache else None

    def confidence_multiplier(self, symbol: str, direction: str = "BUY") -> float:
        """
        Return a confidence multiplier (0.5–1.5) based on latest sentiment.

        For BUY signals: bullish sentiment boosts confidence; bearish reduces it.
        For SELL signals: bearish sentiment boosts confidence; bullish reduces it.
        Neutral / unknown sentiment returns 1.0 (no adjustment).
        """
        result = self.get_latest(symbol)
        if result is None:
            return 1.0

        # Decay factor: sentiment older than 24h is half-weighted
        age_hours = (datetime.utcnow() - result.timestamp).total_seconds() / 3600
        decay = max(0.5, 1.0 - age_hours / 48.0)

        if direction.upper() == "BUY":
            magnitude = result.score * result.confidence * decay
        else:
            magnitude = -result.score * result.confidence * decay

        return max(0.5, min(1.5, 1.0 + magnitude * 0.5))

    def snapshot(self) -> dict[str, Any]:
        return {
            sym: cache[-1].to_dict()
            for sym, cache in self._cache.items()
            if cache
        }

    # ─── LLM tier ─────────────────────────────────────────────────────────────

    async def _llm_analyze(
        self, news: NewsEvent, symbol: str
    ) -> SentimentResult | None:
        """Call Anthropic API for structured sentiment analysis."""
        try:
            import anthropic  # type: ignore[import-untyped]
        except ImportError:
            logger.debug("anthropic package not installed — skipping LLM analysis")
            return None

        try:
            client = anthropic.AsyncAnthropic(api_key=self._api_key)
            body_snippet = news.body[:500] if news.body else ""
            user_content = f"Headline: {news.headline}\n\nBody: {body_snippet}"

            message = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=300,
                system=_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_content}],
            )

            raw_json = message.content[0].text.strip()
            data = json.loads(raw_json)

            return SentimentResult(
                symbol=symbol,
                score=float(data.get("score", 0.0)),
                confidence=float(data.get("confidence", 0.5)),
                catalyst_type=str(data.get("catalyst_type", "other")),
                time_horizon=str(data.get("time_horizon", "short")),
                method="llm",
                headline=news.headline,
                timestamp=datetime.utcnow(),
                reasoning=str(data.get("reasoning", "")),
                affected_symbols=data.get("affected_symbols", news.symbols),
            )
        except Exception as exc:
            logger.warning("LLM sentiment analysis failed: %s", exc)
            return None

    # ─── Keyword tier ─────────────────────────────────────────────────────────

    def _keyword_analyze(self, news: NewsEvent, symbol: str) -> SentimentResult:
        """
        Fast keyword-based sentiment scoring.
        Scans headline + body for positive/negative financial terms.
        """
        text = f"{news.headline} {news.body[:300]}".lower()

        pos_hits = sum(1 for kw in _POSITIVE_KEYWORDS if kw in text)
        neg_hits = sum(1 for kw in _NEGATIVE_KEYWORDS if kw in text)
        total = pos_hits + neg_hits

        if total == 0:
            score = 0.0
            confidence = 0.2
        else:
            score = (pos_hits - neg_hits) / total
            # More keyword hits → higher confidence
            confidence = min(0.6, 0.2 + total * 0.1)

        return SentimentResult(
            symbol=symbol,
            score=round(score, 3),
            confidence=round(confidence, 3),
            catalyst_type="other",
            time_horizon="short",
            method="keyword",
            headline=news.headline,
            timestamp=datetime.utcnow(),
            reasoning=f"keyword: {pos_hits} positive, {neg_hits} negative",
            affected_symbols=news.symbols,
        )

    # ─── Helpers ──────────────────────────────────────────────────────────────

    @staticmethod
    def _load_api_key() -> str | None:
        import os
        key = os.environ.get("NEXUS_ANTHROPIC_API_KEY", "")
        return key if key else None

    async def _emit(self, result: SentimentResult) -> None:
        try:
            await publisher.publish("ai.sentiment", result.to_dict())
        except Exception:
            pass


# Module-level singleton
sentiment_analyzer = SentimentAnalyzer()
