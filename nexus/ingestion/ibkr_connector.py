"""
IBKR Connection Manager.

Wraps ib_insync to provide:
  - Async connection with auto-reconnect watchdog
  - Live tick and bar streaming → published to Redis event bus
  - Historical data requests with rate-limit throttling (IBKR: ~60 req/10 min)
  - Contract resolution (symbol string → ib_insync Contract)

ib_insync field mappings:
  BarData   : .date .open .high .low .close .volume .average .barCount
  Ticker    : .contract .bid .ask .last .bidSize .askSize .lastSize .volume ...
  Contract  : .symbol .secType .exchange .primaryExch .currency .conId

Paper TWS port  : 7497
Live  TWS port  : 7496
Paper IB Gateway: 4002
Live  IB Gateway: 4001
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from ib_insync import IB, BarData as IBBarData, Contract, Stock, Future, Forex, Ticker

from nexus.bus.publisher import publisher
from nexus.core.config import TradingMode, get_settings
from nexus.core.schemas import (
    BarData,
    ComponentType,
    ContractInfo,
    NewsEvent,
    SecType,
    TelemetryEvent,
    TelemetryLevel,
    TickData,
)

logger = logging.getLogger(__name__)

# IBKR historical data rate limit: max 60 requests per 10 minutes
_HIST_REQUEST_INTERVAL = timedelta(seconds=10)
_HIST_MAX_PER_INTERVAL = 6   # conservative: 6/10s = 36/min < 60/min limit


def _make_contract(info: ContractInfo) -> Contract:
    """Convert our ContractInfo to an ib_insync Contract."""
    if info.sec_type == SecType.STK:
        c = Stock(info.symbol, info.exchange, info.currency)
    elif info.sec_type == SecType.FUT:
        c = Future(info.symbol, exchange=info.exchange, currency=info.currency)
    elif info.sec_type == SecType.CASH:
        c = Forex(info.symbol)
    else:
        c = Contract(
            symbol=info.symbol,
            secType=info.sec_type.value,
            exchange=info.exchange,
            currency=info.currency,
        )
    if info.conid:
        c.conId = info.conid
    if info.primary_exchange:
        c.primaryExch = info.primary_exchange
    return c


def _ib_bar_to_schema(bar: IBBarData, symbol: str, frequency: str) -> BarData:
    """Convert ib_insync BarData to our BarData schema."""
    ts = bar.date if isinstance(bar.date, datetime) else datetime.fromisoformat(str(bar.date))
    return BarData(
        timestamp=ts,
        symbol=symbol,
        frequency=frequency,
        open=bar.open,
        high=bar.high,
        low=bar.low,
        close=bar.close,
        volume=bar.volume,
        average=bar.average,
        bar_count=bar.barCount,
        source="IBKR",
    )


class IBKRConnector:
    """
    Manages the ib_insync connection lifecycle and exposes typed streaming methods.

    Architecture notes:
      - Runs in a dedicated asyncio task so it never blocks the main event loop.
      - Emits telemetry on connect / disconnect / error to Redis bus.
      - Historical data requests are queued and throttled to stay under IBKR limits.
    """

    COMPONENT_ID = "ibkr_connector"

    def __init__(self) -> None:
        self._ib = IB()
        self._settings = get_settings()
        self._connected = False
        self._watchdog_task: asyncio.Task[None] | None = None
        self._hist_semaphore = asyncio.Semaphore(_HIST_MAX_PER_INTERVAL)
        self._subscriptions: dict[str, Contract] = {}   # symbol → Contract

        # Register ib_insync event handlers
        self._ib.disconnectedEvent += self._on_disconnected
        self._ib.errorEvent += self._on_error

    # ─── Connection ──────────────────────────────────────────────────────────

    async def connect(self) -> None:
        """Connect to TWS/Gateway and start the reconnect watchdog."""
        await self._connect_once()
        self._watchdog_task = asyncio.create_task(self._watchdog(), name="ibkr-watchdog")

    async def _connect_once(self) -> None:
        try:
            await self._ib.connectAsync(
                host=self._settings.ibkr_host,
                port=self._settings.ibkr_port,
                clientId=self._settings.ibkr_client_id,
                timeout=15,
            )
            self._connected = True
            logger.info(
                "Connected to IBKR at %s:%s (clientId=%s)",
                self._settings.ibkr_host,
                self._settings.ibkr_port,
                self._settings.ibkr_client_id,
            )
            await self._emit_telemetry(
                TelemetryLevel.INFO,
                {"event": "connected", "host": self._settings.ibkr_host, "port": self._settings.ibkr_port},
            )
        except Exception as exc:
            self._connected = False
            logger.error("IBKR connection failed: %s", exc)
            await self._emit_telemetry(TelemetryLevel.ERROR, {"event": "connect_failed", "error": str(exc)})

    async def _watchdog(self) -> None:
        """Reconnect automatically if the connection drops."""
        while True:
            await asyncio.sleep(30)
            if not self._ib.isConnected():
                logger.warning("IBKR disconnected — attempting reconnect...")
                await self._emit_telemetry(TelemetryLevel.WARN, {"event": "reconnecting"})
                await self._connect_once()

    async def disconnect(self) -> None:
        if self._watchdog_task:
            self._watchdog_task.cancel()
        self._ib.disconnect()
        self._connected = False
        logger.info("Disconnected from IBKR")

    # ─── Event Handlers ───────────────────────────────────────────────────────

    def _on_disconnected(self) -> None:
        self._connected = False
        logger.warning("IBKR disconnected event received")

    def _on_error(self, req_id: int, error_code: int, error_string: str, contract: Any) -> None:
        # IBKR error codes 2100-2110 are informational (market data farm notifications)
        level = TelemetryLevel.INFO if 2100 <= error_code <= 2110 else TelemetryLevel.ERROR
        logger.log(
            logging.INFO if level == TelemetryLevel.INFO else logging.ERROR,
            "IBKR error [%s] req=%s: %s", error_code, req_id, error_string,
        )
        asyncio.create_task(
            self._emit_telemetry(
                level,
                {"event": "ib_error", "code": error_code, "msg": error_string, "req_id": req_id},
            )
        )

    # ─── Contract Resolution ──────────────────────────────────────────────────

    async def resolve_contract(self, info: ContractInfo) -> Contract:
        """
        Qualify a contract with IBKR (fills in conId, primaryExch, etc.).
        Always call this before subscribing to market data.
        """
        contract = _make_contract(info)
        qualified = await self._ib.qualifyContractsAsync(contract)
        if not qualified:
            raise ValueError(f"Could not qualify contract: {info.symbol} ({info.sec_type})")
        resolved = qualified[0]
        logger.debug("Resolved %s → conId=%s", info.symbol, resolved.conId)
        return resolved

    # ─── Live Market Data ─────────────────────────────────────────────────────

    async def subscribe_ticks(
        self,
        info: ContractInfo,
        on_tick: Callable[[TickData], None] | None = None,
    ) -> Ticker:
        """
        Subscribe to live tick data for a contract.
        IBKR paper account limit: 100 simultaneous subscriptions.

        on_tick is called synchronously on each Ticker update;
        data is also published to the event bus automatically.
        """
        contract = await self.resolve_contract(info)
        self._subscriptions[info.symbol] = contract

        ticker: Ticker = self._ib.reqMktData(contract, genericTickList="", snapshot=False, regulatorySnapshot=False)

        def _handle_tick(t: Ticker) -> None:
            tick = TickData(
                timestamp=datetime.utcnow(),
                symbol=info.symbol,
                bid=t.bid if t.bid != -1 else None,
                ask=t.ask if t.ask != -1 else None,
                last=t.last if t.last != -1 else None,
                bid_size=t.bidSize,
                ask_size=t.askSize,
                last_size=t.lastSize,
                volume=t.volume if t.volume else None,
                open=t.open if t.open != -1 else None,
                high=t.high if t.high != -1 else None,
                low=t.low if t.low != -1 else None,
                close=t.close if t.close != -1 else None,
                halted=bool(t.halted),
            )
            asyncio.create_task(
                publisher.publish("market.tick", tick.model_dump(mode="json"))
            )
            if on_tick:
                on_tick(tick)

        self._ib.pendingTickersEvent += lambda tickers: [_handle_tick(t) for t in tickers if t.contract == contract]
        logger.info("Subscribed to live ticks: %s", info.symbol)
        return ticker

    def unsubscribe_ticks(self, symbol: str) -> None:
        contract = self._subscriptions.pop(symbol, None)
        if contract:
            self._ib.cancelMktData(contract)
            logger.info("Unsubscribed from ticks: %s", symbol)

    # ─── Real-Time Bars (5-second) ─────────────────────────────────────────────

    async def subscribe_realtime_bars(
        self,
        info: ContractInfo,
        on_bar: Callable[[BarData], None] | None = None,
    ) -> None:
        """
        Subscribe to IBKR real-time 5-second bars (minimum granularity).
        Published to channel: market.bar.5s
        """
        contract = await self.resolve_contract(info)

        bars = self._ib.reqRealTimeBars(contract, barSize=5, whatToShow="TRADES", useRTH=False)

        def _handle_bar(bars_list: Any, has_new_bar: bool) -> None:
            if not has_new_bar or not bars_list:
                return
            bar: IBBarData = bars_list[-1]
            schema_bar = _ib_bar_to_schema(bar, info.symbol, "5s")
            asyncio.create_task(
                publisher.publish("market.bar.5s", schema_bar.model_dump(mode="json"))
            )
            if on_bar:
                on_bar(schema_bar)

        bars.updateEvent += _handle_bar
        logger.info("Subscribed to real-time 5s bars: %s", info.symbol)

    # ─── Historical Data ──────────────────────────────────────────────────────

    async def fetch_historical_bars(
        self,
        info: ContractInfo,
        duration: str = "1 Y",
        bar_size: str = "1 day",
        what_to_show: str = "TRADES",
        use_rth: bool = True,
    ) -> list[BarData]:
        """
        Fetch historical OHLCV bars from IBKR.

        Rate-limited by semaphore (_HIST_MAX_PER_INTERVAL concurrent requests).
        Results published to market.bar.<freq> channel and returned as list.

        bar_size examples: '1 min', '5 mins', '1 hour', '1 day'
        duration examples: '1 D', '1 W', '1 M', '1 Y'
        """
        freq_map = {
            "1 min": "1m", "5 mins": "5m", "15 mins": "15m",
            "30 mins": "30m", "1 hour": "1h", "1 day": "1d",
        }
        frequency = freq_map.get(bar_size, bar_size.replace(" ", ""))

        async with self._hist_semaphore:
            contract = await self.resolve_contract(info)
            ib_bars = await self._ib.reqHistoricalDataAsync(
                contract,
                endDateTime="",
                durationStr=duration,
                barSizeSetting=bar_size,
                whatToShow=what_to_show,
                useRTH=use_rth,
                formatDate=1,
            )
            # Throttle: wait before releasing semaphore so next request doesn't fire instantly
            await asyncio.sleep(_HIST_REQUEST_INTERVAL.total_seconds() / _HIST_MAX_PER_INTERVAL)

        bars: list[BarData] = []
        channel = f"market.bar.{frequency}"
        for ib_bar in ib_bars:
            bar = _ib_bar_to_schema(ib_bar, info.symbol, frequency)
            bars.append(bar)
            await publisher.publish(channel, bar.model_dump(mode="json"))

        logger.info("Fetched %d historical bars for %s (%s %s)", len(bars), info.symbol, duration, bar_size)
        return bars

    # ─── Account Info ─────────────────────────────────────────────────────────

    async def get_account_value(self, tag: str = "NetLiquidation") -> float:
        """Fetch a single account value tag (e.g. NetLiquidation, BuyingPower)."""
        values = await self._ib.accountValuesAsync(account=self._settings.ibkr_account)
        for v in values:
            if v.tag == tag and v.currency == "USD":
                return float(v.value)
        return 0.0

    # ─── Telemetry helper ─────────────────────────────────────────────────────

    async def _emit_telemetry(self, level: TelemetryLevel, payload: dict[str, Any]) -> None:
        settings = get_settings()
        event = TelemetryEvent(
            component_id=self.COMPONENT_ID,
            component_type=ComponentType.INGESTION,
            channel="telemetry.ingestion",
            level=level,
            mode=TradingMode(settings.nexus_trading_mode),
            payload=payload,
        )
        try:
            await publisher.publish_telemetry(event)
        except Exception:
            pass  # Don't let telemetry failures break ingestion
