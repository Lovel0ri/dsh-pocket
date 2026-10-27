"""CLI smoke test: documented `python3 -m dsh_pocket --mock` command (no auth)."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]
    finally:
        sock.close()


def _wait_health(base_url: str, timeout: float = 20.0) -> httpx.Response:
    deadline = time.time() + timeout
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            response = httpx.get(f"{base_url}/api/v1/health", timeout=0.5)
            if response.status_code == 200:
                return response
        except httpx.HTTPError as exc:
            last_error = exc
        time.sleep(0.05)
    raise AssertionError(f"server did not become healthy: {last_error}")


@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_cli_mock_mode_is_open_and_warns(tmp_path):
    port = _free_port()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_dir = tmp_path / "state"
    env = dict(os.environ)
    env["PYTHONPATH"] = str(PROJECT_ROOT)
    env.pop("DSH_POCKET_HOME", None)
    command = [
        sys.executable, "-m", "dsh_pocket", "--mock", "--host", "127.0.0.1", "--port", str(port),
        "--state-dir", str(state_dir), "--mock-workspace", str(workspace), "--log-level", "WARNING",
    ]
    process = subprocess.Popen(
        command, cwd=str(PROJECT_ROOT), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        health = _wait_health(base_url)
        assert health.json()["auth"] == "none"
        with httpx.Client(base_url=base_url, timeout=10) as client:
            assert client.get("/api/v1/workspaces").status_code == 200
            assert client.get("/api/v1/sessions").status_code == 200
            assert client.get("/api/v1/browse").status_code == 200
            assert client.post("/api/v1/auth/pair", json={"code": "123456"}).status_code == 404
    finally:
        process.terminate()
        try:
            output = process.communicate(timeout=15)[0]
        except subprocess.TimeoutExpired:  # pragma: no cover
            process.kill()
            output = process.communicate(timeout=5)[0]

    assert f"http://127.0.0.1:{port}" in output
    assert "NO AUTHENTICATION" in output or "NONE (personal LAN mode)" in output
    assert "trusted LAN" in output
    assert not (state_dir / "token").exists()
