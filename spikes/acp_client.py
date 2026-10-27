"""acp_client.py — minimal, dependency-free ACP (Agent Client Protocol) client.

Wire facts this module encodes (all evidence: docs/protocol.md §2, spike
transcripts in spikes/_transcripts/):

* transport is newline-delimited JSON-RPC 2.0 over the child process's
  stdin/stdout (``ndJsonStream``); stdout carries protocol traffic only, logs
  go to stderr;
* the server is an ACP *agent*: the client sends requests/notifications and
  must answer the server's ``session/request_permission`` requests;
* every message is one UTF-8 line, so a reader thread per pipe is enough.

The client is deliberately transparent: every frame in both directions is
recorded in order to a JSONL transcript, so a spike can be replayed and quoted
as evidence.
"""

from __future__ import annotations

import json
import os
import queue
import shlex
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

DEFAULT_CLI = "/Applications/DeepSeek Harness.app/Contents/Resources/runtime/cli/bin/dsh"
DEFAULT_PROFILE = "acp"

#: ACP error codes observed on the wire (JSON-RPC 2.0 + server detail message).
ERR_INVALID_PARAMS = -32602
ERR_INTERNAL = -32603
ERR_METHOD_NOT_FOUND = -32601
ERR_PARSE = -32700


class AcpError(RuntimeError):
    """A JSON-RPC error response from the agent."""

    def __init__(self, method: str, error: dict[str, Any]):
        self.method = method
        self.code = error.get("code")
        self.message = error.get("message", "")
        self.data = error.get("data")
        super().__init__(f"{method} -> {self.code}: {self.message}")

    @property
    def details(self) -> str | None:
        """Cause text the server put in ``error.data.details``.

        Plain handler failures are serialized by the ACP SDK as
        ``{"code": -32603, "message": "Internal error", "data": {"details": ...}}``
        (source: @agentclientprotocol/sdk ``errorToResult``), so this field is
        the only machine-readable cause for internal errors:
        ``no adapter registered for provider "…"`` or
        ``session "…" is already owned by an active write handle``.
        """
        if isinstance(self.data, dict):
            value = self.data.get("details")
            return value if isinstance(value, str) else None
        return None


def classify_error(exc: "AcpError") -> str:
    """Bucket an ACP error for client-side handling (see docs/protocol.md §3)."""
    details = exc.details or ""
    if exc.code == ERR_METHOD_NOT_FOUND:
        return "unsupported-method"
    if "already owned by an active write handle" in details:
        return "session-locked"
    if "no adapter registered for provider" in details:
        return "provider-unavailable"
    if "not resumable" in exc.message:
        return "not-resumable"
    if "cwd does not match" in exc.message:
        return "cwd-mismatch"
    if "already active" in exc.message:
        return "already-active"
    if "already in flight" in exc.message:
        return "prompt-in-flight"
    if exc.code == ERR_INVALID_PARAMS:
        return "invalid-params"
    if exc.code == ERR_INTERNAL:
        return "internal"
    return "unknown"


class AcpTimeout(RuntimeError):
    pass


@dataclass
class Frame:
    dir: str  # "out" (client -> server) or "in" (server -> client)
    t: float  # seconds since client start
    obj: dict[str, Any]

    def kind(self) -> str:
        if "method" in self.obj and "id" in self.obj:
            return "request" if self.dir == "in" else "request"
        if "method" in self.obj:
            return "notification"
        if "id" in self.obj:
            return "response"
        return "other"


@dataclass
class AcpClient:
    """One ACP agent subprocess plus its request/response bookkeeping."""

    dsh_home: str
    profile: str = DEFAULT_PROFILE
    cli: str = DEFAULT_CLI
    cwd: str | None = None
    transcript_path: str | None = None
    stderr_path: str | None = None
    extra_env: dict[str, str] = field(default_factory=dict)
    #: handler(params) -> response dict for session/request_permission
    permission_handler: Callable[[dict[str, Any]], dict[str, Any]] | None = None
    #: handler(params) -> response dict for any other server->client request
    fallback_request_handler: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None

    proc: subprocess.Popen | None = None
    frames: list[Frame] = field(default_factory=list)
    _pending: dict[int, queue.Queue] = field(default_factory=dict)
    _next_id: int = 0
    _t0: float = 0.0
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _transcript_fh: Any = None
    _stderr_fh: Any = None
    closed: bool = False

    # ------------------------------------------------------------------ process
    def start(self, init_timeout: float = 90.0) -> "AcpClient":
        env = dict(os.environ)
        env["DSH_HOME"] = self.dsh_home
        env.pop("DSH_SESSION", None)
        env.update(self.extra_env)
        argv = [self.cli, "--profile", self.profile]
        if self.stderr_path:
            os.makedirs(os.path.dirname(self.stderr_path) or ".", exist_ok=True)
        self._stderr_fh = open(self.stderr_path, "w") if self.stderr_path else None
        self.proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self._stderr_fh or subprocess.PIPE,
            cwd=self.cwd,
            env=env,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        self._t0 = time.monotonic()
        if self.transcript_path:
            os.makedirs(os.path.dirname(self.transcript_path) or ".", exist_ok=True)
            self._transcript_fh = open(self.transcript_path, "w", encoding="utf-8")
        threading.Thread(target=self._read_stdout, name="acp-out", daemon=True).start()
        if self._stderr_fh is None:
            threading.Thread(target=self._read_stderr, name="acp-err", daemon=True).start()
        # The server emits nothing until it sees a frame, so boot readiness is
        # observed by the first request (initialize) rather than by a greeting.
        time.sleep(0.2)
        if self.proc.poll() is not None:
            raise RuntimeError(
                f"ACP server exited during boot (code {self.proc.returncode}); "
                f"see stderr {self.stderr_path or '(captured)'}"
            )
        return self

    # ------------------------------------------------------------------- readers
    def _record(self, direction: str, obj: dict[str, Any]) -> None:
        frame = Frame(direction, round(time.monotonic() - self._t0, 3), obj)
        with self._lock:
            self.frames.append(frame)
        if self._transcript_fh:
            self._transcript_fh.write(json.dumps({"dir": direction, "t": frame.t, "msg": obj}, ensure_ascii=False) + "\n")
            self._transcript_fh.flush()

    def _read_stdout(self) -> None:
        assert self.proc is not None and self.proc.stdout is not None
        for line in self.proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                self._record("in", {"raw_non_json": line})
                continue
            self._record("in", obj)
            if "method" in obj:
                # server -> client request (needs an answer) or notification
                if "id" in obj:
                    self._answer_server_request(obj)
                continue
            q = self._pending.get(obj.get("id"))
            if q is not None:
                q.put(obj)

    def _read_stderr(self) -> None:
        assert self.proc is not None and self.proc.stderr is not None
        for line in self.proc.stderr:
            if self._transcript_fh:
                self._transcript_fh.write(json.dumps({"dir": "err", "msg": line.rstrip()}, ensure_ascii=False) + "\n")
                self._transcript_fh.flush()

    def _answer_server_request(self, obj: dict[str, Any]) -> None:
        method = obj.get("method", "")
        params = obj.get("params") or {}
        try:
            if method == "session/request_permission" and self.permission_handler is not None:
                result = self.permission_handler(params)
            elif self.fallback_request_handler is not None:
                result = self.fallback_request_handler(method, params)
            else:
                raise LookupError(method)
            self._write({"jsonrpc": "2.0", "id": obj["id"], "result": result})
        except LookupError:
            self._write(
                {
                    "jsonrpc": "2.0",
                    "id": obj["id"],
                    "error": {"code": ERR_METHOD_NOT_FOUND, "message": f"client does not implement {method}"},
                }
            )

    # ------------------------------------------------------------------- writer
    def _write(self, obj: dict[str, Any]) -> None:
        assert self.proc is not None and self.proc.stdin is not None
        self._record("out", obj)
        self.proc.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        msg: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        self._write(msg)

    def request(self, method: str, params: dict[str, Any] | None = None, timeout: float = 120.0) -> dict[str, Any]:
        with self._lock:
            self._next_id += 1
            rid = self._next_id
            q: queue.Queue = queue.Queue(maxsize=1)
            self._pending[rid] = q
        msg: dict[str, Any] = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            msg["params"] = params
        self._write(msg)
        try:
            resp = q.get(timeout=timeout)
        except queue.Empty:
            raise AcpTimeout(f"{method} (id {rid}) timed out after {timeout}s") from None
        finally:
            self._pending.pop(rid, None)
        if "error" in resp:
            raise AcpError(method, resp["error"])
        return resp.get("result") or {}

    # ------------------------------------------------------------- convenience
    def initialize(self, protocol_version: int = 1) -> dict[str, Any]:
        return self.request(
            "initialize",
            {"protocolVersion": protocol_version, "clientCapabilities": {}},
            timeout=120.0,
        )

    def updates(self, session_id: str | None = None) -> list[dict[str, Any]]:
        out = []
        for f in self.frames:
            if f.dir != "in" or f.obj.get("method") != "session/update":
                continue
            params = f.obj.get("params") or {}
            if session_id is not None and params.get("sessionId") != session_id:
                continue
            out.append({**params.get("update", {}), "_t": f.t})
        return out

    def permission_requests(self) -> list[dict[str, Any]]:
        return [
            f.obj.get("params", {})
            for f in self.frames
            if f.dir == "in" and f.obj.get("method") == "session/request_permission"
        ]

    def wait_for(
        self,
        predicate: Callable[[], bool],
        timeout: float = 180.0,
        interval: float = 0.05,
    ) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(interval)
        return False

    def close_session(self, session_id: str, timeout: float = 60.0) -> dict[str, Any]:
        return self.request("session/close", {"sessionId": session_id}, timeout=timeout)

    def shutdown(self, eof: bool = True, timeout: float = 15.0) -> dict[str, Any]:
        """Close stdin (EOF) or SIGTERM, then report exit facts."""
        if self.proc is None:
            return {}
        rc = self.proc.poll()
        if rc is None and eof:
            try:
                assert self.proc.stdin is not None
                self.proc.stdin.close()
            except Exception:  # noqa: BLE001 - best effort
                pass
            try:
                rc = self.proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                rc = None
        if rc is None:
            self.proc.terminate()
            try:
                rc = self.proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                rc = self.proc.wait(timeout=timeout)
        self.closed = True
        if self._transcript_fh:
            self._transcript_fh.close()
            self._transcript_fh = None
        if self._stderr_fh:
            self._stderr_fh.close()
            self._stderr_fh = None
        return {"exitCode": rc, "signal": (self.proc.returncode if self.proc.returncode and self.proc.returncode < 0 else None)}

    # ---------------------------------------------------------------- debugging
    def raw_log(self, limit: int = 0) -> str:
        lines = []
        for f in self.frames:
            if f.dir == "err":
                continue
            body = json.dumps(f.obj, ensure_ascii=False)
            if limit:
                body = body[:limit] + ("…" if len(body) > limit else "")
            lines.append(f"{f.t:8.3f} {f.dir:>3} {body}")
        return "\n".join(lines)


def allow_once(params: dict[str, Any]) -> dict[str, Any]:
    """Answer a permission request by selecting its allow-once option."""
    options = params.get("options") or []
    option_id = next((o["optionId"] for o in options if o.get("kind") == "allow_once"), None)
    if option_id is None and options:
        option_id = options[0]["optionId"]
    return {"outcome": {"outcome": "selected", "optionId": option_id}}


def reject_once(params: dict[str, Any]) -> dict[str, Any]:
    options = params.get("options") or []
    option_id = next((o["optionId"] for o in options if o.get("kind") == "reject_once"), None)
    if option_id is None and options:
        option_id = options[-1]["optionId"]
    return {"outcome": {"outcome": "selected", "optionId": option_id}}


def cancel_permission(_params: dict[str, Any]) -> dict[str, Any]:
    return {"outcome": {"outcome": "cancelled"}}


def shell_join(argv: list[str]) -> str:
    return " ".join(shlex.quote(a) for a in argv)
