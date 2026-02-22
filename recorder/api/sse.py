"""Server-Sent Events event bus (Phase 8)."""

from __future__ import annotations

import json
import logging
import queue
import threading
from collections.abc import Generator

logger = logging.getLogger(__name__)

MAX_SUBSCRIBERS = 10


class EventBus:
    """Fan-out event bus: publish() delivers to all active subscribers."""

    def __init__(self) -> None:
        self._subscribers: list[queue.Queue] = []
        self._lock = threading.Lock()

    def subscribe(self) -> queue.Queue:
        """Return a per-connection queue. Caller owns the queue."""
        q: queue.Queue = queue.Queue(maxsize=100)
        with self._lock:
            if len(self._subscribers) >= MAX_SUBSCRIBERS:
                # Drop oldest subscriber
                old = self._subscribers.pop(0)
                try:
                    old.put_nowait(None)  # sentinel to signal disconnect
                except queue.Full:
                    pass
                logger.warning("sse.subscriber_evicted")
            self._subscribers.append(q)
            logger.debug("sse.subscribed", extra={"total": len(self._subscribers)})
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            try:
                self._subscribers.remove(q)
            except ValueError:
                pass
            logger.debug("sse.unsubscribed", extra={"total": len(self._subscribers)})

    def publish(self, event_type: str, data: dict | str) -> None:
        payload = data if isinstance(data, str) else json.dumps(data)
        message = f"event: {event_type}\ndata: {payload}\n\n"
        with self._lock:
            dead = []
            for q in self._subscribers:
                try:
                    q.put_nowait(message)
                except queue.Full:
                    dead.append(q)
            for q in dead:
                self._subscribers.remove(q)
                logger.debug("sse.dropped_slow_subscriber")


event_bus = EventBus()


def sse_stream(subscriber_q: queue.Queue) -> Generator[str, None, None]:
    """
    Flask generator: yield SSE-formatted strings until the client disconnects
    or a sentinel None is received.
    """
    yield "data: {}\n\n"  # initial keepalive
    try:
        while True:
            try:
                msg = subscriber_q.get(timeout=30)
                if msg is None:  # sentinel
                    break
                yield msg
            except queue.Empty:
                yield ": keepalive\n\n"  # SSE comment = keepalive
    finally:
        event_bus.unsubscribe(subscriber_q)
