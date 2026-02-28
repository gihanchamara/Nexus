"""
Redis Streams publisher.

Every component calls `EventPublisher.publish()` to emit events onto the bus.
Keeps a single async Redis connection pool shared across the process.

Channel naming convention:
  market.tick          — live tick data
  market.bar.<freq>    — OHLCV bars (market.bar.1m, market.bar.1d, ...)
  market.news          — news events
  strategy.signal      — trading signals
  order.event          — order lifecycle events
  risk.breach          — risk limit breaches
  telemetry.<type>     — component telemetry (telemetry.ingestion, etc.)
  system.heartbeat     — liveness pings
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

import redis.asyncio as aioredis

from nexus.core.config import get_settings
from nexus.core.schemas import TelemetryEvent

logger = logging.getLogger(__name__)

# Redis stream max length — older messages are trimmed automatically
_STREAM_MAXLEN = 10_000


class EventPublisher:
    """
    Async Redis Streams publisher.
    Use as a context manager or call connect()/close() manually.

    Usage:
        async with EventPublisher() as pub:
            await pub.publish("market.bar.1m", {"symbol": "AAPL", ...})
    """

    def __init__(self) -> None:
        self._redis: aioredis.Redis | None = None

    async def connect(self) -> None:
        settings = get_settings()
        self._redis = aioredis.from_url(
            settings.redis_url,
            encoding="utf-8",
            decode_responses=True,
            max_connections=20,
        )
        # Verify connection
        await self._redis.ping()
        logger.info("EventPublisher connected to Redis at %s:%s", settings.redis_host, settings.redis_port)

    async def close(self) -> None:
        if self._redis:
            await self._redis.aclose()
            self._redis = None

    async def __aenter__(self) -> "EventPublisher":
        await self.connect()
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()

    def _require_connection(self) -> aioredis.Redis:
        if self._redis is None:
            raise RuntimeError("EventPublisher not connected — call connect() or use as context manager.")
        return self._redis

    async def publish(
        self,
        channel: str,
        payload: dict[str, Any],
        maxlen: int = _STREAM_MAXLEN,
    ) -> str:
        """
        Publish a raw payload dict to a Redis stream channel.
        All values are coerced to strings (Redis stream requirement).

        Returns the stream message ID (e.g. '1716300000000-0').
        """
        r = self._require_connection()
        flat: dict[str, str] = {}
        for k, v in payload.items():
            if isinstance(v, (dict, list)):
                flat[k] = json.dumps(v)
            elif isinstance(v, datetime):
                flat[k] = v.isoformat()
            else:
                flat[k] = str(v)

        msg_id: str = await r.xadd(channel, flat, maxlen=maxlen, approximate=True)
        return msg_id

    async def publish_telemetry(self, event: TelemetryEvent) -> str:
        """Convenience method: publish a TelemetryEvent to its channel."""
        return await self.publish(event.channel, event.to_stream_dict())

    async def heartbeat(self, component_id: str) -> str:
        """Emit a system heartbeat for the given component."""
        return await self.publish(
            "system.heartbeat",
            {
                "component_id": component_id,
                "timestamp_utc": datetime.utcnow().isoformat(),
            },
        )


# ─── Module-level singleton ───────────────────────────────────────────────────
# Components import this and call await publisher.publish(...)
# It must be connected before use (done in application lifespan).
publisher = EventPublisher()
