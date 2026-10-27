"""In-memory mock backend used for frontend development and T2 tests.

Contract references: docs/api.md §5. Every behaviour here is a stand-in for
T3's ACP bridge + history-log reader; routes/WS never see mock specifics.
"""

from __future__ import annotations

import asyncio
import base64
import copy
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import __version__
from .backend import Backend
from .config import Settings
from .errors import ApiError
from .hub import EventHub

logger = logging.getLogger("dsh_pocket.mock")

MOCK_PROVIDER = "mock"
MOCK_MODELS: tuple[tuple[str, str], ...] = (("mock-fast", "Mock Fast"), ("mock-smart", "Mock Smart"))
REASONING_VALUES: tuple[tuple[str, str], ...] = (
    ("off", "Off"),
    ("low", "Low"),
    ("high", "High"),
    ("max", "Max"),
)
PERMISSION_OPTIONS: tuple[dict[str, str], ...] = (
    {"optionId": "allow-once", "name": "Allow once", "kind": "allow_once"},
    {"optionId": "reject-once", "name": "Reject", "kind": "reject_once"},
)
EMPTY_USAGE = {
    "inputTokens": 0,
    "outputTokens": 0,
    "cacheReadTokens": 0,
    "cacheWriteTokens": 0,
    "totalTokens": 0,
}


def now_ms() -> int:
    return int(time.time() * 1000)


def iso_from_ms(milliseconds: int) -> str:
    return (
        datetime.fromtimestamp(milliseconds / 1000, tz=timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def model_value(model: str) -> str:
    return json.dumps([MOCK_PROVIDER, model], separators=(",", ":"))


def mock_config_options(model: str = "mock-fast", reasoning: str = "low") -> list[dict[str, Any]]:
    return [
        {
            "id": "model",
            "name": "Model",
            "category": "model",
            "type": "select",
            "currentValue": model_value(model),
            "options": [
                {
                    "group": MOCK_PROVIDER,
                    "name": "Mock Provider",
                    "options": [
                        {"value": model_value(name), "name": label} for name, label in MOCK_MODELS
                    ],
                }
            ],
        },
        {
            "id": "reasoning_effort",
            "name": "Reasoning effort",
            "category": "thought_level",
            "type": "select",
            "currentValue": reasoning,
            "options": [{"value": value, "name": label} for value, label in REASONING_VALUES],
        },
    ]


def _option_values(options: list[dict[str, Any]]) -> list[str]:
    values: list[str] = []
    for option in options:
        if "options" in option and isinstance(option["options"], list):
            values.extend(_option_values(option["options"]))
        elif isinstance(option.get("value"), str):
            values.append(option["value"])
    return values


def truncate_text(text: str, max_chars: int) -> tuple[str, bool]:
    if max_chars > 0 and len(text) > max_chars:
        return text[:max_chars], True
    return text, False


def encode_cursor(created_at: int, session_id: str) -> str:
    raw = json.dumps([created_at, session_id], separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(cursor: str) -> tuple[int, str]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
        if (
            not isinstance(data, list)
            or len(data) != 2
            or not isinstance(data[0], int)
            or not isinstance(data[1], str)
            or encode_cursor(data[0], data[1]) != cursor
        ):
            raise ValueError("bad cursor payload")
        return data[0], data[1]
    except Exception as exc:  # noqa: BLE001 - any malformed cursor is a 400
        raise ApiError("E_INVALID_REQUEST", "invalid cursor", details=str(exc)) from exc


class _TurnCancelled(Exception):
    """Raised internally when a mock turn is cancelled between emits."""


@dataclass
class MockWorkspace:
    id: str
    path: str
    title: str

    def public(self, sessions: list["MockSession"]) -> dict[str, Any]:
        exists = os.path.isdir(self.path)
        writable = bool(exists and os.access(self.path, os.W_OK))
        if exists:
            try:
                mtime_ms = int(os.stat(self.path).st_mtime * 1000)
            except OSError:  # pragma: no cover - race
                mtime_ms = now_ms()
        else:
            mtime_ms = now_ms()
        own = [session for session in sessions if session.workspace_id == self.id]
        active = [session for session in own if not session.archived]
        default_session = max(active, key=lambda s: s.updated_at) if active else None
        return {
            "id": self.id,
            "path": self.path,
            "title": self.title,
            "exists": exists,
            "writable": writable,
            "sessionCount": len(own),
            "updatedAt": iso_from_ms(mtime_ms),
            "defaultSessionId": default_session.id if default_session else None,
        }


@dataclass
class MockSession:
    id: str
    workspace_id: str
    cwd: str
    title: str
    created_at: int
    updated_at: int
    archived: bool = False
    pinned: bool = False
    state: str = "detached"
    config_options: list[dict[str, Any]] | None = None
    route: dict[str, Any] = field(
        default_factory=lambda: {"provider": MOCK_PROVIDER, "model": "mock-fast", "reasoningEffort": "low"}
    )
    usage: dict[str, int] = field(default_factory=lambda: {"used": 0, "size": 1_000_000})
    items: list[dict[str, Any]] = field(default_factory=list)
    tool_calls: dict[str, dict[str, Any]] = field(default_factory=dict)
    pending_permissions: dict[str, asyncio.Future[str]] = field(default_factory=dict)
    permission_meta: dict[str, dict[str, str]] = field(default_factory=dict)
    in_flight: dict[str, Any] | None = None
    client_messages: dict[str, str] = field(default_factory=dict)
    turn_counter: int = 0
    last_seq: int = 0

    # ---- projection helpers ---------------------------------------------

    def summary(self) -> dict[str, Any]:
        return {
            "sessionId": self.id,
            "workspaceId": self.workspace_id,
            "cwd": self.cwd,
            "title": self.title,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
            "archived": self.archived,
            "pinned": self.pinned,
            "messageCount": len(self.items),
            "lastSeq": self.last_seq,
            "lastMessagePreview": self._preview(),
            "model": dict(self.route) if self.route else None,
            "origin": "root",
            "resumable": True,
            "activeLocally": self.state != "detached",
            "lockedElsewhere": False,
            "unread": False,
        }

    def detail(self, version: str = __version__) -> dict[str, Any]:
        return {
            "summary": self.summary(),
            "configOptions": copy.deepcopy(self.config_options) if self.state != "detached" else None,
            "route": copy.deepcopy(self.route),
            "state": self.state,
            "lockState": "free",
            "providerAvailable": True,
            "usage": dict(self.usage),
            "capabilities": {"image": False, "list": True, "resume": True, "close": True},
            "agentInfo": {"name": "dsh-pocket-mock", "version": version},
        }

    def _preview(self) -> str:
        for item in reversed(self.items):
            text = item.get("text") or ""
            if item.get("kind") == "tool":
                text = item.get("outputText") or ""
            text = " ".join(text.split())
            if text:
                return text[:60] + ("…" if len(text) > 60 else "")
        return ""

    # ---- transcript mutators --------------------------------------------

    def append_user(self, text: str, seq: int) -> dict[str, Any]:
        item = {
            "kind": "user",
            "id": str(uuid.uuid4()),
            "seq": seq,
            "time": now_ms(),
            "text": text,
            "source": {"kind": "user"},
        }
        self.items.append(item)
        return item

    def append_assistant(
        self,
        *,
        seq: int,
        text: str,
        reasoning: str,
        tool_call_ids: list[str],
        turn: int,
        step: int,
    ) -> dict[str, Any]:
        item = {
            "kind": "assistant",
            "id": str(uuid.uuid4()),
            "seq": seq,
            "time": now_ms(),
            "turn": turn,
            "step": step,
            "text": text,
            "reasoning": reasoning,
            "toolCallIds": tool_call_ids,
            "model": self.route.get("model"),
            "usage": dict(EMPTY_USAGE),
        }
        self.items.append(item)
        return item

    def append_tool(
        self,
        *,
        call_id: str,
        seq: int,
        status: str,
        arguments: str,
        output_text: str,
        turn: int,
        step: int,
    ) -> dict[str, Any]:
        item = {
            "kind": "tool",
            "callId": call_id,
            "name": "bash",
            "status": status,
            "isError": status == "failed",
            "seq": seq,
            "time": now_ms(),
            "turn": turn,
            "step": step,
            "arguments": arguments,
            "outputText": output_text,
        }
        self.items.append(item)
        self.last_seq = max(self.last_seq, seq)
        return item


class MockBackend(Backend):
    """Deterministic in-memory backend (contract §5)."""

    def __init__(self, settings: Settings, hub: EventHub) -> None:
        self.settings = settings
        self.hub = hub
        self._workspaces: dict[str, MockWorkspace] = {}
        self._sessions: dict[str, MockSession] = {}
        self._closed = False
        if not settings.mock_workspaces:
            # Default mock setup: keep the demo workspace usable for a quick start.
            demo = settings.resolved_state_dir() / "demo-workspace"
            try:
                demo.mkdir(parents=True, exist_ok=True)
            except OSError:  # pragma: no cover - read-only state dir
                pass
        for raw_path in settings.resolved_mock_workspace_paths():
            path = Path(raw_path).expanduser()
            workspace_id = str(uuid.uuid5(uuid.NAMESPACE_URL, str(path)))
            self._workspaces[workspace_id] = MockWorkspace(
                id=workspace_id,
                path=str(path.resolve()),
                title=path.name or str(path),
            )
        self._seed_demo_sessions()

    # ---- seeding ---------------------------------------------------------

    def _seed_demo_sessions(self) -> None:
        workspace_ids = list(self._workspaces)
        for index, workspace_id in enumerate(workspace_ids):
            workspace = self._workspaces[workspace_id]
            self._seed_long_session(workspace)
            self._seed_short_session(workspace, archived=index == len(workspace_ids) - 1)

    def _seed_long_session(self, workspace: MockWorkspace) -> None:
        created = now_ms() - 3 * 3600_000
        session = MockSession(
            id=f"session-{uuid.uuid4()}",
            workspace_id=workspace.id,
            cwd=workspace.path,
            title="安装 dsh-our-free-model 仓库",
            created_at=created,
            updated_at=created + 6_000,
            pinned=True,
        )
        session.append_user("帮我看看这个项目", seq=1)
        session.append_assistant(
            seq=2,
            text="先看下目录结构。",
            reasoning="用户想了解项目组成，先用 bash 列出目录。",
            tool_call_ids=["call_a"],
            turn=1,
            step=1,
        )
        arguments_a = json.dumps(
            {"command": "pwd && ls -la | head -40", "description": "Show working directory and contents"},
            ensure_ascii=False,
        )
        session.append_tool(
            call_id="call_a",
            seq=3,
            status="completed",
            arguments=arguments_a,
            output_text="/Volumes/extra1tb/clawdbot-project\ntotal 72\ndrwxr-xr-x  10 bing staff  320 Oct 26 20:05 .\n…",
            turn=1,
            step=1,
        )
        session.tool_calls["call_a"] = {
            "name": "bash",
            "status": "completed",
            "arguments": arguments_a,
            "outputText": "/Volumes/extra1tb/clawdbot-project\ntotal 72\ndrwxr-xr-x  10 bing staff  320 Oct 26 20:05 .\n…",
        }
        session.append_user("再读一下 README", seq=4)
        session.append_assistant(
            seq=5,
            text="README 已读，摘要如下：项目提供移动端访问本机 DSH 的 Web 客户端。",
            reasoning="用户要求读 README，用 bash 输出前 40 行。",
            tool_call_ids=["call_b"],
            turn=2,
            step=1,
        )
        arguments_b = json.dumps({"command": "sed -n '1,40p' README.md"}, ensure_ascii=False)
        session.tool_calls["call_b"] = {
            "name": "bash",
            "status": "completed",
            "arguments": arguments_b,
            "outputText": "# dsh-pocket\n\nMobile web client for a local DeepSeek Harness (ACP).\n…",
        }
        session.append_tool(
            call_id="call_b",
            seq=5,
            status="completed",
            arguments=arguments_b,
            output_text="# dsh-pocket\n\nMobile web client for a local DeepSeek Harness (ACP).\n…",
            turn=2,
            step=1,
        )
        session.append_assistant(
            seq=6,
            text="总结：这是一个用于移动端访问本机 DSH 的项目，支持会话、历史与权限确认。",
            reasoning="",
            tool_call_ids=[],
            turn=2,
            step=2,
        )
        session.last_seq = 6
        self._sessions[session.id] = session

    def _seed_short_session(self, workspace: MockWorkspace, *, archived: bool) -> None:
        created = now_ms() - 3600_000
        session = MockSession(
            id=f"session-{uuid.uuid4()}",
            workspace_id=workspace.id,
            cwd=workspace.path,
            title="移动端联调会话",
            created_at=created,
            updated_at=created + 2_000,
            archived=archived,
        )
        session.append_user("生成一个 mock 会话", seq=1)
        session.append_assistant(
            seq=2,
            text="好的，mock 会话已创建。",
            reasoning="这是一条 mock 助手消息。",
            tool_call_ids=[],
            turn=1,
            step=1,
        )
        session.last_seq = 2
        self._sessions[session.id] = session

    # ---- lookups ---------------------------------------------------------

    def _require_session(self, session_id: str) -> MockSession:
        session = self._sessions.get(session_id)
        if session is None:
            raise ApiError("E_NOT_FOUND", f"unknown session: {session_id}")
        return session

    def _require_workspace(self, workspace_id: str) -> MockWorkspace:
        workspace = self._workspaces.get(workspace_id)
        if workspace is None:
            raise ApiError("E_WORKSPACE_UNKNOWN", f"unknown workspace: {workspace_id}")
        return workspace

    def _publish(self, session: MockSession, event: dict[str, Any]) -> None:
        self.hub.publish(session.id, event)

    def _publish_state(self, session: MockSession) -> None:
        self._publish(
            session,
            {"type": "session.state", "sessionId": session.id, "state": session.state, "lockState": "free"},
        )

    # ---- workspace/session queries --------------------------------------

    async def list_workspaces(self) -> list[dict[str, Any]]:
        sessions = list(self._sessions.values())
        return [
            workspace.public(sessions)
            for workspace in sorted(self._workspaces.values(), key=lambda ws: (ws.title, ws.id))
        ]

    async def list_sessions(
        self,
        workspace_id: str | None,
        cursor: str | None,
        limit: int,
        include_archived: bool,
    ) -> dict[str, Any]:
        sessions = list(self._sessions.values())
        if workspace_id:
            sessions = [session for session in sessions if session.workspace_id == workspace_id]
        if not include_archived:
            sessions = [session for session in sessions if not session.archived]
        sessions.sort(key=lambda session: (-session.created_at, session.id.encode("utf-8")))
        if cursor:
            created_at, session_id = decode_cursor(cursor)
            cursor_key = (created_at, session_id.encode("utf-8"))
            sessions = [
                session
                for session in sessions
                if (session.created_at < cursor_key[0])
                or (session.created_at == cursor_key[0] and session.id.encode("utf-8") > cursor_key[1])
            ]
        page = sessions[:limit]
        response: dict[str, Any] = {"sessions": [session.summary() for session in page]}
        if len(sessions) > len(page):
            response["nextCursor"] = encode_cursor(page[-1].created_at, page[-1].id)
        return response

    async def create_session(
        self, workspace_id: str | None, cwd: str | None = None
    ) -> dict[str, Any]:
        if workspace_id is None:
            if not cwd:
                raise ApiError("E_INVALID_REQUEST", "workspaceId or cwd is required")
            wanted = str(Path(cwd).expanduser().resolve())
            workspace = next(
                (entry for entry in self._workspaces.values() if str(Path(entry.path).resolve()) == wanted),
                None,
            )
            if workspace is None:
                if not Path(wanted).is_dir():
                    raise ApiError("E_WORKSPACE_MISSING", f"workspace directory is missing: {wanted}")
                workspace = MockWorkspace(
                    id=str(uuid.uuid5(uuid.NAMESPACE_URL, wanted)),
                    path=wanted,
                    title=Path(wanted).name or wanted,
                )
                self._workspaces[workspace.id] = workspace
            workspace_id = workspace.id
        workspace = self._require_workspace(workspace_id)
        if not os.path.isdir(workspace.path):
            raise ApiError(
                "E_WORKSPACE_MISSING",
                f"workspace directory is missing: {workspace.path}",
                suggested_action="refresh-workspaces",
            )
        if not os.access(workspace.path, os.W_OK):
            raise ApiError(
                "E_WORKSPACE_MISSING",
                f"workspace directory is not writable: {workspace.path}",
                suggested_action="refresh-workspaces",
            )
        created = now_ms()
        session = MockSession(
            id=f"session-{uuid.uuid4()}",
            workspace_id=workspace.id,
            cwd=workspace.path,
            title="新会话",
            created_at=created,
            updated_at=created,
            state="idle",
            config_options=mock_config_options(),
        )
        self._sessions[session.id] = session
        self._publish_state(session)
        return session.detail(__version__)

    async def get_session(self, session_id: str) -> dict[str, Any]:
        return self._require_session(session_id).detail(__version__)

    async def resume_session(self, session_id: str) -> dict[str, Any]:
        session = self._require_session(session_id)
        if session.state != "detached":
            raise ApiError("E_ALREADY_ACTIVE", f"session is already active: {session_id}")
        session.state = "idle"
        session.config_options = copy.deepcopy(session.config_options or mock_config_options())
        session.updated_at = now_ms()
        self._publish_state(session)
        return {
            "configOptions": copy.deepcopy(session.config_options),
            "state": "idle",
            "lockState": "free",
            "providerAvailable": True,
        }

    async def close_session(self, session_id: str) -> dict[str, Any]:
        session = self._require_session(session_id)
        if session.state == "detached":
            return {"ok": True}
        if session.in_flight:
            await self.cancel_turn(session_id, None)
        session.state = "detached"
        session.config_options = None
        session.updated_at = now_ms()
        self._publish_state(session)
        return {"ok": True}

    # ---- history ---------------------------------------------------------

    def _page(
        self,
        session: MockSession,
        before: int | None,
        limit: int,
        include_reasoning: bool,
        max_chars: int,
    ) -> dict[str, Any]:
        items = session.items
        if before is not None:
            items = [item for item in items if item["seq"] < before]
        groups: list[tuple[int, list[dict[str, Any]]]] = []
        for item in items:
            seq = item["seq"]
            if not groups or groups[-1][0] != seq:
                groups.append((seq, [item]))
            else:
                groups[-1][1].append(item)
        page_groups = groups[-limit:]
        page_items = [item for _seq, group in page_groups for item in group]
        next_before = page_groups[0][0] if page_groups else None
        has_more = len(groups) > len(page_groups)
        rendered: list[dict[str, Any]] = []
        for item in page_items:
            entry = copy.deepcopy(item)
            if entry.get("kind") == "assistant":
                if not include_reasoning:
                    entry.pop("reasoning", None)
                elif isinstance(entry.get("reasoning"), str):
                    entry["reasoning"], _ = truncate_text(entry["reasoning"], max_chars)
                if isinstance(entry.get("text"), str):
                    entry["text"], _ = truncate_text(entry["text"], max_chars)
            elif entry.get("kind") == "tool" and isinstance(entry.get("outputText"), str):
                entry["outputText"], truncated = truncate_text(entry["outputText"], max_chars)
                if truncated:
                    entry["truncated"] = True
            rendered.append(entry)
        return {
            "items": rendered,
            "hasMore": has_more,
            "nextBefore": next_before,
            "session": {
                "sessionId": session.id,
                "title": session.title,
                "lastSeq": session.last_seq,
                "usage": dict(session.usage),
            },
        }

    async def get_transcript(
        self,
        session_id: str,
        before: int | None,
        limit: int,
        include_reasoning: bool,
        max_chars: int,
    ) -> dict[str, Any]:
        session = self._require_session(session_id)
        return self._page(session, before, limit, include_reasoning, max_chars)

    async def snapshot(self, session_id: str, before: int | None = None, limit: int = 30) -> dict[str, Any]:
        return await self.get_transcript(
            session_id,
            before,
            limit,
            include_reasoning=True,
            max_chars=self.settings.max_chars,
        )

    async def get_tool_call(self, session_id: str, call_id: str) -> dict[str, Any]:
        session = self._require_session(session_id)
        record = session.tool_calls.get(call_id)
        if record is None:
            raise ApiError("E_NOT_FOUND", f"unknown tool call: {call_id}")
        return {
            "callId": call_id,
            "name": record["name"],
            "status": record["status"],
            "arguments": record["arguments"],
            "outputText": record["outputText"],
        }

    # ---- turns -----------------------------------------------------------

    async def start_turn(
        self,
        session_id: str,
        text: str,
        client_message_id: str | None,
        request_id: str | None,
    ) -> dict[str, Any]:
        session = self._require_session(session_id)
        text = text.strip()
        if not text:
            raise ApiError("E_INVALID_REQUEST", "prompt text must not be empty")
        if client_message_id and client_message_id in session.client_messages:
            return {
                "requestId": session.client_messages[client_message_id],
                "clientMessageId": client_message_id,
            }
        if session.in_flight is not None:
            raise ApiError(
                "E_PROMPT_IN_FLIGHT",
                "a prompt is already in flight for this session",
                suggested_action="wait-or-cancel",
            )
        if session.state == "detached":
            session.state = "idle"
            session.config_options = copy.deepcopy(session.config_options or mock_config_options())
        turn_id = request_id or f"turn_{uuid.uuid4().hex[:12]}"
        session.turn_counter += 1
        turn_number = session.turn_counter
        cancel_event = asyncio.Event()
        session.state = "streaming"
        self._publish_state(session)
        task = asyncio.create_task(
            self._run_turn(session, turn_id, turn_number, text, cancel_event),
            name=f"mock-turn-{turn_id}",
        )
        session.in_flight = {"requestId": turn_id, "task": task, "cancelEvent": cancel_event}
        if client_message_id:
            session.client_messages[client_message_id] = turn_id
        return {"requestId": turn_id, "clientMessageId": client_message_id}

    async def cancel_turn(self, session_id: str, request_id: str | None) -> None:
        session = self._require_session(session_id)
        in_flight = session.in_flight
        if in_flight is None:
            return
        if request_id and request_id != in_flight["requestId"]:
            return
        session.state = "cancelling"
        self._publish_state(session)
        in_flight["cancelEvent"].set()
        for future in list(session.pending_permissions.values()):
            if not future.done():
                future.set_result("cancelled")

    async def respond_permission(self, request_id: str, decision: str) -> None:
        for session in self._sessions.values():
            future = session.pending_permissions.get(request_id)
            if future is None:
                continue
            if future.done():
                break
            future.set_result("cancelled" if decision == "cancel" else decision)
            return
        raise ApiError("E_NOT_FOUND", f"unknown permission request: {request_id}")

    async def set_config(self, session_id: str, config_id: str, value: str) -> list[dict[str, Any]]:
        session = self._require_session(session_id)
        if session.state == "detached" or session.config_options is None:
            raise ApiError("E_ACP_INVALID", f"unknown session: {session_id}")
        config = next((entry for entry in session.config_options if entry.get("id") == config_id), None)
        if config is None:
            raise ApiError("E_ACP_INVALID", f"unknown session config option: {config_id}")
        allowed = _option_values(config.get("options", []))
        if config_id == "reasoning_effort" and value == "":
            allowed = allowed + [""]
        if value not in allowed:
            if config_id == "reasoning_effort":
                raise ApiError(
                    "E_ACP_INVALID",
                    f"unknown reasoning effort for {session.route.get('provider')}/{session.route.get('model')}: {value}",
                )
            raise ApiError("E_ACP_INVALID", f"unknown model option: {value}")
        config["currentValue"] = value
        if config_id == "model":
            try:
                provider, model = json.loads(value)
                session.route["provider"] = provider
                session.route["model"] = model
            except (ValueError, TypeError):  # pragma: no cover - mock values are JSON
                session.route["model"] = value
        elif config_id == "reasoning_effort":
            session.route["reasoningEffort"] = value
        session.updated_at = now_ms()
        options = copy.deepcopy(session.config_options)
        self._publish(session, {"type": "config.update", "sessionId": session.id, "configOptions": options})
        return options

    async def _run_turn(
        self,
        session: MockSession,
        turn_id: str,
        turn_number: int,
        text: str,
        cancel_event: asyncio.Event,
    ) -> None:
        stop_reason = "end_turn"
        thought_message_id = f"msg_{uuid.uuid4().hex[:10]}"
        answer_message_id = f"msg_{uuid.uuid4().hex[:10]}"
        call_id = f"call_{uuid.uuid4().hex[:12]}"
        raw_input = {
            "command": "printf 'hello from dsh-pocket mock\\n'",
            "description": "Mock tool call (not executed)",
        }
        arguments = json.dumps(raw_input, ensure_ascii=False)
        tool_status = "failed"
        tool_output = ""
        answer_chunks: list[str] = []
        reasoning = "用户发来了一条消息；先确认上下文，再决定是否调用工具。"
        tool_started = False
        answer_started = False
        permission_id: str | None = None

        session.tool_calls[call_id] = {
            "name": "bash",
            "status": "running",
            "arguments": arguments,
            "outputText": "",
        }

        try:
            for chunk in ("用户发来了一条消息。", "先确认工作区上下文，再决定是否调用工具。"):
                await self._sleep_or_cancel(cancel_event, 0.01)
                self._publish(
                    session,
                    {
                        "type": "assistant.thought_delta",
                        "sessionId": session.id,
                        "requestId": turn_id,
                        "messageId": thought_message_id,
                        "text": chunk,
                    },
                )

            tool_started = True
            self._publish(
                session,
                {
                    "type": "tool.started",
                    "sessionId": session.id,
                    "requestId": turn_id,
                    "toolCallId": call_id,
                    "title": "bash",
                    "rawInput": raw_input,
                    "status": "in_progress",
                },
            )

            permission_id = f"perm_{uuid.uuid4().hex[:8]}"
            loop = asyncio.get_running_loop()
            future: asyncio.Future[str] = loop.create_future()
            session.pending_permissions[permission_id] = future
            session.permission_meta[permission_id] = {"callId": call_id, "turnId": turn_id}
            session.state = "awaiting_permission"
            self._publish_state(session)
            self._publish(
                session,
                {
                    "type": "permission.request",
                    "sessionId": session.id,
                    "requestId": permission_id,
                    "toolCallId": call_id,
                    "toolName": "bash",
                    "argumentsPreview": arguments,
                    "options": [dict(option) for option in PERMISSION_OPTIONS],
                    "createdAt": now_ms(),
                    "timeoutMs": int(self.settings.permission_timeout_seconds * 1000),
                },
            )
            outcome = await self._wait_permission(session, future)

            if outcome == "allow-once":
                self._publish(
                    session,
                    {"type": "permission.resolved", "requestId": permission_id, "outcome": "allow-once"},
                )
                tool_status = "completed"
                tool_output = "hello from dsh-pocket mock\n"
            elif outcome == "reject-once":
                self._publish(
                    session,
                    {"type": "permission.resolved", "requestId": permission_id, "outcome": "reject-once"},
                )
                tool_output = "Error: the user rejected this tool call"
            elif outcome == "timeout":
                self._publish(
                    session,
                    {"type": "permission.resolved", "requestId": permission_id, "outcome": "timeout"},
                )
                tool_output = "Error: approval request timed out"
                stop_reason = "cancelled"
            else:  # cancelled
                self._publish(
                    session,
                    {"type": "permission.resolved", "requestId": permission_id, "outcome": "cancelled"},
                )
                tool_output = "Error: approval was cancelled"
                stop_reason = "cancelled"

            session.pending_permissions.pop(permission_id, None)
            session.permission_meta.pop(permission_id, None)
            session.tool_calls[call_id]["status"] = tool_status
            session.tool_calls[call_id]["outputText"] = tool_output
            self._publish(
                session,
                {
                    "type": "tool.finished",
                    "sessionId": session.id,
                    "requestId": turn_id,
                    "toolCallId": call_id,
                    "status": tool_status,
                    "output": [{"type": "text", "text": tool_output}],
                },
            )

            if stop_reason != "cancelled":
                session.state = "streaming"
                self._publish_state(session)
                echo = text if len(text) <= 200 else text[:200] + "…"
                chunks = [
                    "收到：",
                    echo,
                    "\n\n（mock）这是 dsh-pocket 的流式回复；工具调用已按你的选择处理。",
                ]
                for chunk in chunks:
                    await self._sleep_or_cancel(cancel_event, 0.01)
                    answer_chunks.append(chunk)
                    self._publish(
                        session,
                        {
                            "type": "assistant.delta",
                            "sessionId": session.id,
                            "requestId": turn_id,
                            "messageId": answer_message_id,
                            "text": chunk,
                        },
                    )
                answer_started = True
                session.usage["used"] += 40 + len(text)
                self._publish(
                    session,
                    {
                        "type": "usage",
                        "sessionId": session.id,
                        "used": session.usage["used"],
                        "size": session.usage["size"],
                    },
                )
        except _TurnCancelled:
            stop_reason = "cancelled"
        except Exception:  # noqa: BLE001 - mock must never kill the WS loop
            logger.exception("mock turn failed", extra={"event": "mock_turn_failed"})
            stop_reason = "cancelled"
            self._publish(
                session,
                {
                    "type": "turn.error",
                    "sessionId": session.id,
                    "requestId": turn_id,
                    "code": "E_INTERNAL",
                    "message": "mock turn failed",
                    "class": "internal",
                },
            )
        finally:
            if permission_id:
                if not future.done():  # type: ignore[possibly-undefined]
                    future.set_result("cancelled")  # type: ignore[possibly-undefined]
                session.pending_permissions.pop(permission_id, None)
                session.permission_meta.pop(permission_id, None)
            user_seq = session.last_seq + 1
            session.append_user(text, seq=user_seq)
            if not tool_started:
                session.tool_calls.pop(call_id, None)
            assistant_text = "".join(answer_chunks)
            if tool_started:
                if assistant_text:
                    group_seq = user_seq + 1
                    session.append_assistant(
                        seq=group_seq,
                        text=assistant_text,
                        reasoning=reasoning,
                        tool_call_ids=[call_id],
                        turn=turn_number,
                        step=1,
                    )
                    session.append_tool(
                        call_id=call_id,
                        seq=group_seq,
                        status=tool_status,
                        arguments=arguments,
                        output_text=tool_output,
                        turn=turn_number,
                        step=1,
                    )
                else:
                    session.append_tool(
                        call_id=call_id,
                        seq=user_seq + 1,
                        status=tool_status,
                        arguments=arguments,
                        output_text=tool_output,
                        turn=turn_number,
                        step=1,
                    )
            session.last_seq = max(session.last_seq, user_seq)
            if session.title == "新会话":
                first_line = text.strip().splitlines()[0] if text.strip() else ""
                session.title = first_line[:40] or "新会话"
            session.updated_at = now_ms()
            if session.state != "detached":
                session.state = "idle"
            session.in_flight = None
            self._publish(
                session,
                {"type": "turn.end", "sessionId": session.id, "requestId": turn_id, "stopReason": stop_reason},
            )
            self._publish_state(session)

    async def _wait_permission(self, session: MockSession, future: asyncio.Future[str]) -> str:
        try:
            return await asyncio.wait_for(asyncio.shield(future), timeout=self.settings.permission_timeout_seconds)
        except asyncio.TimeoutError:
            return "timeout"

    async def _sleep_or_cancel(self, cancel_event: asyncio.Event, seconds: float) -> None:
        try:
            await asyncio.wait_for(cancel_event.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            return
        raise _TurnCancelled

    async def shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
        tasks: list[asyncio.Task[Any]] = []
        for session in self._sessions.values():
            in_flight = session.in_flight
            if not in_flight:
                continue
            in_flight["cancelEvent"].set()
            for future in list(session.pending_permissions.values()):
                if not future.done():
                    future.set_result("cancelled")
            tasks.append(in_flight["task"])
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
