"""
Historical Data Loader — Phase 3.

Loads BarData from multiple sources for backtesting:
  - CSV files  (standard OHLCV format)
  - Parquet files (column-efficient; preferred for large datasets)
  - TimescaleDB   (queries market_bars hypertable via asyncpg)

Also provides OptionsChainBuilder which constructs synthetic OptionsChainSnapshot
objects from historical bars using the options_pricer module.

CSV format expected (column names are flexible — see _COLUMN_MAP):
  date/timestamp, open, high, low, close, volume[, average][, bar_count]

TimescaleDB query: uses asyncpg directly (no ORM overhead in hot path).
"""

from __future__ import annotations

import csv
import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from nexus.backtest.options_pricer import HistoricalVolatility, OptionsChainBuilder
from nexus.core.schemas import BarData, OptionsChainSnapshot

logger = logging.getLogger(__name__)

# Accepted column name aliases for CSV loading
_COLUMN_MAP = {
    "timestamp": ["timestamp", "date", "datetime", "time", "Date", "Datetime"],
    "open":      ["open", "Open", "o"],
    "high":      ["high", "High", "h"],
    "low":       ["low", "Low", "l"],
    "close":     ["close", "Close", "c", "adj_close", "Adj Close"],
    "volume":    ["volume", "Volume", "v", "vol"],
    "average":   ["average", "Average", "vwap", "VWAP"],
    "bar_count": ["bar_count", "barcount", "trades"],
}


def _resolve_column(headers: list[str], field: str) -> str | None:
    """Find the actual column name in headers for a logical field name."""
    for alias in _COLUMN_MAP.get(field, [field]):
        if alias in headers:
            return alias
    return None


class DataLoader:
    """
    Static utility for loading historical BarData from various sources.

    All methods return a list[BarData] sorted chronologically.
    """

    # ─── CSV ──────────────────────────────────────────────────────────────────

    @staticmethod
    def from_csv(
        path: str | Path,
        symbol: str,
        frequency: str = "1d",
        source: str = "CSV",
    ) -> list[BarData]:
        """
        Load OHLCV bars from a CSV file.

        Flexible column detection — works with common data provider formats
        (Yahoo Finance, Alpha Vantage, Quandl, IBKR exported data).

        Args:
            path     : path to the CSV file
            symbol   : symbol to tag on each BarData record
            frequency: bar frequency ('1m', '5m', '1h', '1d')
            source   : source tag (default: 'CSV')

        Returns:
            list[BarData] sorted by timestamp ascending
        """
        path = Path(path)
        bars: list[BarData] = []

        with open(path, newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            headers = list(reader.fieldnames or [])

            # Resolve column mappings
            col = {field: _resolve_column(headers, field) for field in _COLUMN_MAP}
            if not col["timestamp"] or not col["close"]:
                raise ValueError(
                    f"CSV {path} missing required columns (timestamp, close). "
                    f"Found: {headers}"
                )

            for row in reader:
                ts_raw = row[col["timestamp"]]
                try:
                    timestamp = _parse_timestamp(ts_raw)
                except Exception:
                    logger.warning("Skipping unparseable timestamp: %r", ts_raw)
                    continue

                bars.append(BarData(
                    timestamp=timestamp,
                    symbol=symbol,
                    frequency=frequency,
                    open=float(row.get(col["open"] or "", row[col["close"]])),
                    high=float(row.get(col["high"] or "", row[col["close"]])),
                    low=float(row.get(col["low"] or "", row[col["close"]])),
                    close=float(row[col["close"]]),
                    volume=int(float(row.get(col["volume"] or "", 0) or 0)),
                    average=float(row.get(col["average"] or "", 0.0) or 0.0),
                    bar_count=int(float(row.get(col["bar_count"] or "", 0) or 0)),
                    source=source,
                ))

        bars.sort(key=lambda b: b.timestamp)
        logger.info("Loaded %d bars for %s from %s", len(bars), symbol, path.name)
        return bars

    # ─── Parquet ──────────────────────────────────────────────────────────────

    @staticmethod
    def from_parquet(
        path: str | Path,
        symbol: str,
        frequency: str = "1d",
        source: str = "PARQUET",
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[BarData]:
        """
        Load OHLCV bars from a Parquet file.

        Requires pyarrow (included in pandas dependency).
        Optionally filter by date range [start, end].

        Column expectations: same flexible mapping as from_csv().
        """
        try:
            import pandas as pd  # type: ignore[import-untyped]
        except ImportError as exc:
            raise ImportError("pandas required for Parquet loading") from exc

        df = pd.read_parquet(path)

        # Normalise column names to lowercase
        df.columns = [c.lower() for c in df.columns]

        # Detect timestamp column
        time_col = next(
            (c for c in df.columns if c in ["timestamp", "date", "datetime", "time"]),
            df.index.name or "",
        )
        if time_col and time_col in df.columns:
            df["_ts"] = pd.to_datetime(df[time_col], utc=False)
        else:
            df["_ts"] = pd.to_datetime(df.index, utc=False)

        if start:
            df = df[df["_ts"] >= pd.Timestamp(start)]
        if end:
            df = df[df["_ts"] <= pd.Timestamp(end)]

        close_col = next(
            (c for c in df.columns if c in ["close", "adj_close", "adj close"]),
            None,
        )
        if close_col is None:
            raise ValueError(f"Parquet {path} has no 'close' column. Columns: {list(df.columns)}")

        bars: list[BarData] = []
        for _, row in df.iterrows():
            bars.append(BarData(
                timestamp=row["_ts"].to_pydatetime().replace(tzinfo=None),
                symbol=symbol,
                frequency=frequency,
                open=float(row.get("open", row[close_col])),
                high=float(row.get("high", row[close_col])),
                low=float(row.get("low", row[close_col])),
                close=float(row[close_col]),
                volume=int(float(row.get("volume", 0) or 0)),
                average=float(row.get("average", row.get("vwap", 0.0)) or 0.0),
                bar_count=int(float(row.get("bar_count", row.get("trades", 0)) or 0)),
                source=source,
            ))

        bars.sort(key=lambda b: b.timestamp)
        logger.info("Loaded %d bars for %s from Parquet", len(bars), symbol)
        return bars

    # ─── TimescaleDB ──────────────────────────────────────────────────────────

    @staticmethod
    async def from_timescaledb(
        symbol: str,
        frequency: str,
        start: datetime,
        end: datetime,
        conn_str: str | None = None,
    ) -> list[BarData]:
        """
        Async load of OHLCV bars from the TimescaleDB market_bars hypertable.

        Queries the hypertable created in db/timescaledb/init.sql.

        Args:
            symbol    : underlying symbol (e.g. 'AAPL')
            frequency : bar size ('1m', '5m', '1h', '1d')
            start/end : inclusive date range
            conn_str  : asyncpg DSN; defaults to NEXUS_TIMESCALE_URL env var

        Returns:
            list[BarData] sorted by time ascending
        """
        try:
            import asyncpg  # type: ignore[import-untyped]
        except ImportError as exc:
            raise ImportError("asyncpg required for TimescaleDB loading") from exc

        if conn_str is None:
            from nexus.core.config import get_settings
            conn_str = get_settings().timescale_url

        query = """
            SELECT time, open, high, low, close, volume, average, bar_count
            FROM market_bars
            WHERE symbol = $1
              AND frequency = $2
              AND time >= $3
              AND time <= $4
            ORDER BY time ASC
        """
        conn = await asyncpg.connect(conn_str)
        try:
            rows = await conn.fetch(query, symbol, frequency, start, end)
        finally:
            await conn.close()

        bars = [
            BarData(
                timestamp=row["time"],
                symbol=symbol,
                frequency=frequency,
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                volume=int(row["volume"] or 0),
                average=float(row["average"] or 0.0),
                bar_count=int(row["bar_count"] or 0),
                source="TIMESCALEDB",
            )
            for row in rows
        ]
        logger.info("Loaded %d bars for %s/%s from TimescaleDB", len(bars), symbol, frequency)
        return bars


# ─── Options Chain Builder ────────────────────────────────────────────────────

class OptionsChainBuilder:
    """
    Builds synthetic OptionsChainSnapshot objects from historical price bars.

    Used to enable options strategy backtesting when historical options data
    is unavailable. The synthetic chains use Black-Scholes with historical
    volatility and a simplified IV skew model.

    Typical usage:
        bars = DataLoader.from_csv("spy_daily.csv", symbol="SPY")
        chains = OptionsChainBuilder.build_from_bars(
            bars=bars,
            symbol="SPY",
            hv_window=20,
            expiry_dte_list=[30, 60],
            strike_count=10,
            strike_spacing=5.0,
        )
        config = BacktestConfig(..., options_chains=chains)
    """

    @staticmethod
    def build_from_bars(
        bars: list[BarData],
        symbol: str,
        hv_window: int = 20,
        expiry_dte_list: list[int] | None = None,
        strike_count: int = 10,
        strike_spacing: float = 5.0,
        risk_free_rate: float = 0.05,
        iv_skew: bool = True,
        spread_pct: float = 0.04,
        chain_frequency: str = "weekly",
    ) -> list[OptionsChainSnapshot]:
        """
        Build synthetic options chains for each bar in the input series.

        Args:
            bars           : historical price bars (must be sorted chronologically)
            symbol         : underlying symbol
            hv_window      : rolling HV window in bars (default: 20-day)
            expiry_dte_list: list of DTE targets (default: [30, 60])
            strike_count   : number of strikes above and below ATM (total = 2×+1)
            strike_spacing : points between strikes (e.g. 5.0 for SPY)
            risk_free_rate : annualised risk-free rate (default: 5%)
            iv_skew        : apply put skew model (realistic IV surface)
            spread_pct     : bid-ask spread as % of mid price
            chain_frequency: 'daily' | 'weekly' — how often to emit a chain
                             (weekly = fewer chains, faster backtest)

        Returns:
            list[OptionsChainSnapshot] aligned with bar timestamps
        """
        if expiry_dte_list is None:
            expiry_dte_list = [30, 60]

        closes = [b.close for b in bars]
        hv_series = HistoricalVolatility.rolling(closes, window=hv_window)

        # Compute 52-week HV series for IVR
        hv_window_52w = min(252, len(hv_series))
        iv_rank_series = HistoricalVolatility.iv_rank(hv_series, window=hv_window_52w)

        chains: list[OptionsChainSnapshot] = []
        last_chain_week: int | None = None

        for i, bar in enumerate(bars):
            if i < hv_window:
                continue  # not enough history for HV

            # Throttle chain generation
            bar_date = bar.timestamp.date() if isinstance(bar.timestamp, datetime) else bar.timestamp
            bar_week = bar_date.isocalendar()[1]

            if chain_frequency == "weekly":
                if bar_week == last_chain_week:
                    continue
                last_chain_week = bar_week

            spot = bar.close
            hist_vol = hv_series[i]
            ivr = iv_rank_series[i]

            # Build expiry dates from bar timestamp
            expiry_dates = [
                _business_day_offset(bar_date, dte)
                for dte in expiry_dte_list
            ]

            # Build strike grid centred on spot
            atm_strike = _round_to_spacing(spot, strike_spacing)
            strikes = [
                atm_strike + (j - strike_count) * strike_spacing
                for j in range(2 * strike_count + 1)
                if atm_strike + (j - strike_count) * strike_spacing > 0
            ]

            chain = OptionsChainBuilder._build_single_chain(
                symbol=symbol,
                timestamp=bar.timestamp,
                spot=spot,
                strikes=strikes,
                expiry_dates=expiry_dates,
                hist_vol=hist_vol,
                iv_rank=ivr,
                risk_free_rate=risk_free_rate,
                iv_skew=iv_skew,
                spread_pct=spread_pct,
            )
            chains.append(chain)

        logger.info(
            "Built %d synthetic option chains for %s (HV window=%d, expiries=%s)",
            len(chains), symbol, hv_window, expiry_dte_list,
        )
        return chains

    @staticmethod
    def _build_single_chain(
        symbol: str,
        timestamp: datetime,
        spot: float,
        strikes: list[float],
        expiry_dates: list[date],
        hist_vol: float,
        iv_rank: float,
        risk_free_rate: float,
        iv_skew: bool,
        spread_pct: float,
    ) -> OptionsChainSnapshot:
        """Delegate to options_pricer for a single chain snapshot."""
        from nexus.backtest.options_pricer import PricerChainBuilder
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
            spread_pct=spread_pct,
        )


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _parse_timestamp(raw: str) -> datetime:
    """Parse a timestamp string in common formats."""
    for fmt in [
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d",
        "%m/%d/%Y",
        "%m/%d/%Y %H:%M:%S",
        "%Y%m%d",
    ]:
        try:
            return datetime.strptime(raw.strip(), fmt)
        except ValueError:
            continue
    # Fallback to pandas if available
    try:
        import pandas as pd  # type: ignore[import-untyped]
        return pd.Timestamp(raw).to_pydatetime().replace(tzinfo=None)
    except Exception:
        raise ValueError(f"Cannot parse timestamp: {raw!r}")


def _round_to_spacing(price: float, spacing: float) -> float:
    """Round a price to the nearest strike spacing increment."""
    return round(round(price / spacing) * spacing, 2)


def _business_day_offset(start: date, calendar_days: int) -> date:
    """Approximate business-day expiry: adds calendar_days and lands on a Friday."""
    target = start + timedelta(days=calendar_days)
    # Roll back to Friday if weekend
    while target.weekday() > 4:
        target -= timedelta(days=1)
    return target
