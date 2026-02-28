"""
Redis Streams consumer.

Supports both:
  1. Simple XREAD (no consumer group) — for monitoring / UI feeds
  2. Consumer group XREADGROUP — for reliable processing with acknowledgement
     (ensures each message is processed by exactly one consumer in a group)

Usage — simple read (e.g. telemetry display):
    async for msg_id, data in consumer.read_stream("telemetry.strategy"):
        event = TelemetryEvent.from_stream_dict(data)

Usage — consumer group (e.g. OMS consuming signals):
    await consumer.ensure_group("strategy.signal", "oms-group")
    async for msg_id, data in consumer.read_group("strategy.signal", "oms-group", "oms-worker-1"):
        signal = Signal(**json.loads(data["payload"]))
        await consumer.ack("strategy.signal", "oms-group", msg_id)
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator
from typing import Any

import redis.asyncio as aioredis
from redis.exceptions import ResponseError

from nexus.core.config import get_settings

logger = logging.getLogger(__name__)

# How long to block on XREAD/XREADGROUP before yielding control (ms)
_BLOCK_MS = 1_000
_BATCH_SIZE = 100


class EventConsumer:
    """
    Async Redis Streams consumer.
    Use as a context manager or call connect()/close() manually.
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
        await self._redis.ping()
        logger.info("EventConsumer connected to Redis")

    async def close(self) -> None:
        if self._redis:
            await self._redis.aclose()
            self._redis = None

    async def __aenter__(self) -> "EventConsumer":
        await self.connect()
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()

    def _require_connection(self) -> aioredis.Redis:
        if self._redis is None:
            raise RuntimeError("EventConsumer not connected.")
        return self._redis

    # ─── Simple Read (no group) ───────────────────────────────────────────────

    async def read_stream(
        self,
        channel: str,
        last_id: str = "$",
        batch: int = _BATCH_SIZE,
    ) -> AsyncGenerator[tuple[str, dict[str, str]], None]:
        """
        Continuously yield (msg_id, data) from a stream channel.
        Starts from the latest message by default (last_id='$').
        Use '0' to replay from the beginning.

        This is a non-acking read — suitable for monitoring/UI.
        """
        r = self._require_connection()
        while True:
            results: list[Any] = await r.xread(
                {channel: last_id},
                count=batch,
                block=_BLOCK_MS,
            )
            if not results:
                await asyncio.sleep(0)
                continue
            for _stream, messages in results:
                for msg_id, data in messages:
                    last_id = msg_id
                    yield msg_id, data

    # ─── Consumer Group Read (reliable, exactly-once per group) ───────────────

    async def ensure_group(self, channel: str, group: str) -> None:
        """Create consumer group if it doesn't already exist."""
        r = self._require_connection()
        try:
            await r.xgroup_create(channel, group, id="$", mkstream=True)
            logger.info("Created consumer group '%s' on channel '%s'", group, channel)
        except ResponseError as e:
            if "BUSYGROUP" in str(e):
                pass  # group already exists — fine
            else:
                raise

    async def read_group(
        self,
        channel: str,
        group: str,
        consumer_name: str,
        batch: int = _BATCH_SIZE,
    ) -> AsyncGenerator[tuple[str, dict[str, str]], None]:
        """
        Reliably consume from a consumer group.
        Messages must be acknowledged with ack() after processing.
        Unacknowledged messages are re-delivered on the next read.
        """
        r = self._require_connection()
        while True:
            results: list[Any] = await r.xreadgroup(
                groupname=group,
                consumername=consumer_name,
                streams={channel: ">"},
                count=batch,
                block=_BLOCK_MS,
            )
            if not results:
                await asyncio.sleep(0)
                continue
            for _stream, messages in results:
                for msg_id, data in messages:
                    yield msg_id, data

    async def ack(self, channel: str, group: str, *msg_ids: str) -> None:
        """Acknowledge processed messages so they are removed from the PEL."""
        r = self._require_connection()
        await r.xack(channel, group, *msg_ids)

    async def pending_count(self, channel: str, group: str) -> int:
        """Return number of unacknowledged messages in the group."""
        r = self._require_connection()
        info = await r.xpending(channel, group)
        return int(info.get("pending", 0)) if info else 0
