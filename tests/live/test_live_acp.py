"""Real ACP smoke test (opt-in, network + isolated .dev-home).

Run with:  DSH_POCKET_LIVE=1 python3 -m pytest tests/live -q
Requires the T1 isolated home at ``.dev-home`` with refreshed credentials.
Nothing here writes ``~/.dsh``.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import httpx
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "tests"))
from conftest import LiveServer  # noqa: E402

from dsh_pocket.config import Settings  # noqa: E402

DSH_HOME = PROJECT_ROOT / ".dev-home"
DSH_BIN = Path("/Applications/DeepSeek Harness.app/Contents/Resources/runtime/cli/bin/dsh")

pytestmark = pytest.mark.skipif(
    os.environ.get("DSH_POCKET_LIVE") != "1",
    reason="real ACP smoke test; set DSH_POCKET_LIVE=1 with an isolated .dev-home",
)


def _live_settings(tmp_path: Path) -> Settings:
    if not DSH_HOME.is_dir():
        pytest.skip(".dev-home is missing (run the T1 setup script first)")
    if not DSH_BIN.exists():
        pytest.skip("dsh CLI not found")
    return Settings(
        mock=False,
        dsh_home=DSH_HOME,
        state_dir=tmp_path / "state",
        db_path=tmp_path / "state" / "pocket.db",
        dsh_command=(str(DSH_BIN),),
        acp_initialize_timeout_seconds=90,
        acp_request_timeout_seconds=120,
        prompt_timeout_seconds=240,
        permission_timeout_seconds=30,
        cancel_grace_seconds=15,
        ws_idle_timeout_seconds=120,
        log_level="WARNING",
    )


def _prompt_via_ws(server, session_id: str, text: str, tag: str = "1") -> list[dict]:
    connection = server.connect_ws()
    events: list[dict] = []
    try:
        hello = json.loads(connection.recv(timeout=10))
        assert hello["type"] == "hello"
        connection.send(json.dumps({"type": "subscribe", "sessionId": session_id, "limit": 30}))
        assert json.loads(connection.recv(timeout=10))["type"] == "history.snapshot"
        assert json.loads(connection.recv(timeout=10))["type"] == "session.state"
        connection.send(json.dumps({
            "type": "prompt", "requestId": f"turn_live_{tag}", "sessionId": session_id,
            "clientMessageId": f"cm-live-{tag}", "text": text,
        }))
        deadline = time.time() + 240
        while time.time() < deadline:
            event = json.loads(connection.recv(timeout=240))
            events.append(event)
            if event["type"] == "permission.request":
                connection.send(json.dumps({
                    "type": "permission.respond", "requestId": event["requestId"],
                    "decision": "allow-once",
                }))
            if event["type"] in ("turn.end", "turn.error"):
                break
        return events
    finally:
        connection.close()


def test_live_phone_session_chat_restart_history(tmp_path):
    settings = _live_settings(tmp_path)
    server = LiveServer(settings, tmp_path)
    server.start()
    session_id = None
    try:
        with httpx.Client(base_url=server.base_url, timeout=30) as client:
            health = client.get("/api/v1/health").json()
            assert health["mode"] == "acp"
            assert health["auth"] == "none"
            workspaces = client.get("/api/v1/workspaces").json()["workspaces"]
            assert workspaces
            created = client.post("/api/v1/sessions", json={"cwd": str(PROJECT_ROOT)})
            assert created.status_code == 201, created.text
            session_id = created.json()["session"]["summary"]["sessionId"]

        events = _prompt_via_ws(server, session_id, "只回复 OK")
        assert events[-1]["type"] == "turn.end", events[-1]
        assert events[-1]["stopReason"] == "end_turn"
        assert any(event["type"] == "assistant.delta" for event in events)

        with httpx.Client(base_url=server.base_url, timeout=30) as client:
            page = client.get(f"/api/v1/sessions/{session_id}/transcript").json()
            assert page["items"][0]["kind"] == "user"
            assert any(item["kind"] == "assistant" for item in page["items"])
            first_item_count = len(page["items"])
    finally:
        server.stop()

    # Restart the pocket server against the same SQLite DB and DSH home.
    server = LiveServer(settings, tmp_path)
    server.start()
    try:
        with httpx.Client(base_url=server.base_url, timeout=30) as client:
            page = client.get(f"/api/v1/sessions/{session_id}/transcript").json()
            assert len(page["items"]) == first_item_count
            assert page["session"]["sessionId"] == session_id

            events = _prompt_via_ws(server, session_id, "再回复一次 OK", tag="2")
            assert events[-1]["type"] == "turn.end", events[-1]
            assert events[-1]["requestId"] == "turn_live_2"

            page = client.get(f"/api/v1/sessions/{session_id}/transcript").json()
            users = [item for item in page["items"] if item["kind"] == "user"]
            assistants = [item for item in page["items"] if item["kind"] == "assistant"]
            assert len(users) == 2
            assert len(assistants) >= 2
            assert users[-1]["text"] == "再回复一次 OK"
    finally:
        server.stop()
