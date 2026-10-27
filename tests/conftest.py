"""Shared test harness: in-process uvicorn + httpx + websockets (no auth in T7)."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn
from websockets.sync.client import connect as ws_connect

from dsh_pocket.app import create_app
from dsh_pocket.config import Settings

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class LiveServer:
    def __init__(self, settings: Settings, root: Path) -> None:
        self.settings = settings
        self.root = root
        self.app = create_app(settings)
        config = uvicorn.Config(
            self.app,
            host="127.0.0.1",
            port=0,
            log_config=None,
            lifespan="on",
            access_log=False,
            server_header=False,
        )
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True, name="dsh-pocket-test-server")
        self.port = 0

    def start(self) -> "LiveServer":
        self.thread.start()
        deadline = time.time() + 15
        while time.time() < deadline and not self.port:
            if getattr(self.server, "started", False) and self.server.servers:
                for srv in self.server.servers:
                    for sock in srv.sockets:
                        self.port = sock.getsockname()[1]
            time.sleep(0.02)
        if not self.port:
            raise RuntimeError("test server did not bind a port")
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                if httpx.get(f"{self.base_url}/api/v1/health", timeout=0.5).status_code == 200:
                    return self
            except httpx.HTTPError:
                pass
            time.sleep(0.02)
        raise RuntimeError("test server health endpoint not reachable")

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def ws_url(self) -> str:
        return f"ws://127.0.0.1:{self.port}/api/v1/ws"

    def client(self) -> httpx.Client:
        return httpx.Client(base_url=self.base_url, timeout=20)

    def connect_ws(self, **kwargs):
        return ws_connect(
            self.ws_url,
            open_timeout=10,
            ping_interval=None,
            proxy=None,
            **kwargs,
        )


def _make_browseable_tree(root: Path) -> dict[str, Path]:
    workspaces = root / "workspaces"
    alpha = workspaces / "alpha"
    beta = workspaces / "beta"
    gone = workspaces / "gone"
    for path in (alpha, beta, gone):
        path.mkdir(parents=True)
    (alpha / ".hidden-dir").mkdir()
    (alpha / "notes.txt").write_text("not a directory\n", encoding="utf-8")
    (root / ".hidden-root-dir").mkdir()
    return {"alpha": alpha, "beta": beta, "gone": gone}


@pytest.fixture
def server_factory(tmp_path):
    servers: list[LiveServer] = []

    def _make(**overrides) -> LiveServer:
        index = len(servers)
        root = tmp_path / f"srv{index}"
        root.mkdir()
        tree = _make_browseable_tree(root)
        defaults = dict(
            mock=True,
            state_dir=root / "state",
            db_path=root / "state" / "pocket.db",
            mock_workspaces=(str(tree["alpha"]), str(tree["beta"]), str(tree["gone"])),
            browse_roots=(str(root),),
            static_dir=root / "static",
            api_rate_max=10_000,
            permission_timeout_seconds=5.0,
            ws_idle_timeout_seconds=30.0,
            log_level="WARNING",
        )
        settings = Settings(**{**defaults, **overrides})
        server = LiveServer(settings, root)
        server.tree = tree  # type: ignore[attr-defined]
        server.start()
        servers.append(server)
        return server

    yield _make
    for server in servers:
        server.stop()


@pytest.fixture
def server(server_factory) -> LiveServer:
    return server_factory()


def ws_send(connection, payload: dict) -> None:
    connection.send(json.dumps(payload))


def ws_recv(connection, timeout: float = 10) -> dict:
    return json.loads(connection.recv(timeout=timeout))


def ws_collect(connection, *, stop=("turn.end",), timeout: float = 15, on_event=None) -> list[dict]:
    events: list[dict] = []
    deadline = time.time() + timeout
    while time.time() < deadline:
        event = ws_recv(connection, timeout=max(0.1, deadline - time.time()))
        events.append(event)
        if on_event is not None:
            on_event(event)
        if event.get("type") in stop:
            break
    else:  # pragma: no cover
        raise AssertionError(f"did not see any of {stop}; got {[e.get('type') for e in events]}")
    return events


def wait_for_state(client: httpx.Client, session_id: str, state: str, *, timeout: float = 10) -> dict:
    deadline = time.time() + timeout
    last: dict = {}
    while time.time() < deadline:
        response = client.get(f"/api/v1/sessions/{session_id}")
        assert response.status_code == 200, response.text
        last = response.json()
        if last["state"] == state:
            return last
        time.sleep(0.02)
    raise AssertionError(f"session {session_id} never reached state {state!r}; last={last}")
