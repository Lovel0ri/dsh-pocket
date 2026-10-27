"""REST contract tests (docs/api.md §3, §5)."""

from __future__ import annotations

import json

import pytest
import os
import shutil
from pathlib import Path

from conftest import wait_for_state


def _workspace(client, server, name: str) -> dict:
    response = client.get("/api/v1/workspaces")
    assert response.status_code == 200
    workspaces = response.json()["workspaces"]
    target = str(server.tree[name].resolve())
    return next(workspace for workspace in workspaces if workspace["path"] == target)


def _sessions(client, workspace_id: str, **params):
    query = {"workspaceId": workspace_id, **params}
    response = client.get("/api/v1/sessions", params=query)
    assert response.status_code == 200, response.text
    return response.json()


def _find_session(client, workspace_id: str, title: str) -> dict:
    sessions = _sessions(client, workspace_id)["sessions"]
    return next(session for session in sessions if session["title"] == title)


def _model_values(config_options: list[dict]) -> list[str]:
    model = next(option for option in config_options if option["id"] == "model")
    values: list[str] = []
    for group in model["options"]:
        if "options" in group:
            values.extend(option["value"] for option in group["options"])
        elif "value" in group:
            values.append(group["value"])
    return values


def test_workspaces_payload(server):
    with server.client() as client:
        response = client.get("/api/v1/workspaces")
        alpha = _workspace(client, server, "alpha")
        gone = _workspace(client, server, "gone")
    assert response.status_code == 200
    body = response.json()
    assert len(body["workspaces"]) == 3
    assert set(alpha) == {
        "id",
        "path",
        "title",
        "exists",
        "writable",
        "sessionCount",
        "updatedAt",
        "defaultSessionId",
    }
    assert alpha["exists"] is True
    assert alpha["writable"] is True
    assert alpha["sessionCount"] == 2
    assert alpha["defaultSessionId"] is not None
    assert alpha["updatedAt"].endswith("Z")
    assert gone["exists"] is True  # still on disk at this point


def test_browse_listing_and_default_root(server):
    root = str(server.tree["alpha"].parent.parent.resolve())
    with server.client() as client:
        default = client.get("/api/v1/browse")
        hidden = client.get("/api/v1/browse", params={"showHidden": "true"})
        alpha = client.get("/api/v1/browse", params={"path": str(server.tree["alpha"].resolve())})
    assert default.status_code == 200
    body = default.json()
    assert body["path"] == root
    assert body["roots"] == [root]
    names = {entry["name"] for entry in body["entries"]}
    assert "workspaces" in names
    assert ".hidden-root-dir" not in names
    assert {entry["type"] for entry in body["entries"]} == {"directory"}
    assert all(entry["path"].startswith(root) for entry in body["entries"])
    assert {entry["name"] for entry in hidden.json()["entries"]} >= {"workspaces", ".hidden-root-dir"}
    assert alpha.status_code == 200
    alpha_names = {entry["name"] for entry in alpha.json()["entries"]}
    assert alpha_names == set()  # notes.txt is a file, .hidden-dir hidden by default


def test_browse_rejects_traversal_and_invalid_paths(server):
    alpha = server.tree["alpha"].resolve()
    escape_link = server.root / "escape-link"
    inside_link = server.root / "inside-link"
    try:
        os.symlink("/etc", escape_link)
        os.symlink(str(alpha), inside_link)
    except OSError:  # pragma: no cover - platforms without symlink support
        escape_link = inside_link = None

    with server.client() as client:
        cases = {
            "absolute-outside": client.get("/api/v1/browse", params={"path": "/etc"}),
            "dotdot": client.get("/api/v1/browse", params={"path": str(server.root / "..")}),
            "missing": client.get("/api/v1/browse", params={"path": str(server.root / "nope")}),
            "file": client.get("/api/v1/browse", params={"path": str(alpha / "notes.txt")}),
            "relative": client.get("/api/v1/browse", params={"path": "relative/dir"}),
        }
        if escape_link is not None:
            cases["symlink-escape"] = client.get("/api/v1/browse", params={"path": str(escape_link)})
        listing = client.get("/api/v1/browse")
        inside = client.get("/api/v1/browse", params={"path": str(inside_link)}) if inside_link else None

    assert cases["absolute-outside"].status_code == 403
    assert cases["absolute-outside"].json()["error"]["code"] == "E_PATH_FORBIDDEN"
    assert cases["dotdot"].status_code == 403
    assert cases["missing"].status_code == 400
    assert cases["missing"].json()["error"]["code"] == "E_PATH_INVALID"
    assert cases["file"].status_code == 400
    assert cases["relative"].status_code == 400
    if escape_link is not None:
        assert cases["symlink-escape"].status_code == 403
        assert "escape-link" not in {entry["name"] for entry in listing.json()["entries"]}
        assert inside.status_code == 200
        assert inside.json()["path"] == str(alpha)
        assert "inside-link" in {entry["name"] for entry in listing.json()["entries"]}


def test_sessions_pagination_and_filters(server):
    with server.client() as client:
        alpha = _workspace(client, server, "alpha")
        gone = _workspace(client, server, "gone")
        page_one = _sessions(client, alpha["id"], limit=1)
        assert len(page_one["sessions"]) == 1
        assert "nextCursor" in page_one
        page_two = _sessions(client, alpha["id"], limit=1, cursor=page_one["nextCursor"])
        assert len(page_two["sessions"]) == 1
        assert page_two["sessions"][0]["sessionId"] != page_one["sessions"][0]["sessionId"]
        assert "nextCursor" not in page_two
        gone_default = _sessions(client, gone["id"])
        gone_archived = _sessions(client, gone["id"], includeArchived="true")
        invalid_cursor = client.get("/api/v1/sessions", params={"workspaceId": alpha["id"], "cursor": "%%%bad%%%"})
        zero_limit = client.get("/api/v1/sessions", params={"workspaceId": alpha["id"], "limit": 0})
        big_limit = client.get("/api/v1/sessions", params={"workspaceId": alpha["id"], "limit": 101})
        unknown_workspace = _sessions(client, "does-not-exist")
    assert len(gone_default["sessions"]) == 1
    assert len(gone_archived["sessions"]) == 2
    archived = next(session for session in gone_archived["sessions"] if session["title"] == "移动端联调会话")
    assert archived["archived"] is True
    assert invalid_cursor.status_code == 400
    assert invalid_cursor.json()["error"]["code"] == "E_INVALID_REQUEST"
    assert zero_limit.status_code == 400
    assert big_limit.status_code == 400
    assert unknown_workspace["sessions"] == []


def test_session_detail_resume_and_close(server):
    with server.client() as client:
        alpha = _workspace(client, server, "alpha")
        session = _find_session(client, alpha["id"], "安装 dsh-our-free-model 仓库")
        session_id = session["sessionId"]
        detail = client.get(f"/api/v1/sessions/{session_id}")
        resume = client.post(f"/api/v1/sessions/{session_id}/resume")
        second_resume = client.post(f"/api/v1/sessions/{session_id}/resume")
        resumed_detail = client.get(f"/api/v1/sessions/{session_id}")
        closed = client.post(f"/api/v1/sessions/{session_id}/close")
        closed_again = client.post(f"/api/v1/sessions/{session_id}/close")
        closed_detail = client.get(f"/api/v1/sessions/{session_id}")
        resumed_again = client.post(f"/api/v1/sessions/{session_id}/resume")
        unknown = client.get("/api/v1/sessions/session-missing")
    assert detail.status_code == 200
    detail_body = detail.json()
    assert detail_body["summary"]["sessionId"] == session_id
    assert detail_body["state"] == "detached"
    assert detail_body["configOptions"] is None
    assert detail_body["lockState"] == "free"
    assert detail_body["capabilities"] == {"image": False, "list": True, "resume": True, "close": True}
    assert detail_body["agentInfo"]["name"] == "dsh-pocket-mock"
    assert resume.status_code == 200
    assert resume.json()["state"] == "idle"
    assert [option["id"] for option in resume.json()["configOptions"]] == ["model", "reasoning_effort"]
    assert second_resume.status_code == 409
    assert second_resume.json()["error"]["code"] == "E_ALREADY_ACTIVE"
    assert resumed_detail.json()["state"] == "idle"
    assert resumed_detail.json()["configOptions"] is not None
    assert closed.status_code == 200 and closed.json()["ok"] is True
    assert closed_again.status_code == 200
    assert closed_detail.json()["state"] == "detached"
    assert closed_detail.json()["configOptions"] is None
    assert resumed_again.status_code == 200
    assert unknown.status_code == 404
    assert unknown.json()["error"]["code"] == "E_NOT_FOUND"


def test_create_session_validation(server):
    with server.client() as client:
        alpha = _workspace(client, server, "alpha")
        created = client.post("/api/v1/sessions", json={"workspaceId": alpha["id"]})
        unknown = client.post("/api/v1/sessions", json={"workspaceId": "nope"})
        missing_body = client.post("/api/v1/sessions", json={})
    assert created.status_code == 201
    session = created.json()["session"]
    assert session["state"] == "idle"
    assert session["summary"]["cwd"] == str(server.tree["alpha"].resolve())
    assert session["summary"]["title"] == "新会话"
    assert session["configOptions"] is not None
    assert unknown.status_code == 404
    assert unknown.json()["error"]["code"] == "E_WORKSPACE_UNKNOWN"
    assert missing_body.status_code == 400
    assert missing_body.json()["error"]["code"] == "E_INVALID_REQUEST"

    # A registered workspace whose directory disappeared is rejected (409) and
    # reported by the workspace list as exists=false.
    with server.client() as client:
        gone = _workspace(client, server, "gone")
    shutil.rmtree(server.tree["gone"])
    with server.client() as client:
        failed = client.post("/api/v1/sessions", json={"workspaceId": gone["id"]})
        gone_after = _workspace(client, server, "gone")
    assert failed.status_code == 409
    assert failed.json()["error"]["code"] == "E_WORKSPACE_MISSING"
    assert gone_after["exists"] is False


def test_transcript_pagination(server):
    with server.client() as client:
        alpha = _workspace(client, server, "alpha")
        session_id = _find_session(client, alpha["id"], "安装 dsh-our-free-model 仓库")["sessionId"]
        page1 = client.get(f"/api/v1/sessions/{session_id}/transcript", params={"limit": 2})
        page2 = client.get(
            f"/api/v1/sessions/{session_id}/transcript",
            params={"limit": 2, "before": page1.json()["nextBefore"]},
        )
        page3 = client.get(
            f"/api/v1/sessions/{session_id}/transcript",
            params={"limit": 2, "before": page2.json()["nextBefore"]},
        )
        no_reasoning = client.get(
            f"/api/v1/sessions/{session_id}/transcript",
            params={"limit": 30, "includeReasoning": "false"},
        )
        with_reasoning = client.get(
            f"/api/v1/sessions/{session_id}/transcript",
            params={"limit": 30, "includeReasoning": "true"},
        )
        truncated = client.get(
            f"/api/v1/sessions/{session_id}/transcript",
            params={"limit": 30, "maxChars": 10},
        )
        full_tool = client.get(f"/api/v1/sessions/{session_id}/tool-calls/call_b")
        missing_tool = client.get(f"/api/v1/sessions/{session_id}/tool-calls/call_zzz")
        missing_session = client.get("/api/v1/sessions/nope/transcript")
    assert page1.status_code == 200
    body1 = page1.json()
    assert [item["seq"] for item in body1["items"]] == [5, 5, 6]
    assert body1["nextBefore"] == 5 and body1["hasMore"] is True
    assert body1["session"]["lastSeq"] == 6
    body2 = page2.json()
    assert [item["seq"] for item in body2["items"]] == [3, 4]
    assert body2["nextBefore"] == 3 and body2["hasMore"] is True
    body3 = page3.json()
    assert [item["seq"] for item in body3["items"]] == [1, 2]
    assert body3["nextBefore"] == 1 and body3["hasMore"] is False
    all_items = body1["items"] + body2["items"] + body3["items"]
    assert len(all_items) == 7
    assert len({item["id"] for item in all_items if item["kind"] != "tool"}) == 5
    # same-seq assistant + tool group is never split across pages
    assert any(item["kind"] == "tool" and item["seq"] == 5 for item in body1["items"])
    assert any(item["kind"] == "assistant" and item["seq"] == 5 for item in body1["items"])

    assert all("reasoning" not in item for item in no_reasoning.json()["items"] if item["kind"] == "assistant")
    assert any(item.get("reasoning") for item in with_reasoning.json()["items"] if item["kind"] == "assistant")
    truncated_tools = [item for item in truncated.json()["items"] if item["kind"] == "tool"]
    assert truncated_tools
    assert all(len(item["outputText"]) <= 10 and item["truncated"] is True for item in truncated_tools)
    assert full_tool.status_code == 200
    assert len(full_tool.json()["outputText"]) > 10
    assert full_tool.json()["name"] == "bash"
    assert json.loads(full_tool.json()["arguments"])["command"] == "sed -n '1,40p' README.md"
    assert missing_tool.status_code == 404
    assert missing_session.status_code == 404


def test_messages_cancel_config_and_permission_errors(server):
    with server.client() as client:
        alpha = _workspace(client, server, "alpha")
        session_id = client.post("/api/v1/sessions", json={"workspaceId": alpha["id"]}).json()["session"]["summary"]["sessionId"]
        accepted = client.post(
            f"/api/v1/sessions/{session_id}/messages",
            json={"clientMessageId": "cm-1", "text": "只回复 OK"},
        )
        duplicate = client.post(
            f"/api/v1/sessions/{session_id}/messages",
            json={"clientMessageId": "cm-1", "text": "只回复 OK"},
        )
        empty = client.post(
            f"/api/v1/sessions/{session_id}/messages",
            json={"clientMessageId": "cm-2", "text": "   "},
        )
        cancelled = client.post(f"/api/v1/sessions/{session_id}/cancel", json={})
    assert accepted.status_code == 202, accepted.text
    request_id = accepted.json()["requestId"]
    assert request_id.startswith("turn_")
    assert accepted.json()["clientMessageId"] == "cm-1"
    assert duplicate.status_code == 202
    assert duplicate.json()["requestId"] == request_id
    assert empty.status_code == 400
    assert empty.json()["error"]["code"] == "E_INVALID_REQUEST"
    assert cancelled.status_code == 200 and cancelled.json()["ok"] is True

    with server.client() as client:
        wait_for_state(client, session_id, "idle")
        transcript = client.get(f"/api/v1/sessions/{session_id}/transcript", params={"limit": 30})
        duplicate_after = client.post(
            f"/api/v1/sessions/{session_id}/messages",
            json={"clientMessageId": "cm-1", "text": "只回复 OK"},
        )
        detail = client.get(f"/api/v1/sessions/{session_id}").json()
        model_values = _model_values(detail["configOptions"])
        updated = client.put(
            f"/api/v1/sessions/{session_id}/config",
            json={"configId": "model", "value": model_values[-1]},
        )
        bad_config_id = client.put(
            f"/api/v1/sessions/{session_id}/config",
            json={"configId": "temperature", "value": "1"},
        )
        bad_config_value = client.put(
            f"/api/v1/sessions/{session_id}/config",
            json={"configId": "model", "value": "not-a-model"},
        )
        unknown_permission = client.post(
            f"/api/v1/sessions/{session_id}/permissions/perm_missing",
            json={"decision": "allow-once"},
        )
        bad_decision = client.post(
            f"/api/v1/sessions/{session_id}/permissions/perm_missing",
            json={"decision": "allow-always"},
        )
        unknown_session_cancel = client.post("/api/v1/sessions/nope/cancel", json={})
    assert duplicate_after.status_code == 202
    assert duplicate_after.json()["requestId"] == request_id
    items = transcript.json()["items"]
    assert any(item["kind"] == "user" and item["text"] == "只回复 OK" for item in items)
    assert detail["summary"]["title"] == "只回复 OK"
    assert updated.status_code == 200
    assert updated.json()["configOptions"][0]["currentValue"] == model_values[-1]
    assert bad_config_id.status_code == 400
    assert bad_config_id.json()["error"]["code"] == "E_ACP_INVALID"
    assert bad_config_value.status_code == 400
    assert unknown_permission.status_code == 404
    assert bad_decision.status_code == 400
    assert unknown_session_cancel.status_code == 404


def test_static_root_fallback(server):
    with server.client() as client:
        root = client.get("/")
        favicon = client.get("/favicon.ico")
    assert root.status_code == 200
    body = root.json()
    assert body["name"] == "dsh-pocket"
    assert body["api"] == "/api/v1/health"
    assert favicon.status_code == 204


def test_create_session_rejects_unwritable_workspace(server):
    with server.client() as client:
        alpha = _workspace(client, server, "alpha")
    workspace_path = server.tree["alpha"]
    original_mode = workspace_path.stat().st_mode
    try:
        workspace_path.chmod(0o500)
        if os.access(workspace_path, os.W_OK):  # pragma: no cover - root ignores modes
            pytest.skip("filesystem ignores directory permissions here")
        with server.client() as client:
            response = client.post("/api/v1/sessions", json={"workspaceId": alpha["id"]})
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "E_WORKSPACE_MISSING"
        assert "not writable" in response.json()["error"]["message"]
    finally:
        workspace_path.chmod(original_mode)


def test_no_auth_surface(server):
    with server.client() as client:
        health = client.get("/api/v1/health")
        assert health.status_code == 200
        assert health.json()["auth"] == "none"
        for path in ("/api/v1/workspaces", "/api/v1/sessions", "/api/v1/browse"):
            assert client.get(path).status_code == 200
        assert client.post("/api/v1/auth/pair", json={"code": "123456"}).status_code == 404
        assert client.post("/api/v1/auth/ws-ticket").status_code == 404


def test_rate_limit_without_auth(server_factory):
    server = server_factory(api_rate_max=3, api_rate_window_seconds=60.0)
    with server.client() as client:
        statuses = [client.get("/api/v1/workspaces").status_code for _ in range(3)]
        blocked = client.get("/api/v1/workspaces")
    assert statuses == [200, 200, 200]
    assert blocked.status_code == 429
    assert blocked.json()["error"]["code"] == "E_RATE_LIMITED"


def test_create_session_with_cwd(server):
    import uuid
    from pathlib import Path

    cwd = server.root / "cwd-workspace"
    cwd.mkdir()
    with server.client() as client:
        created = client.post("/api/v1/sessions", json={"cwd": str(cwd)})
        workspaces = client.get("/api/v1/workspaces").json()["workspaces"]
    assert created.status_code == 201, created.text
    summary = created.json()["session"]["summary"]
    assert summary["cwd"] == str(cwd.resolve())
    expected_workspace = str(uuid.uuid5(uuid.NAMESPACE_URL, str(cwd.resolve())))
    assert summary["workspaceId"] == expected_workspace
    assert any(workspace["id"] == expected_workspace for workspace in workspaces)
    with server.client() as client:
        missing = client.post("/api/v1/sessions", json={"cwd": str(server.root / "nope")})
        neither = client.post("/api/v1/sessions", json={})
    assert missing.status_code == 409
    assert missing.json()["error"]["code"] == "E_WORKSPACE_MISSING"
    assert neither.status_code == 400
