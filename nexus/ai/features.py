"""
Feature Store — Phase 4.

Computes and caches ML-ready features per symbol from incoming BarData.
No pandas in the hot path — pure deque ring buffers + Python math.

Features computed on every bar update:
  Price/Return  : log_return, rolling_vol_20, rolling_vol_60
  Momentum      : momentum_5, momentum_20, momentum_60  (% price change)
  Technicals    : rsi_14, macd_signal, bollinger_pct, atr_14
  Volume        : volume_ratio_20  (current vs 20-bar avg)

All features are normalised to dimensionless quantities where possible so
they can be fed directly to ML models (HMM, XGBoost) without scaling.

Published to: ai.features  (on every bar update, per symbol)

The FeatureSnapshot is also used by:
  - RegimeDetector.predict()
  - StrategySelector.evaluate()
  - trainer.py (persists to TimescaleDB for training data collection)
"""

from __future__ import annotations

import json
import logging
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime
from math import log, sqrt
from typing import Any

from nexus.bus.publisher import publisher
from nexus.core.schemas import BarData

logger = logging.getLogger(__name__)

# Ring buffer size — enough for 60-bar features + warmup
_BUFFER_SIZE = 300


# ─── Feature Snapshot ─────────────────────────────────────────────────────────

@dataclass
class FeatureSnapshot:
    """
    All ML features for a single symbol at a single point in time.
    Fields are 0.0 until sufficient history is available (warmed up).
    """
    symbol: str
    timestamp: datetime

    # Return / volatility
    log_return: float = 0.0          # log(close_t / close_t-1)
    rolling_vol_20: float = 0.0      # annualised HV over last 20 bars
    rolling_vol_60: float = 0.0      # annualised HV over last 60 bars

    # Momentum (price change normalised by current price)
    momentum_5: float = 0.0          # (close_t - close_t-5) / close_t-5
    momentum_20: float = 0.0         # (close_t - close_t-20) / close_t-20
    momentum_60: float = 0.0         # (close_t - close_t-60) / close_t-60

    # Technicals
    rsi_14: float = 50.0             # 0–100 (neutral = 50)
    macd_signal: float = 0.0         # MACD line value (EMA12 - EMA26)
    bollinger_pct: float = 0.5       # 0 = at lower band, 1 = at upper band
    atr_14: float = 0.0              # average true range (dollar terms)

    # Volume
    volume_ratio_20: float = 1.0     # current vol / 20-bar avg vol

    # Computed flags (useful as categorical features)
    is_warmed_up: bool = False        # True once all features have enough history

    def to_vector(self) -> list[float]:
        """Return feature vector for ML model input (excludes metadata fields)."""
        return [
            self.log_return, self.rolling_vol_20, self.rolling_vol_60,
            self.momentum_5, self.momentum_20, self.momentum_60,
            self.rsi_14 / 100.0,    # normalise to 0–1
            self.macd_signal,
            self.bollinger_pct,
            self.atr_14,
            self.volume_ratio_20,
        ]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ─── Per-symbol rolling state ─────────────────────────────────────────────────

class _SymbolState:
    """Ring-buffer state for one symbol. Not thread-safe (single event loop)."""

    def __init__(self) -> None:
        self.closes:  deque[float] = deque(maxlen=_BUFFER_SIZE)
        self.highs:   deque[float] = deque(maxlen=_BUFFER_SIZE)
        self.lows:    deque[float] = deque(maxlen=_BUFFER_SIZE)
        self.volumes: deque[float] = deque(maxlen=_BUFFER_SIZE)
        self.log_returns: deque[float] = deque(maxlen=_BUFFER_SIZE)

    def push(self, bar: BarData) -> None:
        if self.closes and self.closes[-1] > 0:
            self.log_returns.append(log(bar.close / self.closes[-1]))
        else:
            self.log_returns.append(0.0)
        self.closes.append(bar.close)
        self.highs.append(bar.high)
        self.lows.append(bar.low)
        self.volumes.append(float(bar.volume or 0))

    def compute(self, timestamp: datetime, symbol: str) -> FeatureSnapshot:
        snap = FeatureSnapshot(symbol=symbol, timestamp=timestamp)
        n = len(self.closes)

        if n < 2:
            return snap

        # ── Log return ────────────────────────────────────────────────────────
        snap.log_return = self.log_returns[-1]

        # ── Rolling volatility ────────────────────────────────────────────────
        if n >= 20:
            snap.rolling_vol_20 = _rolling_vol(list(self.log_returns), 20)
        if n >= 60:
            snap.rolling_vol_60 = _rolling_vol(list(self.log_returns), 60)

        # ── Momentum ──────────────────────────────────────────────────────────
        closes = list(self.closes)
        c = closes[-1]
        if n >= 6 and closes[-6] > 0:
            snap.momentum_5 = (c - closes[-6]) / closes[-6]
        if n >= 21 and closes[-21] > 0:
            snap.momentum_20 = (c - closes[-21]) / closes[-21]
        if n >= 61 and closes[-61] > 0:
            snap.momentum_60 = (c - closes[-61]) / closes[-61]

        # ── RSI ───────────────────────────────────────────────────────────────
        if n >= 15:
            snap.rsi_14 = _rsi(closes, 14)

        # ── MACD ──────────────────────────────────────────────────────────────
        if n >= 26:
            snap.macd_signal = _macd_signal(closes)

        # ── Bollinger Band % ──────────────────────────────────────────────────
        if n >= 20:
            snap.bollinger_pct = _bollinger_pct(closes, 20)

        # ── ATR ───────────────────────────────────────────────────────────────
        if n >= 15:
            snap.atr_14 = _atr(
                list(self.highs), list(self.lows), closes, 14
            )

        # ── Volume ratio ──────────────────────────────────────────────────────
        vols = list(self.volumes)
        if n >= 21 and len(vols) >= 21:
            avg_vol = sum(vols[-21:-1]) / 20
            snap.volume_ratio_20 = vols[-1] / avg_vol if avg_vol > 0 else 1.0

        # Mark warm-up complete once all 60-bar features are ready
        snap.is_warmed_up = n >= 61

        return snap


# ─── Feature Store ────────────────────────────────────────────────────────────

class FeatureStore:
    """
    Central in-memory feature store. Maintains rolling state per symbol.

    Usage:
        store = FeatureStore()
        snap = await store.update(bar)       # returns latest FeatureSnapshot
        snap = store.get("AAPL")             # latest without updating
        history = store.history("AAPL", 50) # last 50 snapshots

    Also publishes to ai.features on each update (consumed by RegimeDetector etc.)
    """

    def __init__(self, publish: bool = True) -> None:
        self._states: dict[str, _SymbolState] = {}
        self._latest: dict[str, FeatureSnapshot] = {}
        self._history: dict[str, deque[FeatureSnapshot]] = {}
        self._publish = publish

    async def update(self, bar: BarData) -> FeatureSnapshot:
        """
        Ingest a new bar and return the updated FeatureSnapshot for its symbol.
        Publishes to ai.features if publish=True.
        """
        sym = bar.symbol
        if sym not in self._states:
            self._states[sym] = _SymbolState()
            self._history[sym] = deque(maxlen=_BUFFER_SIZE)

        state = self._states[sym]
        state.push(bar)
        snap = state.compute(bar.timestamp, sym)

        self._latest[sym] = snap
        self._history[sym].append(snap)

        if self._publish:
            await self._emit(snap)

        return snap

    def get(self, symbol: str) -> FeatureSnapshot | None:
        """Return the latest FeatureSnapshot for a symbol (None if unseen)."""
        return self._latest.get(symbol)

    def history(self, symbol: str, n: int = 60) -> list[FeatureSnapshot]:
        """Return the last n FeatureSnapshots for a symbol."""
        h = self._history.get(symbol)
        if not h:
            return []
        return list(h)[-n:]

    def symbols(self) -> list[str]:
        return list(self._states.keys())

    def snapshot(self) -> dict[str, Any]:
        """Dashboard snapshot: latest features per symbol."""
        return {
            sym: snap.to_dict()
            for sym, snap in self._latest.items()
        }

    async def _emit(self, snap: FeatureSnapshot) -> None:
        try:
            payload: dict[str, Any] = {
                "symbol": snap.symbol,
                "timestamp": snap.timestamp.isoformat(),
                "features": json.dumps(snap.to_dict()),
            }
            await publisher.publish("ai.features", payload)
        except Exception:
            pass   # feature publish never blocks strategy execution


# ─── Technical indicator implementations (pure Python, no pandas) ─────────────

def _rolling_vol(log_returns: list[float], window: int) -> float:
    """Annualised rolling historical volatility from log returns."""
    r = log_returns[-window:]
    if len(r) < 2:
        return 0.0
    mean = sum(r) / len(r)
    variance = sum((x - mean) ** 2 for x in r) / (len(r) - 1)
    return sqrt(variance * 252)


def _rsi(closes: list[float], period: int = 14) -> float:
    """Relative Strength Index — Wilder smoothing."""
    if len(closes) < period + 1:
        return 50.0
    gains, losses = [], []
    for i in range(1, len(closes)):
        delta = closes[i] - closes[i - 1]
        gains.append(max(0.0, delta))
        losses.append(max(0.0, -delta))

    # Initial average
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    # Wilder smoothing for remaining bars
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def _ema(prices: list[float], period: int) -> float:
    """Exponential moving average."""
    if not prices:
        return 0.0
    k = 2.0 / (period + 1)
    ema = prices[0]
    for p in prices[1:]:
        ema = p * k + ema * (1 - k)
    return ema


def _macd_signal(closes: list[float], fast: int = 12, slow: int = 26) -> float:
    """MACD line = EMA(fast) - EMA(slow). Returns 0 if insufficient data."""
    if len(closes) < slow:
        return 0.0
    # Use 3× period for EMA warmup to reduce initialisation bias
    fast_prices = closes[-fast * 3:] if len(closes) >= fast * 3 else closes
    slow_prices = closes[-slow * 2:] if len(closes) >= slow * 2 else closes
    return _ema(fast_prices, fast) - _ema(slow_prices, slow)


def _bollinger_pct(closes: list[float], window: int = 20) -> float:
    """
    Bollinger Band %B: 0 = at lower band, 0.5 = at midpoint, 1 = at upper band.
    Values outside [0, 1] indicate price beyond bands.
    """
    w = closes[-window:]
    if len(w) < window:
        return 0.5
    mid = sum(w) / len(w)
    std = sqrt(sum((p - mid) ** 2 for p in w) / len(w))
    if std == 0:
        return 0.5
    upper = mid + 2 * std
    lower = mid - 2 * std
    if upper == lower:
        return 0.5
    return (closes[-1] - lower) / (upper - lower)


def _atr(highs: list[float], lows: list[float], closes: list[float],
         period: int = 14) -> float:
    """Average True Range (Wilder)."""
    if len(closes) < period + 1:
        return 0.0
    trs = []
    for i in range(1, len(closes)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        trs.append(tr)
    if len(trs) < period:
        return sum(trs) / len(trs)

    # Wilder smoothing
    atr = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr = (atr * (period - 1) + tr) / period
    return atr


# Module-level singleton
feature_store = FeatureStore()
