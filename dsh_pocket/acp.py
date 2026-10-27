"""Async JSON-RPC client for one ``dsh --profile acp`` child process.

Wire facts: newline-delimited JSON-RPC 2.0 on stdin/stdout, logs on stderr,
server->client requests only for ``session/request_permission`` (docs/protocol.md §2/§6).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

logger = logging.getLogger("dsh_pocket.acp")

ERR_INVALID_PARAMS = -32602
ERR_INTERNAL = -32603
ERR_METHOD_NOT_FOUND = -32601


class AcpError(Exception):
    def __init__(self, method: str, error: dict[str, Any]) -> None:
        self.method = method
        self.code = error.get("code")
        self.message = error.get("message") or ""
        self.data = error.get("data")
        super().__init__(f"{method} -> {self.code}: {self.message}")

    @property
    def details(self) -> str | None:
        if isinstance(self.data, dict):
            value = self.data.get("details")
            return value if isinstance(value, str) else None
        return None


def classify_error(exc: AcpError) -> str:
    details = exc.details or ""
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
    if exc.code == ERR_METHOD_NOT_FOUND:
        return "unsupported-method"
    if exc.code == ERR_INVALID_PARAMS:
        return "invalid-params"
    if exc.code == ERR_INTERNAL:
        return "internal"
    return "unknown"


class AcpTimeout(Exception):
    pass


class AcpClosed(Exception):
    pass


OnUpdate = Callable[[dict[str, Any]], None]
OnPermission = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]
OnExit = Callable[["AcpProcess", int | None], None]


@dataclass
class AcpProcess:
    """One child process + JSON-RPC framing/association."""

    command: list[str]
    cwd: str
    env: dict[str, str]
    stderr_path: Path
    on_update: OnUpdate
    on_permission: OnPermission
    on_exit: OnExit

    _proc: asyncio.subprocess.Process | None = field(default=None, init=False, repr=False)
    _next_id: int = field(default=0, init=False)
    _pending: dict[int, asyncio.Future[dict[str, Any]]] = field(default_factory=dict, init=False, repr=False)
    _write_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)
    _tasks: list[asyncio.Task[Any]] = field(default_factory=list, init=False, repr=False)
    _stderr_fh: Any = field(default=None, init=False, repr=False)
    _closing: bool = field(default=False, init=False)
    pid: int | None = field(default=None, init=False)
    alive: bool = field(default=False, init=False)

    async def start(self, initialize_timeout: float = 90.0) -> dict[str, Any]:
        self.stderr_path.parent.mkdir(parents=True, exist_ok=True)
        self._stderr_fh = self.stderr_path.open("w", encoding="utf-8")
        self._proc = await asyncio.create_subprocess_exec(
            *self.command,
            cwd=self.cwd,
            env=self.env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=4 * 1024 * 1024,
        )
        self.pid = self._proc.pid
        self.alive = True
        self._tasks = [
            asyncio.create_task(self._read_stdout(), name="acp-stdout"),
            asyncio.create_task(self._read_stderr(), name="acp-stderr"),
            asyncio.create_task(self._wait_exit(), name="acp-wait"),
        ]
        return await self.request(
            "initialize",
            {"protocolVersion": 1, "clientCapabilities": {}},
            timeout=initialize_timeout,
        )

    # ---- transport -------------------------------------------------------

    async def request(
        self, method: str, params: dict[str, Any] | None = None, timeout: float = 120.0
    ) -> dict[str, Any]:
        if not self.alive or self._proc is None:
            raise AcpClosed("ACP process is not running")
        self._next_id += 1
        request_id = self._next_id
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        message: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        try:
            await self._write(message)
            response = await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            raise AcpTimeout(f"{method} timed out after {timeout}s") from None
        finally:
            self._pending.pop(request_id, None)
        if "error" in response:
            raise AcpError(method, response["error"])
        result = response.get("result")
        return result if isinstance(result, dict) else {}

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        await self._write(message)

    async def _write(self, message: dict[str, Any]) -> None:
        if self._proc is None or self._proc.stdin is None:
            raise AcpClosed("ACP stdin is closed")
        data = json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n"
        try:
            async with self._write_lock:
                self._proc.stdin.write(data.encode("utf-8"))
                await self._proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, RuntimeError) as exc:
            raise AcpClosed(f"ACP stdin closed: {exc}") from exc

    async def _read_stdout(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        while True:
            line = await self._proc.stdout.readline()
            if not line:
                break
            text = line.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            try:
                message = json.loads(text)
            except json.JSONDecodeError:
                logger.warning("non-JSON ACP stdout line", extra={"event": "acp_bad_frame"})
                continue
            if not isinstance(message, dict):
                continue
            if "method" in message:
                if "id" in message:
                    self._tasks.append(
                        asyncio.create_task(self._answer_server_request(message), name="acp-server-request")
                    )
                else:
                    try:
                        self.on_update(message.get("params") or {})
                    except Exception:  # noqa: BLE001 - never kill the reader
                        logger.exception("ACP update handler failed", extra={"event": "acp_update_error"})
                continue
            pending = self._pending.get(message.get("id"))
            if pending is not None and not pending.done():
                pending.set_result(message)
        self.alive = False
        for future in self._pending.values():
            if not future.done():
                future.set_exception(AcpClosed("ACP process closed stdout"))
        self._pending.clear()

    async def _answer_server_request(self, message: dict[str, Any]) -> None:
        method = message.get("method", "")
        params = message.get("params") or {}
        try:
            if method == "session/request_permission":
                result = await self.on_permission(params)
            else:
                result = None
                raise LookupError(method)
            await self._write({"jsonrpc": "2.0", "id": message["id"], "result": result})
        except LookupError:
            try:
                await self._write(
                    {
                        "jsonrpc": "2.0",
                        "id": message["id"],
                        "error": {"code": ERR_METHOD_NOT_FOUND, "message": f"client does not implement {method}"},
                    }
                )
            except AcpClosed:
                pass
        except Exception:  # noqa: BLE001 - answer cancelled rather than hanging the agent
            logger.exception("ACP permission handler failed", extra={"event": "acp_permission_error"})
            try:
                await self._write(
                    {
                        "jsonrpc": "2.0",
                        "id": message["id"],
                        "result": {"outcome": {"outcome": "cancelled"}},
                    }
                )
            except AcpClosed:
                pass

    async def _read_stderr(self) -> None:
        assert self._proc is not None and self._proc.stderr is not None
        while True:
            line = await self._proc.stderr.readline()
            if not line:
                break
            if self._stderr_fh is not None:
                self._stderr_fh.write(line.decode("utf-8", errors="replace"))
                self._stderr_fh.flush()

    async def _wait_exit(self) -> None:
        assert self._proc is not None
        returncode = await self._proc.wait()
        self.alive = False
        self._close_stderr()
        if not self._closing:
            try:
                self.on_exit(self, returncode)
            except Exception:  # noqa: BLE001
                logger.exception("ACP exit handler failed", extra={"event": "acp_exit_error"})

    def _close_stderr(self) -> None:
        if self._stderr_fh is not None:
            try:
                self._stderr_fh.close()
            finally:
                self._stderr_fh = None

    async def close(self, timeout: float = 15.0) -> int | None:
        self._closing = True
        self.alive = False
        proc = self._proc
        if proc is None:
            return None
        returncode = proc.returncode
        if returncode is None:
            try:
                if proc.stdin is not None:
                    proc.stdin.close()
            except Exception:  # noqa: BLE001 - best effort EOF
                pass
            try:
                returncode = await asyncio.wait_for(proc.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                proc.terminate()
                try:
                    returncode = await asyncio.wait_for(proc.wait(), timeout=5)
                except asyncio.TimeoutError:  # pragma: no cover - stubborn child
                    proc.kill()
                    returncode = await proc.wait()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        self._close_stderr()
        self._proc = None
        return returncode
