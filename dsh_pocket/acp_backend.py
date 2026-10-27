"""Real backend: one ACP child for the current workspace + SQLite phone history.

Simplified T7 scope: pockets owns its sessions/history (SQLite). Desktop
session logs, desktop history, flock downgrade and ACP ``session/list`` merging
are deliberately out of scope.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .acp import AcpClosed, AcpError, AcpProcess, AcpTimeout, classify_error
from .backend import Backend
from .config import Settings
from .errors import ApiError
from .hub import EventHub
from .storage import (
    PocketStore,
    decode_cursor,
    encode_cursor,
    make_assistant_item,
    make_tool_item,
    make_user_item,
    now_ms,
    usage_dict,
)
from .workspace_registry import WorkspaceRegistry

logger = logging.getLogger("dsh_pocket.real")

VALID_PERMISSION_DECISIONS = ("allow-once", "reject-once", "cancel")


def synthetic_workspace_id(cwd: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, os.path.realpath(cwd)))


def iso_from_ms(milliseconds: int) -> str:
    return (
        datetime.fromtimestamp(milliseconds / 1000, tz=timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def route_from_config(options: list[dict[str, Any]] | None) -> dict[str, Any] | None:
    if not isinstance(options, list):
        return None
    model_config = next((entry for entry in options if entry.get("id") == "model"), None)
    reasoning_config = next((entry for entry in options if entry.get("id") == "reasoning_effort"), None)
    route: dict[str, Any] = {}
    if isinstance(model_config, dict):
        value = model_config.get("currentValue")
        if isinstance(value, str) and value:
            try:
                provider, model = json.loads(value)
                route["provider"] = provider
                route["model"] = model
            except (ValueError, TypeError):
                route["model"] = value
    if isinstance(reasoning_config, dict) and isinstance(reasoning_config.get("currentValue"), str):
        route["reasoningEffort"] = reasoning_config["currentValue"]
    return route or None


def api_error_from_acp(exc: AcpError) -> ApiError:
    bucket = classify_error(exc)
    details = exc.details or exc.message
    if "RATE_LIMIT" in details or "429" in details or "rate limit" in details.lower():
        return ApiError("E_RATE_LIMITED", f"upstream rate limit: {details}", details=details, acp_code=exc.code)
    mapping = {
        "session-locked": ("E_SESSION_LOCKED", "session is in use by another process"),
        "provider-unavailable": ("E_PROVIDER_UNAVAILABLE", "provider is not mounted in this profile"),
        "not-resumable": ("E_NOT_RESUMABLE", "session is not resumable; create a new session"),
        "cwd-mismatch": ("E_CWD_MISMATCH", "session cwd does not match this workspace"),
        "already-active": ("E_ALREADY_ACTIVE", "session is already active"),
        "prompt-in-flight": ("E_PROMPT_IN_FLIGHT", "a prompt is already in flight for this session"),
        "unsupported-method": ("E_ACP_INVALID", "the ACP agent does not support this method"),
        "invalid-params": ("E_ACP_INVALID", "invalid ACP params"),
        "internal": ("E_ACP_INTERNAL", "agent internal error"),
    }
    code, message = mapping.get(bucket, ("E_ACP_INTERNAL", "agent error"))
    return ApiError(code, f"{message}: {details}" if details else message, details=details, acp_code=exc.code)


def turn_error_class(exc: BaseException) -> str:
    if isinstance(exc, AcpClosed):
        return "internal"
    if isinstance(exc, AcpTimeout):
        return "internal"
    if isinstance(exc, AcpError):
        details = (exc.details or "") + " " + (exc.message or "")
        if "RATE_LIMIT" in details or " 429" in details or "rate limit" in details.lower():
            return "rate-limit"
        if classify_error(exc) == "provider-unavailable":
            return "provider"
    return "internal"


@dataclass
class _Turn:
    turn_id: str
    seq: int
    user_text: str
    assistant_chunks: dict[str, list[str]] = field(default_factory=dict)
    thought_chunks: dict[str, list[str]] = field(default_factory=dict)
    tools: dict[str, dict[str, Any]] = field(default_factory=dict)
    tool_meta: dict[str, dict[str, Any]] = field(default_factory=dict)
    usage_used: int | None = None
    usage_size: int | None = None
    task: asyncio.Task[Any] | None = None
    done: bool = False


@dataclass
class _Active:
    session_id: str
    cwd: str
    workspace_id: str
    state: str = "idle"
    config_options: list[dict[str, Any]] | None = None
    route: dict[str, Any] | None = None
    usage_used: int = 0
    usage_size: int = 0
    turn: _Turn | None = None
    turn_count: int = 0
    pending_permissions: dict[str, asyncio.Future[str]] = field(default_factory=dict)


class AcpBackend(Backend):
    def __init__(self, settings: Settings, hub: EventHub) -> None:
        self.settings = settings
        self.hub = hub
        self.store = PocketStore(settings.resolved_db_path())
        self.registry = WorkspaceRegistry(settings.resolved_dsh_home())
        self._proc: AcpProcess | None = None
        self._proc_cwd: str | None = None
        self._active: dict[str, _Active] = {}
        self._proc_lock = asyncio.Lock()
        self._closing = False
        self._agent_info: dict[str, Any] = {"name": "deepseek-harness-acp", "version": None}

    # ---- process management ---------------------------------------------

    @property
    def acp_pid(self) -> int | None:
        return self._proc.pid if self._proc is not None else None

    @property
    def acp_cwd(self) -> str | None:
        return self._proc_cwd

    def _publish(self, session_id: str, event: dict[str, Any]) -> None:
        self.hub.publish(session_id, event)

    def _publish_state(self, active: _Active) -> None:
        self._publish(
            active.session_id,
            {"type": "session.state", "sessionId": active.session_id, "state": active.state, "lockState": "free"},
        )

    async def _ensure_proc(self, cwd: str) -> AcpProcess:
        cwd = os.path.realpath(cwd)
        async with self._proc_lock:
            if self._proc is not None and self._proc.alive and self._proc_cwd == cwd:
                return self._proc
            if self._proc is not None:
                await self._shutdown_proc()
            self._active.clear()
            self._proc_cwd = cwd
            last_error: BaseException | None = None
            command = list(self.settings.resolved_dsh_command()) + ["--profile", self.settings.profiles[0]]
            for attempt in range(3):
                stderr_path = self.settings.acp_stderr_dir() / (
                    f"{uuid.uuid5(uuid.NAMESPACE_URL, cwd).hex[:12]}.stderr.log"
                )
                env = dict(os.environ)
                env["DSH_HOME"] = str(self.settings.resolved_dsh_home())
                env.pop("DSH_SESSION", None)
                proc = AcpProcess(
                    command=command,
                    cwd=cwd,
                    env=env,
                    stderr_path=stderr_path,
                    on_update=self._on_update,
                    on_permission=self._on_permission,
                    on_exit=self._on_proc_exit,
                )
                try:
                    result = await proc.start(self.settings.acp_initialize_timeout_seconds)
                except Exception as exc:  # noqa: BLE001 - retry/translate
                    last_error = exc
                    logger.warning(
                        "ACP start failed (attempt %d)", attempt + 1, extra={"event": "acp_start_failed"}
                    )
                    await proc.close(timeout=5)
                    await asyncio.sleep(0.4 * (attempt + 1))
                    continue
                self._proc = proc
                self._proc_cwd = cwd
                if isinstance(result.get("agentInfo"), dict):
                    self._agent_info = result["agentInfo"]
                logger.info(
                    "ACP started", extra={"event": "acp_started", "pid": proc.pid, "path": cwd}
                )
                return proc
            self._proc = None
            raise ApiError(
                "E_ACP_UNAVAILABLE",
                f"failed to start dsh --profile {self.settings.profiles[0]} in {cwd}: {last_error}",
            )

    async def _shutdown_proc(self) -> None:
        proc, self._proc = self._proc, None
        self._proc_cwd = None
        self._active.clear()
        if proc is not None:
            try:
                await proc.close()
            except Exception:  # noqa: BLE001 - best effort
                logger.exception("ACP shutdown failed", extra={"event": "acp_shutdown_failed"})

    def _on_proc_exit(self, proc: AcpProcess, returncode: int | None) -> None:
        if self._proc is not proc:
            return
        self._proc = None
        self._proc_cwd = None
        logger.warning(
            "ACP exited", extra={"event": "acp_exit", "pid": proc.pid, "code": returncode}
        )
        for session_id, active in list(self._active.items()):
            active.state = "error"
            self._publish_state(active)
            self._active.pop(session_id, None)

    # ---- update / permission mapping ------------------------------------

    @staticmethod
    def _content_text(update: dict[str, Any]) -> str:
        content = update.get("content")
        if isinstance(content, dict) and isinstance(content.get("text"), str):
            return content["text"]
        return ""

    @staticmethod
    def _tool_output_text(update: dict[str, Any]) -> str:
        parts: list[str] = []
        content = update.get("content")
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                if isinstance(block.get("text"), str):
                    parts.append(block["text"])
                inner = block.get("content")
                if isinstance(inner, dict) and isinstance(inner.get("text"), str):
                    parts.append(inner["text"])
        return "\n".join(parts)

    def _on_update(self, params: dict[str, Any]) -> None:
        session_id = params.get("sessionId")
        update = params.get("update") or {}
        if not isinstance(session_id, str) or not isinstance(update, dict):
            return
        active = self._active.get(session_id)
        if active is None:
            return
        turn = active.turn
        request_id = turn.turn_id if turn else None
        kind = update.get("sessionUpdate")
        if kind == "agent_message_chunk":
            text = self._content_text(update)
            message_id = update.get("messageId") or (f"msg_{turn.turn_id}" if turn else "msg")
            if turn is not None and text:
                turn.assistant_chunks.setdefault(message_id, []).append(text)
            self._publish(
                session_id,
                {
                    "type": "assistant.delta",
                    "sessionId": session_id,
                    "requestId": request_id,
                    "messageId": message_id,
                    "text": text,
                },
            )
        elif kind == "agent_thought_chunk":
            text = self._content_text(update)
            message_id = update.get("messageId") or (f"thought_{turn.turn_id}" if turn else "thought")
            if turn is not None and text:
                turn.thought_chunks.setdefault(message_id, []).append(text)
            self._publish(
                session_id,
                {
                    "type": "assistant.thought_delta",
                    "sessionId": session_id,
                    "requestId": request_id,
                    "messageId": message_id,
                    "text": text,
                },
            )
        elif kind == "tool_call":
            call_id = update.get("toolCallId")
            if not isinstance(call_id, str):
                return
            title = update.get("title")
            raw_input = update.get("rawInput")
            if turn is not None:
                turn.tool_meta[call_id] = {"title": title, "rawInput": raw_input}
                turn.tools[call_id] = {
                    "callId": call_id,
                    "name": title,
                    "arguments": raw_input if isinstance(raw_input, str) else json.dumps(raw_input, ensure_ascii=False),
                    "status": "running",
                    "outputText": "",
                    "isError": False,
                }
            self._publish(
                session_id,
                {
                    "type": "tool.started",
                    "sessionId": session_id,
                    "requestId": request_id,
                    "toolCallId": call_id,
                    "title": title,
                    "rawInput": raw_input,
                    "status": "in_progress",
                },
            )
        elif kind == "tool_call_update":
            call_id = update.get("toolCallId")
            if not isinstance(call_id, str):
                return
            status = update.get("status") if update.get("status") in ("completed", "failed") else "completed"
            output = self._tool_output_text(update)
            if turn is not None:
                record = turn.tools.get(call_id)
                if record is None:
                    record = {
                        "callId": call_id,
                        "name": None,
                        "arguments": None,
                        "status": status,
                        "outputText": output,
                        "isError": status == "failed",
                    }
                    turn.tools[call_id] = record
                record["status"] = status
                record["outputText"] = output
                record["isError"] = status == "failed"
            self._publish(
                session_id,
                {
                    "type": "tool.finished",
                    "sessionId": session_id,
                    "requestId": request_id,
                    "toolCallId": call_id,
                    "status": status,
                    "output": [{"type": "text", "text": output[: self.settings.max_chars]}],
                },
            )
        elif kind == "usage_update":
            used = int(update.get("used") or 0)
            size = int(update.get("size") or 0)
            active.usage_used, active.usage_size = used, size
            if turn is not None:
                turn.usage_used, turn.usage_size = used, size
            self._publish(
                session_id,
                {"type": "usage", "sessionId": session_id, "used": used, "size": size},
            )
        elif kind == "config_option_update":
            options = update.get("configOptions")
            if isinstance(options, list):
                active.config_options = options
                active.route = route_from_config(options) or active.route
                self.store.update_session(
                    session_id, config_options=options, route=active.route, updated_at=now_ms()
                )
                self._publish(
                    session_id,
                    {"type": "config.update", "sessionId": session_id, "configOptions": options},
                )

    async def _on_permission(self, params: dict[str, Any]) -> dict[str, Any]:
        session_id = params.get("sessionId")
        active = self._active.get(session_id) if isinstance(session_id, str) else None
        if active is None:
            return {"outcome": {"outcome": "cancelled"}}
        tool_call = params.get("toolCall") or {}
        call_id = tool_call.get("toolCallId")
        meta = {}
        if active.turn is not None and isinstance(call_id, str):
            meta = active.turn.tool_meta.get(call_id, {})
        options = params.get("options") or []
        request_id = f"perm_{uuid.uuid4().hex[:8]}"
        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        active.pending_permissions[request_id] = future
        active.state = "awaiting_permission"
        self._publish_state(active)
        raw_input = meta.get("rawInput")
        self._publish(
            session_id,
            {
                "type": "permission.request",
                "sessionId": session_id,
                "requestId": request_id,
                "toolCallId": call_id,
                "toolName": meta.get("title") or "tool",
                "argumentsPreview": (
                    raw_input if isinstance(raw_input, str) else json.dumps(raw_input, ensure_ascii=False)
                )[:2000]
                if raw_input is not None
                else "",
                "options": options,
                "createdAt": now_ms(),
                "timeoutMs": int(self.settings.permission_timeout_seconds * 1000),
            },
        )
        try:
            decision = await asyncio.wait_for(future, timeout=self.settings.permission_timeout_seconds)
            outcome = decision if decision in ("allow-once", "reject-once") else "cancelled"
        except asyncio.TimeoutError:
            outcome = "timeout"
        finally:
            active.pending_permissions.pop(request_id, None)
        self._publish(
            session_id,
            {
                "type": "permission.resolved",
                "sessionId": session_id,
                "requestId": request_id,
                "outcome": outcome,
            },
        )
        active.state = "streaming"
        self._publish_state(active)
        if outcome in ("allow-once", "reject-once"):
            option_id = self._pick_option(options, outcome)
            if option_id:
                return {"outcome": {"outcome": "selected", "optionId": option_id}}
        return {"outcome": {"outcome": "cancelled"}}

    @staticmethod
    def _pick_option(options: list[dict[str, Any]], decision: str) -> str | None:
        wanted_kind = "allow_once" if decision == "allow-once" else "reject_once"
        for option in options:
            if isinstance(option, dict) and option.get("kind") == wanted_kind:
                return option.get("optionId")
        for option in options:
            if isinstance(option, dict) and option.get("optionId") == decision:
                return decision
        return None

    # ---- session helpers -------------------------------------------------

    async def _resume_row(self, row: dict[str, Any]) -> _Active:
        session_id = row["session_id"]
        proc = await self._ensure_proc(row["cwd"])
        try:
            result = await proc.request(
                "session/resume",
                {"sessionId": session_id, "cwd": row["cwd"], "mcpServers": []},
                timeout=self.settings.acp_request_timeout_seconds,
            )
        except AcpError as exc:
            raise api_error_from_acp(exc) from exc
        except AcpTimeout as exc:
            raise ApiError("E_TIMEOUT", f"session/resume timed out: {exc}") from exc
        except AcpClosed as exc:
            raise ApiError("E_ACP_UNAVAILABLE", f"ACP is not running: {exc}") from exc
        options = result.get("configOptions")
        if not isinstance(options, list):
            options = row.get("config_options") or []
        route = route_from_config(options) or row.get("route")
        self.store.update_session(session_id, config_options=options, route=route)
        active = _Active(
            session_id=session_id,
            cwd=row["cwd"],
            workspace_id=row["workspace_id"],
            state="idle",
            config_options=options,
            route=route,
            usage_used=int(row.get("usage_used") or 0),
            usage_size=int(row.get("usage_size") or 0),
        )
        self._active[session_id] = active
        self._publish_state(active)
        return active

    def _summary(self, row: dict[str, Any], active: _Active | None) -> dict[str, Any]:
        route = (active.route if active is not None else row.get("route")) or {}
        session_id = row["session_id"]
        model = None
        if route.get("provider") or route.get("model"):
            model = {"provider": route.get("provider"), "model": route.get("model")}
        return {
            "sessionId": session_id,
            "workspaceId": row["workspace_id"],
            "cwd": row["cwd"],
            "title": row["title"],
            "createdAt": int(row["created_at"]),
            "updatedAt": int(row["updated_at"]),
            "archived": bool(row["archived"]),
            "pinned": bool(row["pinned"]),
            "messageCount": self.store.count_messages(session_id),
            "lastSeq": int(row["last_seq"]),
            "lastMessagePreview": self.store.last_preview(session_id),
            "model": model,
            "origin": "root",
            "resumable": True,
            "activeLocally": active is not None,
            "lockedElsewhere": False,
            "unread": False,
        }

    def _detail(self, row: dict[str, Any]) -> dict[str, Any]:
        active = self._active.get(row["session_id"])
        route = (active.route if active is not None else row.get("route")) or None
        used = active.usage_used if active is not None else int(row["usage_used"])
        size = active.usage_size if active is not None else int(row["usage_size"])
        return {
            "summary": self._summary(row, active),
            "configOptions": copy.deepcopy(active.config_options) if active is not None else None,
            "route": copy.deepcopy(route) or {"provider": None, "model": None, "reasoningEffort": None},
            "state": active.state if active is not None else "detached",
            "lockState": "free",
            "providerAvailable": True,
            "usage": {"used": used, "size": size},
            "capabilities": {"image": False, "list": True, "resume": True, "close": True},
            "agentInfo": dict(self._agent_info),
        }

    async def _start_turn_impl(
        self, session_id: str, text: str, client_message_id: str | None, request_id: str | None
    ) -> dict[str, Any]:
        row = self.store.get_session(session_id)
        if row is None:
            raise ApiError("E_NOT_FOUND", f"unknown session: {session_id}")
        text = text.strip()
        if not text:
            raise ApiError("E_INVALID_REQUEST", "prompt text must not be empty")
        active = self._active.get(session_id)
        if active is not None and active.turn is not None:
            raise ApiError("E_PROMPT_IN_FLIGHT", "a prompt is already in flight for this session")
        if active is None:
            active = await self._resume_row(row)
        assert active is not None
        turn_id = request_id or f"turn_{uuid.uuid4().hex[:12]}"
        turn = _Turn(turn_id=turn_id, seq=int(row["last_seq"]) + 1, user_text=text)
        active.turn = turn
        active.turn_count += 1
        active.state = "streaming"
        self._publish_state(active)
        user_item = make_user_item(text)
        user_item["seq"] = turn.seq
        self.store.append_message(session_id, turn.seq, "user", user_item)
        updates: dict[str, Any] = {"last_seq": turn.seq, "updated_at": now_ms()}
        if row["title"] == "新会话":
            first_line = text.splitlines()[0].strip() if text.splitlines() else text
            updates["title"] = (first_line or "新会话")[:40]
        self.store.update_session(session_id, **updates)
        turn.task = asyncio.create_task(
            self._run_prompt(active, turn, text), name=f"acp-prompt-{turn_id}"
        )
        return {"requestId": turn_id, "clientMessageId": client_message_id}

    async def _run_prompt(self, active: _Active, turn: _Turn, text: str) -> None:
        stop_reason: str | None = None
        error: BaseException | None = None
        try:
            proc = self._proc
            if proc is None or not proc.alive:
                raise AcpClosed("ACP process is not running")
            result = await proc.request(
                "session/prompt",
                {"sessionId": active.session_id, "prompt": [{"type": "text", "text": text}]},
                timeout=self.settings.prompt_timeout_seconds,
            )
            stop_reason = str(result.get("stopReason") or "end_turn")
        except (AcpError, AcpTimeout, AcpClosed) as exc:
            error = exc
        finally:
            if turn.done:
                logger.debug("turn %s already finalized", turn.turn_id)
            else:
                self._finalize_turn(active, turn, stop_reason, error)

    def _finalize_turn(
        self, active: _Active, turn: _Turn, stop_reason: str | None, error: BaseException | None
    ) -> None:
        if turn.done:
            return
        turn.done = True
        self._persist_turn(active, turn)
        final_code: str | None = None
        if error is None:
            self._publish(
                active.session_id,
                {
                    "type": "turn.end",
                    "sessionId": active.session_id,
                    "requestId": turn.turn_id,
                    "stopReason": stop_reason or "end_turn",
                },
            )
        else:
            if isinstance(error, AcpError):
                api = api_error_from_acp(error)
            elif isinstance(error, AcpTimeout):
                api = ApiError("E_TIMEOUT", f"prompt timed out: {error}")
            else:
                api = ApiError("E_ACP_UNAVAILABLE", f"ACP process is not available: {error}")
            final_code = api.code
            self._publish(
                active.session_id,
                {
                    "type": "turn.error",
                    "sessionId": active.session_id,
                    "requestId": turn.turn_id,
                    "code": api.code,
                    "message": api.message,
                    "details": api.details,
                    "class": turn_error_class(error),
                },
            )
        active.turn = None
        if active.session_id in self._active:
            active.state = "error" if final_code in ("E_ACP_UNAVAILABLE", "E_ACP_INTERNAL") else "idle"
            self._publish_state(active)
        else:
            active.state = "idle"

    def _persist_turn(self, active: _Active, turn: _Turn) -> None:
        assistant_text = "\n\n".join(
            "".join(chunks).strip() for chunks in turn.assistant_chunks.values() if "".join(chunks).strip()
        )
        reasoning = "\n\n".join(
            "".join(chunks).strip() for chunks in turn.thought_chunks.values() if "".join(chunks).strip()
        )
        tool_ids = list(turn.tools.keys())
        assistant = None
        if assistant_text or reasoning:
            usage = usage_dict(
                turn.usage_used if turn.usage_used is not None else active.usage_used,
                turn.usage_size if turn.usage_size is not None else active.usage_size,
            )
            assistant = make_assistant_item(
                text=assistant_text,
                reasoning=reasoning,
                tool_call_ids=tool_ids,
                model=(active.route or {}).get("model"),
                turn=active.turn_count,
                usage=usage,
            )
            assistant["seq"] = turn.seq
        tools: list[dict[str, Any]] = []
        for call_id, record in turn.tools.items():
            status = record.get("status") or "completed"
            if status == "running":
                status = "failed"
            tools.append(
                make_tool_item(
                    call_id=call_id,
                    name=record.get("name"),
                    arguments=record.get("arguments"),
                    status=status,
                    output_text=record.get("outputText") or "",
                    is_error=bool(record.get("isError")) or status == "failed",
                    turn=active.turn_count,
                    turn_id=turn.turn_id,
                )
            )
        self.store.finalize_turn(
            active.session_id,
            seq=turn.seq,
            assistant=assistant,
            tools=tools,
            usage_used=turn.usage_used if turn.usage_used is not None else active.usage_used,
            usage_size=turn.usage_size if turn.usage_size is not None else active.usage_size,
        )

    # ---- Backend API -----------------------------------------------------

    async def list_workspaces(self) -> list[dict[str, Any]]:
        counts = self.store.session_counts()
        sessions = self.store.list_sessions(None, True)
        result: list[dict[str, Any]] = []
        known_paths: set[str] = set()
        entries = self.registry.workspaces()
        entries.sort(key=lambda entry: entry.updated_at or "", reverse=True)
        for entry in entries:
            known_paths.add(os.path.realpath(entry.path))
            result.append(
                {
                    "id": entry.id,
                    "path": entry.path,
                    "title": entry.title,
                    "exists": os.path.isdir(entry.path),
                    "writable": os.path.isdir(entry.path) and os.access(entry.path, os.W_OK),
                    "sessionCount": counts.get(entry.id, 0),
                    "updatedAt": entry.updated_at or iso_from_ms(now_ms()),
                    "defaultSessionId": self.store.latest_session_id(entry.id),
                }
            )
        for cwd in self.store.distinct_cwds():
            if os.path.realpath(cwd) in known_paths:
                continue
            workspace_id = synthetic_workspace_id(cwd)
            if any(item["id"] == workspace_id for item in result):
                continue
            own = [session for session in sessions if session["cwd"] == cwd]
            latest = max(own, key=lambda item: item["updated_at"]) if own else None
            result.append(
                {
                    "id": workspace_id,
                    "path": cwd,
                    "title": os.path.basename(cwd.rstrip("/")) or cwd,
                    "exists": os.path.isdir(cwd),
                    "writable": os.path.isdir(cwd) and os.access(cwd, os.W_OK),
                    "sessionCount": len(own),
                    "updatedAt": iso_from_ms(latest["updated_at"]) if latest else iso_from_ms(now_ms()),
                    "defaultSessionId": latest["session_id"] if latest else None,
                }
            )
        return result

    async def list_sessions(
        self,
        workspace_id: str | None,
        cursor: str | None,
        limit: int,
        include_archived: bool,
    ) -> dict[str, Any]:
        rows = self.store.list_sessions(workspace_id, include_archived)
        if cursor:
            created_at, session_id = decode_cursor(cursor)
            rows = [
                row
                for row in rows
                if (row["created_at"] < created_at)
                or (row["created_at"] == created_at and row["session_id"].encode() > session_id.encode())
            ]
        page = rows[:limit]
        result: dict[str, Any] = {
            "sessions": [self._summary(row, self._active.get(row["session_id"])) for row in page]
        }
        if len(rows) > len(page):
            result["nextCursor"] = encode_cursor(int(page[-1]["created_at"]), page[-1]["session_id"])
        return result

    async def create_session(
        self, workspace_id: str | None, cwd: str | None = None
    ) -> dict[str, Any]:
        path, resolved_workspace_id = self._resolve_workspace(workspace_id, cwd)
        if not os.path.isdir(path):
            raise ApiError(
                "E_WORKSPACE_MISSING",
                f"workspace directory is missing: {path}",
                suggested_action="refresh-workspaces",
            )
        if not os.access(path, os.W_OK):
            raise ApiError(
                "E_WORKSPACE_MISSING",
                f"workspace directory is not writable: {path}",
                suggested_action="refresh-workspaces",
            )
        proc = await self._ensure_proc(path)
        try:
            result = await proc.request(
                "session/new", {"cwd": path, "mcpServers": []},
                timeout=self.settings.acp_request_timeout_seconds,
            )
        except AcpError as exc:
            raise api_error_from_acp(exc) from exc
        except AcpTimeout as exc:
            raise ApiError("E_TIMEOUT", f"session/new timed out: {exc}") from exc
        except AcpClosed as exc:
            raise ApiError("E_ACP_UNAVAILABLE", f"ACP is not running: {exc}") from exc
        session_id = result.get("sessionId")
        if not isinstance(session_id, str) or not session_id:
            raise ApiError("E_ACP_INTERNAL", "ACP session/new returned no sessionId")
        options = result.get("configOptions")
        options = options if isinstance(options, list) else []
        route = route_from_config(options)
        row = self.store.create_session(
            session_id=session_id,
            workspace_id=resolved_workspace_id,
            cwd=path,
            title="新会话",
            config_options=options,
            route=route,
        )
        active = _Active(
            session_id=session_id,
            cwd=path,
            workspace_id=resolved_workspace_id,
            state="idle",
            config_options=options,
            route=route,
        )
        self._active[session_id] = active
        self._publish_state(active)
        return self._detail(row)

    def _resolve_workspace(self, workspace_id: str | None, cwd: str | None) -> tuple[str, str]:
        if cwd:
            raw = Path(cwd).expanduser()
            if not raw.is_absolute():
                raise ApiError("E_PATH_INVALID", "cwd must be an absolute path")
            path = str(raw.resolve())
            entry = self.registry.by_path(path)
            return path, entry.id if entry else synthetic_workspace_id(path)
        if not workspace_id:
            raise ApiError("E_INVALID_REQUEST", "workspaceId or cwd is required")
        entry = self.registry.by_id(workspace_id)
        if entry is not None:
            return entry.path, entry.id
        for existing_cwd in self.store.distinct_cwds():
            if synthetic_workspace_id(existing_cwd) == workspace_id:
                return existing_cwd, workspace_id
        raise ApiError("E_WORKSPACE_UNKNOWN", f"unknown workspace: {workspace_id}",
                       suggested_action="refresh-workspaces")

    async def get_session(self, session_id: str) -> dict[str, Any]:
        row = self.store.get_session(session_id)
        if row is None:
            raise ApiError("E_NOT_FOUND", f"unknown session: {session_id}")
        return self._detail(row)

    async def resume_session(self, session_id: str) -> dict[str, Any]:
        row = self.store.get_session(session_id)
        if row is None:
            raise ApiError("E_NOT_FOUND", f"unknown session: {session_id}")
        if session_id in self._active:
            raise ApiError("E_ALREADY_ACTIVE", f"session is already active: {session_id}")
        active = await self._resume_row(row)
        return {
            "sessionId": session_id,
            "configOptions": active.config_options or [],
            "state": "idle",
            "lockState": "free",
            "providerAvailable": True,
        }

    async def close_session(self, session_id: str) -> dict[str, Any]:
        row = self.store.get_session(session_id)
        if row is None:
            raise ApiError("E_NOT_FOUND", f"unknown session: {session_id}")
        active = self._active.pop(session_id, None)
        if active is not None and active.turn is not None:
            await self.cancel_turn(session_id, None)
        proc = self._proc
        if proc is not None and proc.alive:
            try:
                await proc.request(
                    "session/close", {"sessionId": session_id},
                    timeout=self.settings.acp_request_timeout_seconds,
                )
            except (AcpError, AcpTimeout, AcpClosed):
                pass
        if active is not None:
            active.state = "detached"
            self._publish_state(active)
        return {"ok": True}

    async def get_transcript(
        self,
        session_id: str,
        before: int | None,
        limit: int,
        include_reasoning: bool,
        max_chars: int,
    ) -> dict[str, Any]:
        page = self.store.get_transcript(
            session_id,
            before=before,
            limit=limit,
            include_reasoning=include_reasoning,
            max_chars=max_chars,
        )
        if page is None:
            raise ApiError("E_NOT_FOUND", f"unknown session: {session_id}")
        return page

    async def snapshot(self, session_id: str, before: int | None = None, limit: int = 30) -> dict[str, Any]:
        return await self.get_transcript(
            session_id, before, limit, include_reasoning=True, max_chars=self.settings.max_chars
        )

    async def get_tool_call(self, session_id: str, call_id: str) -> dict[str, Any]:
        record = self.store.get_tool_call(session_id, call_id)
        if record is not None:
            return record
        active = self._active.get(session_id)
        if active is not None and active.turn is not None:
            tool = active.turn.tools.get(call_id)
            if tool is not None:
                return {
                    "callId": call_id,
                    "name": tool.get("name"),
                    "status": tool.get("status"),
                    "arguments": tool.get("arguments"),
                    "outputText": tool.get("outputText") or "",
                    "isError": bool(tool.get("isError")),
                }
        raise ApiError("E_NOT_FOUND", f"unknown tool call: {call_id}")

    async def start_turn(
        self,
        session_id: str,
        text: str,
        client_message_id: str | None,
        request_id: str | None,
    ) -> dict[str, Any]:
        return await self._start_turn_impl(session_id, text, client_message_id, request_id)

    async def cancel_turn(self, session_id: str, request_id: str | None) -> None:
        active = self._active.get(session_id)
        if active is None or active.turn is None:
            return
        turn = active.turn
        if request_id and request_id != turn.turn_id:
            return
        active.state = "cancelling"
        self._publish_state(active)
        for future in list(active.pending_permissions.values()):
            if not future.done():
                future.set_result("cancelled")
        proc = self._proc
        if proc is not None and proc.alive:
            try:
                await proc.notify("session/cancel", {"sessionId": session_id})
            except (AcpClosed, OSError):
                pass
        task = turn.task
        if task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=self.settings.cancel_grace_seconds)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                if not turn.done:
                    turn.done = True
                    self._persist_turn(active, turn)
                    self._publish(
                        session_id,
                        {
                            "type": "turn.end",
                            "sessionId": session_id,
                            "requestId": turn.turn_id,
                            "stopReason": "cancelled",
                        },
                    )
                    active.turn = None
                    active.state = "idle"
                    self._publish_state(active)

    async def respond_permission(self, request_id: str, decision: str) -> None:
        if decision not in VALID_PERMISSION_DECISIONS:
            raise ApiError("E_INVALID_REQUEST", f"invalid permission decision: {decision}")
        for active in self._active.values():
            future = active.pending_permissions.get(request_id)
            if future is not None and not future.done():
                future.set_result("cancelled" if decision == "cancel" else decision)
                return
        raise ApiError("E_NOT_FOUND", f"unknown permission request: {request_id}")

    async def set_config(self, session_id: str, config_id: str, value: str) -> list[dict[str, Any]]:
        row = self.store.get_session(session_id)
        if row is None:
            raise ApiError("E_NOT_FOUND", f"unknown session: {session_id}")
        active = self._active.get(session_id)
        if active is None:
            active = await self._resume_row(row)
        proc = self._proc
        if proc is None or not proc.alive:
            raise ApiError("E_ACP_UNAVAILABLE", "ACP is not running")
        try:
            result = await proc.request(
                "session/set_config_option",
                {"sessionId": session_id, "configId": config_id, "value": value},
                timeout=self.settings.acp_request_timeout_seconds,
            )
        except AcpError as exc:
            raise api_error_from_acp(exc) from exc
        except AcpTimeout as exc:
            raise ApiError("E_TIMEOUT", f"set_config_option timed out: {exc}") from exc
        except AcpClosed as exc:
            raise ApiError("E_ACP_UNAVAILABLE", f"ACP is not running: {exc}") from exc
        options = result.get("configOptions")
        if not isinstance(options, list):
            options = active.config_options or []
        active.config_options = options
        active.route = route_from_config(options) or active.route
        self.store.update_session(
            session_id, config_options=options, route=active.route, updated_at=now_ms()
        )
        self._publish(
            session_id, {"type": "config.update", "sessionId": session_id, "configOptions": options}
        )
        return options

    async def shutdown(self) -> None:
        if self._closing:
            return
        self._closing = True
        proc = self._proc
        self._proc = None
        self._proc_cwd = None
        if proc is not None and proc.alive:
            for session_id, active in list(self._active.items()):
                if active.turn is not None and active.turn.task is not None:
                    try:
                        await proc.notify("session/cancel", {"sessionId": session_id})
                    except Exception:  # noqa: BLE001
                        pass
                try:
                    await proc.request("session/close", {"sessionId": session_id}, timeout=3)
                except Exception:  # noqa: BLE001
                    pass
            try:
                await proc.close()
            except Exception:  # noqa: BLE001
                logger.exception("ACP shutdown failed", extra={"event": "acp_shutdown_failed"})
        self._active.clear()
        self.store.close()

