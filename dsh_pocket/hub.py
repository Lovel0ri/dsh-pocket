"""In-process event fan-out with per-session monotonic sequence numbers.

Contract §4.5: every hub-published event carries ``seq`` (per session, starting
at 1). It is additive relative to ``api-contract.md`` and is **not** a history
cursor.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Any


class EventHub:
    def __init__(self, max_queue_size: int = 1000) -> None:
        self._subscribers: dict[str, set[asyncio.Queue[dict[str, Any]]]] = defaultdict(set)
        self._seq: dict[str, int] = defaultdict(int)
        self._max_queue_size = max_queue_size

    def subscribe(self, session_id: str, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._subscribers[session_id].add(queue)

    def unsubscribe(self, session_id: str, queue: asyncio.Queue[dict[str, Any]]) -> None:
        subscribers = self._subscribers.get(session_id)
        if not subscribers:
            return
        subscribers.discard(queue)
        if not subscribers:
            self._subscribers.pop(session_id, None)

    def subscriber_count(self, session_id: str) -> int:
        return len(self._subscribers.get(session_id, ()))

    def publish(self, session_id: str, event: dict[str, Any]) -> dict[str, Any]:
        """Attach ``seq`` (and a default ``sessionId``) then fan out."""
        self._seq[session_id] += 1
        payload = dict(event)
        payload.setdefault("sessionId", session_id)
        payload["seq"] = self._seq[session_id]
        for queue in tuple(self._subscribers.get(session_id, ())):
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:
                # Slow client: drop the oldest pending event, keep the newest.
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:  # pragma: no cover - race only
                    pass
                try:
                    queue.put_nowait(payload)
                except asyncio.QueueFull:  # pragma: no cover - defensive
                    pass
        return payload

    def last_seq(self, session_id: str) -> int:
        return self._seq.get(session_id, 0)
