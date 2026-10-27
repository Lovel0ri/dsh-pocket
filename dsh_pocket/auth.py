"""No-authentication mode helpers.

Captain decision for T7: this is a personal LAN tool, so there is no login.
The server binds to loopback by default and prints a prominent warning when the
operator chooses a LAN bind. Only a lightweight flood limiter remains here.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from dataclasses import dataclass, field

from .errors import ApiError


def client_ip(client: object | None) -> str:
    """Normalise an ASGI client host (handles IPv4-mapped IPv6)."""
    host = getattr(client, "host", None) or "unknown"
    if host.startswith("::ffff:"):
        host = host[len("::ffff:"):]
    return host


@dataclass
class SlidingWindowLimiter:
    """Small in-memory per-key limiter (not authentication)."""

    max_events: int
    window_seconds: float
    _events: dict[str, deque[float]] = field(default_factory=lambda: defaultdict(deque), init=False, repr=False)

    def hit(self, key: str, *, message: str = "too many requests") -> None:
        if self.max_events <= 0:
            return
        now = time.monotonic()
        events = self._events[key]
        while events and now - events[0] > self.window_seconds:
            events.popleft()
        if len(events) >= self.max_events:
            retry_after = max(1, int(self.window_seconds - (now - events[0])) + 1)
            raise ApiError(
                "E_RATE_LIMITED",
                message,
                http_status=429,
                headers={"Retry-After": str(retry_after)},
            )
        events.append(now)
