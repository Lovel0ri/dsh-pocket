"""Process-wide runtime container shared by REST, WS and the CLI."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .acp_backend import AcpBackend
from .auth import SlidingWindowLimiter
from .backend import Backend
from .config import Settings
from .hub import EventHub
from .mock import MockBackend


@dataclass
class Runtime:
    settings: Settings
    backend: Backend
    hub: EventHub
    api_limiter: SlidingWindowLimiter
    started_at: float = field(default_factory=time.monotonic)


def build_runtime(settings: Settings) -> Runtime:
    hub = EventHub()
    backend: Backend = MockBackend(settings, hub) if settings.mock else AcpBackend(settings, hub)
    return Runtime(
        settings=settings,
        backend=backend,
        hub=hub,
        api_limiter=SlidingWindowLimiter(settings.api_rate_max, settings.api_rate_window_seconds),
    )
