"""Real backend tests using a fake ACP child (offline, deterministic)."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from conftest import LiveServer, ws_collect, ws_recv, ws_send
from dsh_pocket.acp_backend import AcpBackend, synthetic_workspace_id
from dsh_pocket.config import Settings
from dsh_pocket.errors import ApiError
from dsh_pocket.hub import EventHub

FAKE_ACP = Path(__file__).with_name("fake_acp.py")
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def write_registry(home: Path, workspaces: list[tuple[str, Path, str]]) -> None:
    (home / "storages").mkdir(parents=True, exist_ok=True)
    tables = {}
    for workspace_id, path, title in workspaces:
        tables[workspace_id] = {
            "path": str(path),
            "title": title,
            "sessionIds": [],
            "createdAt": "2026-10-26T00:00:00.000Z",
            "updatedAt": "2026-10-26T00:00:00.000Z",
        }
    registry = {
        "unit": {"name": "workspace", "version": 2},
        "global": {"initialized": True, "workspaceIds": list(tables),
                   "archivedSessionIds": [], "pinnedSessionIds": []},
        "tables": {"workspaces": tables},
    }
    (home / "storages" / "workspace.json").write_text(json.dumps(registry), encoding="utf-8")


@pytest.fixture
def env(tmp_path, monkeypatch):
    for key in ("FAKE_RESUME_ERROR", "FAKE_PROMPT_ERROR", "FAKE_NO_PERMISSION"):
        monkeypatch.delenv(key, raising=False)
    home = tmp_path / "dsh-home"
    ws_one = tmp_path / "ws-one"
    ws_two = tmp_path / "ws-two"
    ws_three = tmp_path / "ws-three"
    for path in (ws_one, ws_two, ws_three):
        path.mkdir()
    wsid_one, wsid_two = str(uuid.uuid4()), str(uuid.uuid4())
    write_registry(home, [(wsid_one, ws_one, "ws-one"), (wsid_two, ws_two, "ws-two")])
    settings = Settings(
        mock=False,
        dsh_home=home,
        db_path=tmp_path / "state" / "pocket.db",
        state_dir=tmp_path / "state",
        dsh_command=(sys.executable, str(FAKE_ACP)),
        acp_initialize_timeout_seconds=20,
        acp_request_timeout_seconds=20,
        prompt_timeout_seconds=30,
        cancel_grace_seconds=5,
        permission_timeout_seconds=10,
        ws_idle_timeout_seconds=60,
        log_level="WARNING",
    )
    return SimpleNamespace(
        settings=settings, home=home, ws_one=ws_one, ws_two=ws_two, ws_three=ws_three,
        wsid_one=wsid_one, wsid_two=wsid_two, tmp=tmp_path,
    )


def run(coro):
    return asyncio.run(coro)


async def prompt_turn(backend: AcpBackend, session_id: str, text: str, decision: str = "allow-once"):
    queue: asyncio.Queue = asyncio.Queue()
    backend.hub.subscribe(session_id, queue)
    try:
        accepted = await backend.start_turn(session_id, text, None, None)
        events: list[dict] = []
        while True:
            event = await asyncio.wait_for(queue.get(), timeout=20)
            events.append(event)
            if event["type"] == "permission.request":
                await backend.respond_permission(event["requestId"], decision)
            if event["type"] in ("turn.end", "turn.error"):
                break
        return accepted, events
    finally:
        backend.hub.unsubscribe(session_id, queue)


def test_create_session_prompt_roundtrip(env):
    async def scenario():
        backend = AcpBackend(env.settings, EventHub())
        try:
            detail = await backend.create_session(env.wsid_one)
            session_id = detail["summary"]["sessionId"]
            assert detail["state"] == "idle"
            assert detail["configOptions"]
            assert detail["summary"]["cwd"] == str(env.ws_one.resolve())
            assert backend.acp_pid is not None
            assert Path(backend.acp_cwd or "") == env.ws_one.resolve()

            accepted, events = await prompt_turn(backend, session_id, "hello fake")
            types = [event["type"] for event in events]
            assert events[0]["type"] == "session.state" and events[0]["state"] == "streaming"
            assert "assistant.thought_delta" in types
            assert "tool.started" in types
            assert "permission.request" in types
            assert "permission.resolved" in types
            assert "tool.finished" in types
            assert "assistant.delta" in types
            assert "usage" in types
            assert types[-1] == "turn.end"
            assert events[-1]["stopReason"] == "end_turn"
            assert events[-1]["requestId"] == accepted["requestId"]
            permission = next(event for event in events if event["type"] == "permission.request")
            assert permission["toolName"] == "bash"
            assert "echo fake-tool" in permission["argumentsPreview"]
            assert [option["optionId"] for option in permission["options"]] == ["allow-once", "reject-once"]

            page = await backend.get_transcript(session_id, None, 30, True, 4000)
            items = page["items"]
            assert items[0]["kind"] == "user" and items[0]["text"] == "hello fake"
            assistant = next(item for item in items if item["kind"] == "assistant")
            assert "fake reply" in assistant["text"]
            assert "thinking" in assistant["reasoning"]
            tool = next(item for item in items if item["kind"] == "tool")
            assert tool["status"] == "completed"
            assert tool["outputText"] == "fake-tool-ok"
            full = await backend.get_tool_call(session_id, tool["callId"])
            assert full["outputText"] == "fake-tool-ok"
            assert (await backend.get_session(session_id))["summary"]["title"] == "hello fake"

            options = (await backend.get_session(session_id))["configOptions"]
            values = [option["value"] for group in options[0]["options"] for option in group["options"]]
            updated = await backend.set_config(session_id, "model", values[-1])
            assert updated[0]["currentValue"] == values[-1]
            assert (await backend.get_session(session_id))["route"]["model"] == "fake-smart"

            await backend.close_session(session_id)
            assert (await backend.get_session(session_id))["state"] == "detached"
        finally:
            await backend.shutdown()

    run(scenario())


def test_workspace_switch_keeps_single_child(env):
    async def scenario():
        backend = AcpBackend(env.settings, EventHub())
        try:
            first = await backend.create_session(env.wsid_one)
            first_pid = backend.acp_pid
            second = await backend.create_session(env.wsid_two)
            second_pid = backend.acp_pid
            assert first_pid is not None and second_pid is not None
            assert first_pid != second_pid
            assert Path(backend.acp_cwd or "") == env.ws_two.resolve()
            with pytest.raises(ProcessLookupError):
                os.kill(first_pid, 0)
            assert (await backend.get_session(first["summary"]["sessionId"]))["state"] == "detached"
            assert (await backend.get_session(second["summary"]["sessionId"]))["state"] == "idle"
        finally:
            await backend.shutdown()

    run(scenario())


def test_restart_keeps_history_and_resumes(env):
    async def scenario():
        backend_one = AcpBackend(env.settings, EventHub())
        detail = await backend_one.create_session(env.wsid_one)
        session_id = detail["summary"]["sessionId"]
        _, events = await prompt_turn(backend_one, session_id, "first turn")
        assert events[-1]["type"] == "turn.end"
        await backend_one.shutdown()

        backend_two = AcpBackend(env.settings, EventHub())
        try:
            page = await backend_two.get_transcript(session_id, None, 30, True, 4000)
            users = [item for item in page["items"] if item["kind"] == "user"]
            assert [item["text"] for item in users] == ["first turn"]
            # Sending again auto-resumes the stored ACP session (fake accepts resume).
            _, second = await prompt_turn(backend_two, session_id, "second turn")
            assert second[-1]["type"] == "turn.end"
            assert second[-1]["requestId"] != events[-1]["requestId"]
            page = await backend_two.get_transcript(session_id, None, 30, True, 4000)
            users = [item for item in page["items"] if item["kind"] == "user"]
            assert [item["text"] for item in users] == ["first turn", "second turn"]
            assert (await backend_two.get_session(session_id))["state"] == "idle"
        finally:
            await backend_two.shutdown()

    run(scenario())


def test_resume_errors_are_clear(env, monkeypatch):
    async def scenario():
        backend_one = AcpBackend(env.settings, EventHub())
        detail = await backend_one.create_session(env.wsid_one)
        session_id = detail["summary"]["sessionId"]
        await prompt_turn(backend_one, session_id, "seed")
        await backend_one.shutdown()

        monkeypatch.setenv("FAKE_RESUME_ERROR", "provider")
        backend_two = AcpBackend(env.settings, EventHub())
        try:
            with pytest.raises(ApiError) as exc_info:
                await backend_two.start_turn(session_id, "x", None, None)
            assert exc_info.value.code == "E_PROVIDER_UNAVAILABLE"
            assert exc_info.value.http_status == 409
            assert "no adapter registered" in (exc_info.value.details or "")
        finally:
            await backend_two.shutdown()

        monkeypatch.setenv("FAKE_RESUME_ERROR", "not-resumable")
        backend_three = AcpBackend(env.settings, EventHub())
        try:
            with pytest.raises(ApiError) as exc_info:
                await backend_three.start_turn(session_id, "x", None, None)
            assert exc_info.value.code == "E_NOT_RESUMABLE"
            assert "new session" in exc_info.value.message
        finally:
            await backend_three.shutdown()

    run(scenario())


def test_prompt_rate_limit_error(env, monkeypatch):
    monkeypatch.setenv("FAKE_PROMPT_ERROR", "rate-limit")

    async def scenario():
        backend = AcpBackend(env.settings, EventHub())
        try:
            detail = await backend.create_session(env.wsid_one)
            session_id = detail["summary"]["sessionId"]
            _, events = await prompt_turn(backend, session_id, "rate limited")
            assert events[-1]["type"] == "turn.error"
            assert events[-1]["code"] == "E_RATE_LIMITED"
            assert events[-1]["class"] == "rate-limit"
            assert (await backend.get_session(session_id))["state"] == "idle"
        finally:
            await backend.shutdown()

    run(scenario())


def test_cancel_turn(env):
    async def scenario():
        backend = AcpBackend(env.settings, EventHub())
        try:
            detail = await backend.create_session(env.wsid_one)
            session_id = detail["summary"]["sessionId"]
            queue: asyncio.Queue = asyncio.Queue()
            backend.hub.subscribe(session_id, queue)
            accepted = await backend.start_turn(session_id, "cancel me", None, None)
            events: list[dict] = []
            while True:
                event = await asyncio.wait_for(queue.get(), timeout=10)
                events.append(event)
                if event["type"] == "permission.request":
                    break
            await backend.cancel_turn(session_id, accepted["requestId"])
            while True:
                event = await asyncio.wait_for(queue.get(), timeout=10)
                events.append(event)
                if event["type"] == "turn.end":
                    break
            backend.hub.unsubscribe(session_id, queue)
            assert events[-1]["stopReason"] == "cancelled"
            assert any(event["type"] == "permission.resolved" and event["outcome"] == "cancelled" for event in events)
            assert (await backend.get_session(session_id))["state"] == "idle"
            users = [item for item in (await backend.get_transcript(session_id, None, 30, True, 4000))["items"]
                     if item["kind"] == "user"]
            assert [item["text"] for item in users] == ["cancel me"]
        finally:
            await backend.shutdown()

    run(scenario())


def test_create_session_by_cwd_and_synthetic_workspace(env):
    async def scenario():
        backend = AcpBackend(env.settings, EventHub())
        try:
            detail = await backend.create_session(None, str(env.ws_three))
            expected = synthetic_workspace_id(str(env.ws_three.resolve()))
            assert detail["summary"]["workspaceId"] == expected
            workspaces = await backend.list_workspaces()
            synthetic = next(item for item in workspaces if item["id"] == expected)
            assert synthetic["sessionCount"] == 1
            assert synthetic["path"] == str(env.ws_three.resolve())
            assert await backend.list_workspaces()  # registry entries still present
            with pytest.raises(ApiError) as exc_info:
                await backend.create_session(None, str(env.tmp / "missing"))
            assert exc_info.value.code == "E_WORKSPACE_MISSING"
            with pytest.raises(ApiError) as exc_info:
                await backend.create_session("unknown-workspace")
            assert exc_info.value.code == "E_WORKSPACE_UNKNOWN"
        finally:
            await backend.shutdown()

    run(scenario())


def test_unavailable_acp_is_clear(env):
    settings = Settings(
        mock=False, dsh_home=env.home, state_dir=env.tmp / "state2",
        db_path=env.tmp / "state2" / "pocket.db",
        dsh_command=("/nonexistent/dsh-binary-for-tests",),
        acp_initialize_timeout_seconds=2,
    )

    async def scenario():
        backend = AcpBackend(settings, EventHub())
        try:
            with pytest.raises(ApiError) as exc_info:
                await backend.create_session(env.wsid_one)
            assert exc_info.value.code == "E_ACP_UNAVAILABLE"
            assert exc_info.value.http_status == 503
            assert await backend.list_workspaces()  # registry reads still work
        finally:
            await backend.shutdown()

    run(scenario())


def test_app_websocket_with_real_backend(env):
    server = LiveServer(env.settings, env.tmp)
    server.start()
    try:
        with httpx.Client(base_url=server.base_url, timeout=20) as client:
            workspaces = client.get("/api/v1/workspaces").json()["workspaces"]
            workspace = next(item for item in workspaces if item["id"] == env.wsid_one)
            created = client.post("/api/v1/sessions", json={"workspaceId": workspace["id"]})
            assert created.status_code == 201, created.text
            session_id = created.json()["session"]["summary"]["sessionId"]

            connection = server.connect_ws()
            try:
                hello = ws_recv(connection)
                assert hello["type"] == "hello"
                ws_send(connection, {"type": "subscribe", "sessionId": session_id})
                assert ws_recv(connection)["type"] == "history.snapshot"
                assert ws_recv(connection)["type"] == "session.state"
                ws_send(
                    connection,
                    {"type": "prompt", "requestId": "turn_ws_1", "sessionId": session_id,
                     "clientMessageId": "cm-ws-1", "text": "hello from ws"},
                )

                def respond(event: dict) -> None:
                    if event["type"] == "permission.request":
                        ws_send(
                            connection,
                            {"type": "permission.respond", "requestId": event["requestId"],
                             "decision": "allow-once"},
                        )

                events = ws_collect(connection, stop=("turn.end",), timeout=20, on_event=respond)
                assert events[-1]["type"] == "turn.end"
                assert events[-1]["requestId"] == "turn_ws_1"
            finally:
                connection.close()

            page = client.get(f"/api/v1/sessions/{session_id}/transcript").json()
            assert [item["kind"] for item in page["items"]][0] == "user"
            assert any(item["kind"] == "assistant" for item in page["items"])
    finally:
        server.stop()
