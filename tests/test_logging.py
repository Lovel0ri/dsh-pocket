"""Operational checks: no auth material, no prompt contents in logs."""

from __future__ import annotations

import json
import logging
import time

import httpx

from conftest import wait_for_state


def test_health_reports_no_auth(server):
    with httpx.Client(base_url=server.base_url, timeout=10) as client:
        body = client.get("/api/v1/health").json()
    assert body["auth"] == "none"
    assert "token" not in json.dumps(body).lower()


def test_no_token_file_or_auth_routes(server):
    with server.client() as client:
        assert client.get("/api/v1/workspaces").status_code == 200
        assert client.post("/api/v1/auth/pair", json={"code": "123456"}).status_code == 404
        assert client.post("/api/v1/auth/ws-ticket").status_code == 404
        assert client.post("/api/v1/auth/revoke").status_code == 404
    assert not (server.settings.state_dir / "token").exists()


def test_logs_do_not_contain_prompt_text(server, caplog):
    caplog.set_level(logging.DEBUG)
    secret = "SECRET-PROMPT-TEXT-9f3c"
    with server.client() as client:
        workspaces = client.get("/api/v1/workspaces").json()["workspaces"]
        session_id = client.post(
            "/api/v1/sessions", json={"workspaceId": workspaces[0]["id"]}
        ).json()["session"]["summary"]["sessionId"]
        accepted = client.post(
            f"/api/v1/sessions/{session_id}/messages",
            json={"clientMessageId": "cm-log-1", "text": secret},
        )
        assert accepted.status_code == 202
        client.post(f"/api/v1/sessions/{session_id}/cancel", json={})
        wait_for_state(client, session_id, "idle")

    server_text = "\n".join(
        record.getMessage()
        for record in caplog.records
        if record.name.startswith(("dsh_pocket", "uvicorn"))
    )
    assert server_text
    assert secret not in server_text
    assert "authorization" not in server_text.lower()
