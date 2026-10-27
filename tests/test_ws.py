"""WebSocket contract tests (docs/api.md §4, §5.3)."""

from __future__ import annotations

import json

import pytest


from conftest import ws_collect, ws_recv, ws_send

TURN_EVENT_TYPES = {
    "assistant.delta",
    "assistant.thought_delta",
    "tool.started",
    "tool.finished",
    "turn.end",
}


def _session_id(server, title: str = "安装 dsh-our-free-model 仓库") -> str:
    with server.client() as client:
        workspaces = client.get("/api/v1/workspaces").json()["workspaces"]
        alpha = next(workspace for workspace in workspaces if workspace["path"] == str(server.tree["alpha"].resolve()))
        sessions = client.get("/api/v1/sessions", params={"workspaceId": alpha["id"]}).json()["sessions"]
        return next(session["sessionId"] for session in sessions if session["title"] == title)


def _connect(server):
    connection = server.connect_ws()
    hello = ws_recv(connection)
    assert hello["type"] == "hello"
    return connection, hello


def _subscribe(connection, session_id: str, limit: int = 2) -> tuple[dict, dict]:
    ws_send(connection, {"type": "subscribe", "sessionId": session_id, "limit": limit})
    snapshot = ws_recv(connection)
    state = ws_recv(connection)
    assert snapshot["type"] == "history.snapshot"
    assert state["type"] == "session.state"
    return snapshot, state


def test_hello_payload(server):
    connection, hello = _connect(server)
    try:
        assert hello["apiVersion"] == 1
        assert hello["serverVersion"] == "0.1.0"
        assert hello["capabilities"] == {"image": False}
        assert len(hello["workspaces"]) == 3
        assert all("sessionCount" in workspace for workspace in hello["workspaces"])
    finally:
        connection.close()


def test_subscribe_returns_snapshot_and_state(server):
    session_id = _session_id(server)
    connection, _hello = _connect(server)
    try:
        snapshot, state = _subscribe(connection, session_id)
        assert snapshot["sessionId"] == session_id
        assert [item["seq"] for item in snapshot["items"]] == [5, 5, 6]
        assert snapshot["hasMore"] is True
        assert snapshot["nextBefore"] == 5
        assert snapshot["session"]["sessionId"] == session_id
        assert state["sessionId"] == session_id
        assert state["state"] == "detached"
        assert state["lockState"] == "free"
    finally:
        connection.close()


def test_prompt_full_flow_with_permission(server):
    session_id = _session_id(server)
    connection, _hello = _connect(server)
    try:
        _subscribe(connection, session_id)
        ws_send(
            connection,
            {
                "type": "prompt",
                "requestId": "turn_test_1",
                "sessionId": session_id,
                "clientMessageId": "cm-ws-1",
                "text": "请只回复 OK",
            },
        )
        permission_seen = {}

        def respond(event: dict) -> None:
            if event["type"] != "permission.request":
                return
            permission_seen["event"] = event
            assert event["sessionId"] == session_id
            assert event["toolName"] == "bash"
            assert "printf" in event["argumentsPreview"]
            assert [option["optionId"] for option in event["options"]] == ["allow-once", "reject-once"]
            assert event["timeoutMs"] == 5000  # server fixture uses 5 s
            ws_send(
                connection,
                {"type": "permission.respond", "requestId": event["requestId"], "decision": "allow-once"},
            )

        events = ws_collect(connection, stop=("turn.end",), on_event=respond)
        idle = ws_recv(connection)
    finally:
        connection.close()

    types = [event["type"] for event in events]
    assert types[0] == "session.state"
    assert events[0]["state"] == "streaming"
    assert "assistant.thought_delta" in types
    assert "tool.started" in types
    assert "permission.request" in types
    assert "permission.resolved" in types
    assert "tool.finished" in types
    assert "assistant.delta" in types
    assert "usage" in types
    assert types[-1] == "turn.end"
    assert idle["type"] == "session.state" and idle["state"] == "idle"

    # every hub event carries a strictly increasing per-session seq and sessionId
    seqs = [event["seq"] for event in events]
    assert seqs == sorted(seqs) and len(seqs) == len(set(seqs))
    assert all(event["sessionId"] == session_id for event in events)
    assert idle["seq"] > events[-1]["seq"]

    # turn-scoped events echo the client requestId; permission ids are separate
    assert all(event["requestId"] == "turn_test_1" for event in events if event["type"] in TURN_EVENT_TYPES)
    permission = permission_seen["event"]
    assert permission["requestId"].startswith("perm_")
    resolved = next(event for event in events if event["type"] == "permission.resolved")
    assert resolved["outcome"] == "allow-once"

    finished = next(event for event in events if event["type"] == "tool.finished")
    assert finished["status"] == "completed"
    assert finished["toolCallId"] == permission["toolCallId"]
    assert finished["output"] == [{"type": "text", "text": "hello from dsh-pocket mock\n"}]

    delta_text = "".join(event["text"] for event in events if event["type"] == "assistant.delta")
    assert "请只回复 OK" in delta_text
    assert "收到：" in delta_text


def test_prompt_permission_reject_flow(server):
    session_id = _session_id(server)
    connection, _hello = _connect(server)
    try:
        ws_send(
            connection,
            {"type": "prompt", "requestId": "turn_reject", "sessionId": session_id, "text": "测试拒绝"},
        )

        def reject(event: dict) -> None:
            if event["type"] == "permission.request":
                ws_send(
                    connection,
                    {"type": "permission.respond", "requestId": event["requestId"], "decision": "reject-once"},
                )

        events = ws_collect(connection, stop=("turn.end",), on_event=reject)
        idle = ws_recv(connection)
    finally:
        connection.close()
    resolved = next(event for event in events if event["type"] == "permission.resolved")
    finished = next(event for event in events if event["type"] == "tool.finished")
    turn_end = events[-1]
    assert resolved["outcome"] == "reject-once"
    assert finished["status"] == "failed"
    assert "rejected" in finished["output"][0]["text"]
    assert turn_end["stopReason"] == "end_turn"
    assert idle["state"] == "idle"


def test_cancel_turn_then_continue(server):
    session_id = _session_id(server)
    connection, _hello = _connect(server)
    try:
        ws_send(
            connection,
            {"type": "prompt", "requestId": "turn_cancel_1", "sessionId": session_id, "text": "取消测试"},
        )
        pre = ws_collect(connection, stop=("permission.request",))
        assert pre[-1]["type"] == "permission.request"
        ws_send(connection, {"type": "cancel", "sessionId": session_id, "requestId": "turn_cancel_1"})
        tail = ws_collect(connection, stop=("turn.end",))
        idle = ws_recv(connection)
        assert tail[-1]["stopReason"] == "cancelled"
        assert any(
            event["type"] == "permission.resolved" and event["outcome"] == "cancelled" for event in tail
        )
        assert idle["state"] == "idle"

        # the session remains usable after cancellation
        ws_send(
            connection,
            {"type": "prompt", "requestId": "turn_cancel_2", "sessionId": session_id, "text": "继续"},
        )

        def allow(event: dict) -> None:
            if event["type"] == "permission.request":
                ws_send(
                    connection,
                    {"type": "permission.respond", "requestId": event["requestId"], "decision": "allow-once"},
                )

        second = ws_collect(connection, stop=("turn.end",), on_event=allow)
        assert ws_recv(connection)["state"] == "idle"
        assert second[-1]["requestId"] == "turn_cancel_2"
        assert second[-1]["stopReason"] == "end_turn"
    finally:
        connection.close()


def test_config_set_and_rest_broadcast(server):
    session_id = _session_id(server, "移动端联调会话")
    with server.client() as client:
        assert client.post(f"/api/v1/sessions/{session_id}/resume").status_code == 200
        detail = client.get(f"/api/v1/sessions/{session_id}").json()
    values = [option["value"] for group in detail["configOptions"][0]["options"] for option in group["options"]]
    target = values[-1]

    connection, _hello = _connect(server)
    try:
        _subscribe(connection, session_id)
        ws_send(
            connection,
            {
                "type": "config.set",
                "requestId": "cfg-1",
                "sessionId": session_id,
                "configId": "model",
                "value": target,
            },
        )
        update = ws_recv(connection)
        assert update["type"] == "config.update"
        assert update["sessionId"] == session_id
        assert update["configOptions"][0]["currentValue"] == target
        assert update["seq"] >= 1

        # REST-side change is broadcast to the connected client too
        with server.client() as client:
            response = client.put(
                f"/api/v1/sessions/{session_id}/config",
                json={"configId": "model", "value": values[0]},
            )
            assert response.status_code == 200
        broadcast = ws_recv(connection)
        assert broadcast["type"] == "config.update"
        assert broadcast["configOptions"][0]["currentValue"] == values[0]
    finally:
        connection.close()


def test_history_page_and_ping(server):
    session_id = _session_id(server)
    connection, _hello = _connect(server)
    try:
        snapshot, _state = _subscribe(connection, session_id)
        ws_send(
            connection,
            {
                "type": "history.page",
                "requestId": "hp-1",
                "sessionId": session_id,
                "before": snapshot["nextBefore"],
                "limit": 2,
            },
        )
        result = ws_recv(connection)
        assert result["type"] == "history.page.result"
        assert result["requestId"] == "hp-1"
        assert result["sessionId"] == session_id
        assert [item["seq"] for item in result["items"]] == [3, 4]
        assert result["nextBefore"] == 3
        assert result["hasMore"] is True

        ws_send(connection, {"type": "ping", "nonce": 7})
        pong = ws_recv(connection)
        assert pong == {"type": "pong", "nonce": 7}
    finally:
        connection.close()


def test_ws_error_events(server):
    session_id = _session_id(server)
    connection, _hello = _connect(server)
    try:
        ws_send(connection, {"type": "prompt", "requestId": "t1", "sessionId": "session-missing", "text": "x"})
        not_found = ws_recv(connection)
        assert not_found["type"] == "error"
        assert not_found["code"] == "E_NOT_FOUND"

        ws_send(connection, {"type": "does-not-exist", "sessionId": session_id})
        unknown_type = ws_recv(connection)
        assert unknown_type["type"] == "error"
        assert unknown_type["code"] == "E_INVALID_REQUEST"

        connection.send("{not json")
        bad_json = ws_recv(connection)
        assert bad_json["type"] == "error"
        assert bad_json["code"] == "E_INVALID_REQUEST"

        ws_send(connection, {"type": "prompt", "requestId": "t2", "sessionId": session_id, "text": "   "})
        empty_text = ws_recv(connection)
        assert empty_text["type"] == "error"
        assert empty_text["code"] == "E_INVALID_REQUEST"

        ws_send(connection, {"type": "subscribe", "sessionId": session_id})
        snapshot = ws_recv(connection)
        assert snapshot["type"] == "history.snapshot"
        assert ws_recv(connection)["type"] == "session.state"
        ws_send(
            connection,
            {"type": "history.page", "requestId": "hp", "sessionId": session_id, "before": 0, "limit": 2},
        )
        bad_before = ws_recv(connection)
        assert bad_before["type"] == "error"
        assert bad_before["code"] == "E_INVALID_REQUEST"
    finally:
        connection.close()


def test_second_prompt_while_in_flight_is_rejected(server):
    session_id = _session_id(server)
    connection, _hello = _connect(server)
    try:
        ws_send(
            connection,
            {"type": "prompt", "requestId": "turn_first", "sessionId": session_id, "text": "第一条"},
        )
        pre = ws_collect(connection, stop=("permission.request",))
        assert pre[-1]["type"] == "permission.request"

        ws_send(
            connection,
            {"type": "prompt", "requestId": "turn_second", "sessionId": session_id, "text": "第二条"},
        )
        error = ws_recv(connection)
        assert error["type"] == "error"
        assert error["code"] == "E_PROMPT_IN_FLIGHT"
        assert error["requestId"] == "turn_second"

        ws_send(
            connection,
            {"type": "permission.respond", "requestId": pre[-1]["requestId"], "decision": "allow-once"},
        )
        tail = ws_collect(connection, stop=("turn.end",))
        assert tail[-1]["requestId"] == "turn_first"
        assert ws_recv(connection)["state"] == "idle"
    finally:
        connection.close()


def test_rest_messages_stream_to_ws(server):
    session_id = _session_id(server)
    connection, _hello = _connect(server)
    try:
        _subscribe(connection, session_id)
        with server.client() as client:
            accepted = client.post(
                f"/api/v1/sessions/{session_id}/messages",
                json={"clientMessageId": "cm-rest-1", "text": "REST 发消息"},
            )
            assert accepted.status_code == 202
            request_id = accepted.json()["requestId"]
        events = []

        def respond(event: dict) -> None:
            if event["type"] == "permission.request":
                with server.client() as client:
                    response = client.post(
                        f"/api/v1/sessions/{session_id}/permissions/{event['requestId']}",
                        json={"decision": "allow-once"},
                    )
                    assert response.status_code == 200

        events = ws_collect(connection, stop=("turn.end",), on_event=respond)
        assert events[-1]["requestId"] == request_id
        assert events[-1]["stopReason"] == "end_turn"
        assert ws_recv(connection)["state"] == "idle"
    finally:
        connection.close()


def test_rest_closed_session_transcript_still_readable_and_ws_resubscribes(server):
    session_id = _session_id(server, "移动端联调会话")
    with server.client() as client:
        assert client.post(f"/api/v1/sessions/{session_id}/resume").status_code == 200
        assert client.post(f"/api/v1/sessions/{session_id}/close").status_code == 200
    connection, _hello = _connect(server)
    try:
        snapshot, state = _subscribe(connection, session_id)
        assert snapshot["items"]
        assert state["state"] == "detached"
    finally:
        connection.close()


def test_unsubscribe_removes_hub_subscription(server):
    session_id = _session_id(server)
    connection, _hello = _connect(server)
    try:
        _subscribe(connection, session_id)
        hub = server.app.state.runtime.hub
        assert hub.subscriber_count(session_id) == 1
        ws_send(connection, {"type": "unsubscribe", "sessionId": session_id})
        ws_send(connection, {"type": "ping", "nonce": 9})
        pong = ws_recv(connection)
        assert pong == {"type": "pong", "nonce": 9}
        assert hub.subscriber_count(session_id) == 0
    finally:
        connection.close()
