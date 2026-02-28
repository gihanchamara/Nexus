"""
Core Pydantic schemas for Nexus.

All market data schemas are intentionally aligned with ib_insync field names
so data flows from the broker with zero transformation.

ib_insync reference types:
  BarData       → .date .open .high .low .close .volume .average .barCount
  Ticker        → .contract .bid .ask .last .bidSize .askSize .lastSize .volume ...
  Contract      → .symbol .secType .exchange .primaryExch .currency .conId
  Trade         → .contract .order .orderStatus .fills .log
  Fill          → .contract .execution .commissionReport .time
  Position      → .account .contract .position .avgCost
  NewsBulletin  → .msgId .msgType .message .origExchange
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


# ─── Enums ───────────────────────────────────────────────────────────────────

class TradingMode(str, Enum):
    PAPER = "PAPER"
    LIVE = "LIVE"
    BACKTEST = "BACKTEST"


class SecType(str, Enum):
    STK = "STK"
    OPT = "OPT"
    FUT = "FUT"
    CASH = "CASH"
    BOND = "BOND"
    IND = "IND"


class Direction(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


class OrderType(str, Enum):
    MKT = "MKT"
    LMT = "LMT"
    STP = "STP"
    STP_LMT = "STP_LMT"
    TRAIL = "TRAIL"
    BRACKET = "BRACKET"


class OrderStatus(str, Enum):
    PENDING = "PENDING"
    SUBMITTED = "SUBMITTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"


class ComponentType(str, Enum):
    INGESTION = "INGESTION"
    STRATEGY = "STRATEGY"
    AI_MODEL = "AI_MODEL"
    RISK_MANAGER = "RISK_MANAGER"
    ORDER_MANAGER = "ORDER_MANAGER"
    PAPER_ENGINE = "PAPER_ENGINE"
    BACKTEST_ENGINE = "BACKTEST_ENGINE"
    SCHEDULER = "SCHEDULER"
    SYSTEM = "SYSTEM"


class TelemetryLevel(str, Enum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARN = "WARN"
    ERROR = "ERROR"


# ─── Market Data ─────────────────────────────────────────────────────────────

class ContractInfo(BaseModel):
    """Mirrors ib_insync.Contract essential fields."""
    symbol: str
    sec_type: SecType = SecType.STK
    exchange: str = "SMART"
    primary_exchange: str = ""
    currency: str = "USD"
    conid: int | None = None          # IBKR contract ID (populated after resolution)


class BarData(BaseModel):
    """
    Mirrors ib_insync.BarData exactly.
    Field names kept in snake_case; map from camelCase on ingest.
    """
    timestamp: datetime               # ib_insync: .date (converted to datetime)
    symbol: str
    frequency: str                    # '1m', '5m', '1h', '1d'
    open: float
    high: float
    low: float
    close: float
    volume: int = 0
    average: float = 0.0             # VWAP (ib_insync: .average)
    bar_count: int = 0               # trades in bar (ib_insync: .barCount)
    source: str = "IBKR"


class TickData(BaseModel):
    """
    Mirrors key fields from ib_insync.Ticker.
    Not all fields are mandatory — IBKR sends what it has.
    """
    timestamp: datetime
    symbol: str
    bid: float | None = None
    ask: float | None = None
    last: float | None = None
    bid_size: float | None = None    # ib_insync: .bidSize
    ask_size: float | None = None    # ib_insync: .askSize
    last_size: float | None = None   # ib_insync: .lastSize
    volume: int | None = None
    open: float | None = None
    high: float | None = None
    low: float | None = None
    close: float | None = None       # previous close (ib_insync: .close)
    halted: bool = False


class NewsEvent(BaseModel):
    """Mirrors ib_insync.NewsBulletin + structured news API payloads."""
    timestamp: datetime
    source: str                       # 'IBKR', 'BENZINGA', 'NEWSAPI', etc.
    headline: str
    body: str = ""
    symbols: list[str] = Field(default_factory=list)
    sentiment: float | None = None    # -1.0 to 1.0 (populated by AI engine)
    url: str = ""
    msg_id: str = ""                  # ib_insync: .msgId


# ─── Options Data ────────────────────────────────────────────────────────────

class OptionsContract(BaseModel):
    """
    A single options contract with market data and Greeks.
    Fields sourced from ib_insync Ticker + computed Black-Scholes Greeks.
    """
    symbol: str              # underlying symbol (e.g. 'SPY')
    right: str               # 'C' (call) or 'P' (put)
    strike: float
    expiry: str              # IBKR format: 'YYYYMMDD'
    expiry_date: date
    dte: int                 # calendar days to expiry
    bid: float = 0.0
    ask: float = 0.0
    iv: float = 0.0          # implied volatility (decimal, e.g. 0.20 = 20%)
    delta: float = 0.0       # +0.0 to +1.0 for calls; -1.0 to 0.0 for puts
    gamma: float = 0.0
    theta: float = 0.0       # daily time decay (negative for long options)
    vega: float = 0.0        # per 1% IV change
    rho: float = 0.0
    open_interest: int = 0
    volume: int = 0
    conid: int | None = None  # IBKR contract ID

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0 if self.bid and self.ask else 0.0

    @property
    def spread(self) -> float:
        return self.ask - self.bid if self.ask and self.bid else 0.0


class OptionsLeg(BaseModel):
    """A single leg within a multi-leg options order (Iron Condor, Spread, etc.)."""
    action: str       # 'BUY' or 'SELL'
    right: str        # 'C' or 'P'
    strike: float
    expiry: str       # 'YYYYMMDD'
    quantity: int
    conid: int | None = None
    bid: float = 0.0
    ask: float = 0.0

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0


class OptionsChainSnapshot(BaseModel):
    """
    Full options chain for a symbol at a point in time.
    Published to channel: market.options_chain
    """
    symbol: str
    spot_price: float
    timestamp: datetime
    contracts: list[OptionsContract] = Field(default_factory=list)
    iv_rank: float | None = None        # IVR: 0–100
    iv_percentile: float | None = None  # IVP: 0–100

    def calls(self) -> list[OptionsContract]:
        return [c for c in self.contracts if c.right == "C"]

    def puts(self) -> list[OptionsContract]:
        return [c for c in self.contracts if c.right == "P"]

    def in_dte_range(self, min_dte: int, max_dte: int) -> list[OptionsContract]:
        return [c for c in self.contracts if min_dte <= c.dte <= max_dte]

    def by_expiry(self, expiry: str) -> list[OptionsContract]:
        return [c for c in self.contracts if c.expiry == expiry]

    def expiries(self) -> list[str]:
        return sorted({c.expiry for c in self.contracts})

    def nearest_delta(
        self,
        target_delta: float,
        right: str,
        min_dte: int = 15,
        max_dte: int = 60,
    ) -> OptionsContract | None:
        """Find the contract whose absolute delta is closest to target_delta."""
        candidates = [
            c for c in self.contracts
            if c.right == right and min_dte <= c.dte <= max_dte
        ]
        if not candidates:
            return None
        return min(candidates, key=lambda c: abs(abs(c.delta) - abs(target_delta)))

    def nearest_strike(self, strike: float, right: str, expiry: str) -> OptionsContract | None:
        candidates = [c for c in self.contracts if c.right == right and c.expiry == expiry]
        if not candidates:
            return None
        return min(candidates, key=lambda c: abs(c.strike - strike))


# ─── Greeks ──────────────────────────────────────────────────────────────────

class PortfolioGreeks(BaseModel):
    """Aggregated Greeks across all open options positions in the portfolio."""
    delta: float = 0.0   # net directional exposure (positive = long bias)
    gamma: float = 0.0   # rate of delta change (high near expiry = dangerous)
    theta: float = 0.0   # daily time decay (positive = income, negative = cost)
    vega: float = 0.0    # sensitivity to 1% IV move
    rho: float = 0.0     # sensitivity to 1% interest rate change
    timestamp: datetime = Field(default_factory=datetime.utcnow)


# ─── Risk ────────────────────────────────────────────────────────────────────

class RiskCheckResult(BaseModel):
    """Result of a risk check. Every signal passes through RiskManager before OMS."""
    approved: bool
    reasons: list[str] = Field(default_factory=list)  # empty if approved
    adjusted_quantity: float | None = None             # risk manager may reduce size


# ─── Strategy Signal ─────────────────────────────────────────────────────────

class Signal(BaseModel):
    """
    Output of a strategy. Consumed by the OMS.
    Confidence enables AI-weighted position sizing.
    """
    signal_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    symbol: str
    direction: Direction
    confidence: float = Field(ge=0.0, le=1.0, default=1.0)
    strategy_id: str
    model_version: str = ""
    mode: TradingMode = TradingMode.PAPER
    suggested_quantity: float | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


# ─── Orders ──────────────────────────────────────────────────────────────────

class OrderRequest(BaseModel):
    """Submitted by strategy/UI → OMS."""
    symbol: str
    side: Direction
    order_type: OrderType = OrderType.MKT
    quantity: float
    limit_price: float | None = None
    stop_price: float | None = None
    time_in_force: str = "DAY"
    strategy_id: str | None = None
    signal_id: str | None = None
    session_id: int | None = None


class OrderEvent(BaseModel):
    """Persisted order state — emitted to event bus on every status change."""
    order_id: int | None = None
    ib_order_id: int | None = None
    symbol: str
    side: Direction
    order_type: OrderType
    quantity: float
    limit_price: float | None = None
    stop_price: float | None = None
    status: OrderStatus = OrderStatus.PENDING
    filled_qty: float = 0.0
    avg_fill_price: float = 0.0
    commission: float = 0.0
    mode: TradingMode = TradingMode.PAPER
    timestamp: datetime = Field(default_factory=datetime.utcnow)


# ─── Telemetry ───────────────────────────────────────────────────────────────

class TelemetryEvent(BaseModel):
    """
    Universal event emitted by every component to the event bus.
    The UI subscribes to these for real-time observability.
    """
    event_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    component_id: str                    # e.g. "ibkr_connector", "mean_reversion_v1"
    component_type: ComponentType
    channel: str                         # Redis Streams channel name
    level: TelemetryLevel = TelemetryLevel.INFO
    mode: TradingMode = TradingMode.PAPER
    session_id: int | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    timestamp_utc: datetime = Field(default_factory=datetime.utcnow)

    def to_stream_dict(self) -> dict[str, str]:
        """Serialize to flat string dict for Redis XADD."""
        import json
        return {
            "event_id": self.event_id,
            "component_id": self.component_id,
            "component_type": self.component_type.value,
            "channel": self.channel,
            "level": self.level.value,
            "mode": self.mode.value,
            "session_id": str(self.session_id or ""),
            "payload": json.dumps(self.payload),
            "timestamp_utc": self.timestamp_utc.isoformat(),
        }

    @classmethod
    def from_stream_dict(cls, data: dict[bytes | str, bytes | str]) -> "TelemetryEvent":
        """Deserialize from Redis XREAD response."""
        import json

        def decode(v: bytes | str) -> str:
            return v.decode() if isinstance(v, bytes) else v

        d = {decode(k): decode(v) for k, v in data.items()}
        return cls(
            event_id=d["event_id"],
            component_id=d["component_id"],
            component_type=ComponentType(d["component_type"]),
            channel=d["channel"],
            level=TelemetryLevel(d["level"]),
            mode=TradingMode(d["mode"]),
            session_id=int(d["session_id"]) if d.get("session_id") else None,
            payload=json.loads(d.get("payload", "{}")),
            timestamp_utc=datetime.fromisoformat(d["timestamp_utc"]),
        )
