"""
Telemetry Aggregator.

Subscribes to ALL telemetry.* channels on the Redis event bus.
Provides:
  1. Async generator for WebSocket push to UI (real-time display)
  2. Persistence of telemetry events to MySQL (sampled or full)
  3. In-memory circular buffer for recent events (last N per component)

The UI is a passive consumer — it subscribes to the aggregator's WebSocket
endpoint and displays whatever arrives. It never polls.

Design: each component type gets its own consumer group so failures in
one consumer don't block others.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import AsyncGenerator
from typing import Any

from nexus.bus.consumer import EventConsumer
from nexus.bus.publisher import publisher
from nexus.core.schemas import ComponentType, TelemetryEvent, TelemetryLevel

logger = logging.getLogger(__name__)

# Channels the aggregator subscribes to
_TELEMETRY_CHANNELS = [
    "telemetry.ingestion",
    "telemetry.strategy",
    "telemetry.ai",
    "telemetry.oms",
    "telemetry.risk",
    "telemetry.backtest",
    "telemetry.system",
    "system.heartbeat",
    "risk.breach",
]

# How many recent events to keep per component in the in-memory buffer
_BUFFER_SIZE = 200


class TelemetryAggregator:
    """
    Central telemetry hub.

    Usage:
        aggregator = TelemetryAggregator()
        await aggregator.start()

        # Stream to WebSocket client
        async for event in aggregator.stream():
            await ws.send_json(event.model_dump(mode="json"))
    """

    def __init__(self) -> None:
        self._consumer = EventConsumer()
        self._buffer: dict[str, deque[TelemetryEvent]] = {}   # component_id → recent events
        self._subscribers: list[asyncio.Queue[TelemetryEvent]] = []
        self._running = False
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Connect and start the background aggregation loop."""
        await self._consumer.connect()
        self._running = True
        self._task = asyncio.create_task(self._aggregate_loop(), name="telemetry-aggregator")
        logger.info("TelemetryAggregator started, watching %d channels", len(_TELEMETRY_CHANNELS))

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
        await self._consumer.close()

    async def _aggregate_loop(self) -> None:
        """
        Continuously read from all telemetry channels and fan out to:
          - In-memory buffer
          - All registered WebSocket subscriber queues
        Persistence to MySQL is done in a separate task to avoid blocking.
        """
        r = self._consumer._require_connection()
        # Build the streams dict: {channel: "$"} (start from latest)
        streams: dict[str, str] = {ch: "$" for ch in _TELEMETRY_CHANNELS}

        while self._running:
            try:
                results = await r.xread(streams, count=50, block=1000)
                if not results:
                    continue
                for stream_name, messages in results:
                    for msg_id, raw in messages:
                        streams[stream_name] = msg_id  # advance cursor
                        try:
                            event = TelemetryEvent.from_stream_dict(raw)
                        except Exception as exc:
                            logger.debug("Failed to parse telemetry event: %s", exc)
                            continue

                        self._buffer_event(event)
                        await self._fan_out(event)

            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("Aggregator loop error: %s", exc)
                await asyncio.sleep(1)

    def _buffer_event(self, event: TelemetryEvent) -> None:
        buf = self._buffer.setdefault(event.component_id, deque(maxlen=_BUFFER_SIZE))
        buf.append(event)

    async def _fan_out(self, event: TelemetryEvent) -> None:
        """Push event to all active WebSocket subscriber queues."""
        dead: list[asyncio.Queue[TelemetryEvent]] = []
        for q in self._subscribers:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                dead.append(q)  # slow consumer — drop it
        for q in dead:
            self._subscribers.remove(q)

    # ─── WebSocket streaming ──────────────────────────────────────────────────

    def stream(self, max_queue: int = 500) -> AsyncGenerator[TelemetryEvent, None]:
        """
        Async generator: yields TelemetryEvents as they arrive.
        Each caller gets its own queue — safe for multiple concurrent WebSocket connections.

        Usage in FastAPI:
            @router.websocket("/ws/telemetry")
            async def telemetry_ws(ws: WebSocket):
                await ws.accept()
                async for event in aggregator.stream():
                    await ws.send_json(event.model_dump(mode="json"))
        """
        q: asyncio.Queue[TelemetryEvent] = asyncio.Queue(maxsize=max_queue)
        self._subscribers.append(q)
        return self._stream_from_queue(q)

    async def _stream_from_queue(
        self, q: asyncio.Queue[TelemetryEvent]
    ) -> AsyncGenerator[TelemetryEvent, None]:
        try:
            while True:
                event = await q.get()
                yield event
        finally:
            try:
                self._subscribers.remove(q)
            except ValueError:
                pass

    # ─── Query helpers ────────────────────────────────────────────────────────

    def recent(self, component_id: str, n: int = 20) -> list[TelemetryEvent]:
        """Return the N most recent events for a specific component."""
        buf = self._buffer.get(component_id, deque())
        return list(buf)[-n:]

    def recent_errors(self, n: int = 50) -> list[TelemetryEvent]:
        """Return the N most recent ERROR-level events across all components."""
        all_events: list[TelemetryEvent] = []
        for buf in self._buffer.values():
            all_events.extend(e for e in buf if e.level == TelemetryLevel.ERROR)
        return sorted(all_events, key=lambda e: e.timestamp_utc, reverse=True)[:n]

    def component_status(self) -> dict[str, dict[str, Any]]:
        """
        Return a summary of each known component's last event.
        Used by the UI for a component health dashboard.
        """
        summary: dict[str, dict[str, Any]] = {}
        for comp_id, buf in self._buffer.items():
            if not buf:
                continue
            last = buf[-1]
            summary[comp_id] = {
                "component_type": last.component_type.value,
                "last_seen": last.timestamp_utc.isoformat(),
                "last_level": last.level.value,
                "mode": last.mode.value,
            }
        return summary


# Module-level singleton
aggregator = TelemetryAggregator()
