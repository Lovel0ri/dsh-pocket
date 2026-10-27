"""SQLite persistence for phone-created sessions (T7 simplified scope).

The pocket server owns this database; it never reads or writes desktop session
logs. Default location: ``$DSH_POCKET_HOME/pocket.db`` (override via Settings).
"""

from __future__ import annotations

import base64
import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from .errors import ApiError

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    cwd TEXT NOT NULL,
    title TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    last_seq INTEGER NOT NULL DEFAULT 0,
    usage_used INTEGER NOT NULL DEFAULT 0,
    usage_size INTEGER NOT NULL DEFAULT 0,
    config_options TEXT,
    route TEXT,
    archived INTEGER NOT NULL DEFAULT 0,
    pinned INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS messages (
    row_id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_session_seq ON messages(session_id, seq, row_id);
CREATE TABLE IF NOT EXISTS tool_calls (
    session_id TEXT NOT NULL,
    call_id TEXT NOT NULL,
    name TEXT,
    arguments TEXT,
    status TEXT,
    output_text TEXT,
    is_error INTEGER,
    seq INTEGER NOT NULL,
    turn_id TEXT,
    created_at INTEGER NOT NULL,
    PRIMARY KEY (session_id, call_id)
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""

EMPTY_USAGE = {
    "inputTokens": 0,
    "outputTokens": 0,
    "cacheReadTokens": 0,
    "cacheWriteTokens": 0,
    "totalTokens": 0,
}

RANK = {"user": 0, "assistant": 1, "tool": 2}


def now_ms() -> int:
    return int(time.time() * 1000)


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
    except Exception as exc:  # noqa: BLE001 - malformed cursor -> 400
        raise ApiError("E_INVALID_REQUEST", "invalid cursor", details=str(exc)) from exc


class PocketStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(SCHEMA)
        self._conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES('schema', '1')")
        self._conn.commit()

    def close(self) -> None:
        try:
            self._conn.commit()
        finally:
            self._conn.close()

    # ---- sessions --------------------------------------------------------

    def create_session(
        self,
        *,
        session_id: str,
        workspace_id: str,
        cwd: str,
        title: str,
        config_options: list[dict[str, Any]] | None,
        route: dict[str, Any] | None,
    ) -> dict[str, Any]:
        created = now_ms()
        self._conn.execute(
            "INSERT INTO sessions(session_id, workspace_id, cwd, title, created_at, updated_at,"
            " config_options, route) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                session_id,
                workspace_id,
                cwd,
                title,
                created,
                created,
                json.dumps(config_options, ensure_ascii=False) if config_options is not None else None,
                json.dumps(route, ensure_ascii=False) if route is not None else None,
            ),
        )
        self._conn.commit()
        row = self.get_session(session_id)
        assert row is not None
        return row

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
        return self._row_to_session(row) if row else None

    def _row_to_session(self, row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        for key in ("config_options", "route"):
            raw = data.get(key)
            data[key] = json.loads(raw) if raw else None
        data["archived"] = bool(data["archived"])
        data["pinned"] = bool(data["pinned"])
        return data

    def update_session(self, session_id: str, **fields: Any) -> None:
        allowed = {
            "title",
            "updated_at",
            "last_seq",
            "usage_used",
            "usage_size",
            "config_options",
            "route",
            "archived",
            "pinned",
            "cwd",
            "workspace_id",
        }
        updates = {key: value for key, value in fields.items() if key in allowed}
        if not updates:
            return
        for key in ("config_options", "route"):
            if key in updates and updates[key] is not None and not isinstance(updates[key], str):
                updates[key] = json.dumps(updates[key], ensure_ascii=False)
        assignments = ", ".join(f"{key} = ?" for key in updates)
        self._conn.execute(
            f"UPDATE sessions SET {assignments} WHERE session_id = ?",
            (*updates.values(), session_id),
        )
        self._conn.commit()

    def list_sessions(self, workspace_id: str | None, include_archived: bool) -> list[dict[str, Any]]:
        query = "SELECT * FROM sessions"
        clauses: list[str] = []
        params: list[Any] = []
        if workspace_id is not None:
            clauses.append("workspace_id = ?")
            params.append(workspace_id)
        if not include_archived:
            clauses.append("archived = 0")
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at DESC, session_id ASC"
        rows = self._conn.execute(query, params).fetchall()
        return [self._row_to_session(row) for row in rows]

    def session_counts(self) -> dict[str, int]:
        rows = self._conn.execute(
            "SELECT workspace_id, COUNT(*) AS count FROM sessions GROUP BY workspace_id"
        ).fetchall()
        return {row["workspace_id"]: int(row["count"]) for row in rows}

    def latest_session_id(self, workspace_id: str) -> str | None:
        row = self._conn.execute(
            "SELECT session_id FROM sessions WHERE workspace_id = ? ORDER BY updated_at DESC LIMIT 1",
            (workspace_id,),
        ).fetchone()
        return row["session_id"] if row else None

    def distinct_cwds(self) -> list[str]:
        rows = self._conn.execute("SELECT DISTINCT cwd FROM sessions").fetchall()
        return [row["cwd"] for row in rows]

    def count_messages(self, session_id: str) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS count FROM messages WHERE session_id = ?", (session_id,)
        ).fetchone()
        return int(row["count"]) if row else 0

    # ---- messages / turns ------------------------------------------------

    def append_message(self, session_id: str, seq: int, kind: str, payload: dict[str, Any]) -> None:
        self._conn.execute(
            "INSERT INTO messages(session_id, seq, kind, payload, created_at) VALUES (?, ?, ?, ?, ?)",
            (session_id, seq, kind, json.dumps(payload, ensure_ascii=False), now_ms()),
        )
        self._conn.commit()

    def finalize_turn(
        self,
        session_id: str,
        *,
        seq: int,
        assistant: dict[str, Any] | None,
        tools: list[dict[str, Any]],
        usage_used: int | None,
        usage_size: int | None,
    ) -> None:
        if assistant is not None:
            self._conn.execute(
                "INSERT INTO messages(session_id, seq, kind, payload, created_at) VALUES (?, ?, 'assistant', ?, ?)",
                (session_id, seq, json.dumps(assistant, ensure_ascii=False), now_ms()),
            )
        for tool in tools:
            self._conn.execute(
                "INSERT OR REPLACE INTO tool_calls(session_id, call_id, name, arguments, status,"
                " output_text, is_error, seq, turn_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    session_id,
                    tool["callId"],
                    tool.get("name"),
                    tool.get("arguments"),
                    tool.get("status"),
                    tool.get("outputText", ""),
                    1 if tool.get("isError") else 0,
                    seq,
                    tool.get("turnId"),
                    now_ms(),
                ),
            )
            payload = {key: value for key, value in tool.items() if key != "turnId"}
            self._conn.execute(
                "INSERT INTO messages(session_id, seq, kind, payload, created_at) VALUES (?, ?, 'tool', ?, ?)",
                (session_id, seq, json.dumps(payload, ensure_ascii=False), now_ms()),
            )
        updates: dict[str, Any] = {"last_seq": seq, "updated_at": now_ms()}
        if usage_used is not None:
            updates["usage_used"] = usage_used
        if usage_size is not None:
            updates["usage_size"] = usage_size
        self.update_session(session_id, **updates)

    def get_transcript(
        self,
        session_id: str,
        *,
        before: int | None,
        limit: int,
        include_reasoning: bool,
        max_chars: int,
    ) -> dict[str, Any] | None:
        session = self.get_session(session_id)
        if session is None:
            return None
        clauses = ["session_id = ?"]
        params: list[Any] = [session_id]
        if before is not None:
            clauses.append("seq < ?")
            params.append(before)
        rows = self._conn.execute(
            "SELECT row_id, seq, kind, payload FROM messages WHERE "
            + " AND ".join(clauses)
            + " ORDER BY seq ASC, row_id ASC",
            params,
        ).fetchall()
        groups: list[tuple[int, list[dict[str, Any]]]] = []
        for row in rows:
            seq = int(row["seq"])
            item = json.loads(row["payload"])
            if groups and groups[-1][0] == seq:
                groups[-1][1].append(item)
            else:
                groups.append((seq, [item]))
        selected = groups[-limit:] if limit > 0 else groups
        has_more = len(groups) > len(selected)
        next_before = selected[0][0] if selected else None
        items: list[dict[str, Any]] = []
        for _seq, group in selected:
            for item in sorted(group, key=lambda entry: RANK.get(entry.get("kind"), 3)):
                items.append(_render_item(item, include_reasoning, max_chars))
        return {
            "items": items,
            "hasMore": has_more,
            "nextBefore": next_before,
            "session": {
                "sessionId": session_id,
                "title": session["title"],
                "lastSeq": session["last_seq"],
                "usage": {"used": session["usage_used"], "size": session["usage_size"]},
            },
        }

    def get_tool_call(self, session_id: str, call_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM tool_calls WHERE session_id = ? AND call_id = ?",
            (session_id, call_id),
        ).fetchone()
        if row is None:
            return None
        return {
            "callId": row["call_id"],
            "name": row["name"],
            "status": row["status"],
            "arguments": row["arguments"],
            "outputText": row["output_text"],
            "isError": bool(row["is_error"]),
        }

    def last_preview(self, session_id: str) -> str:
        rows = self._conn.execute(
            "SELECT payload FROM messages WHERE session_id = ? ORDER BY seq DESC, row_id DESC LIMIT 20",
            (session_id,),
        ).fetchall()
        for row in rows:
            item = json.loads(row["payload"])
            text = item.get("text") or item.get("outputText") or ""
            text = " ".join(str(text).split())
            if text:
                return text[:60] + ("…" if len(text) > 60 else "")
        return ""


def _render_item(item: dict[str, Any], include_reasoning: bool, max_chars: int) -> dict[str, Any]:
    entry = json.loads(json.dumps(item))
    kind = entry.get("kind")
    if kind == "assistant":
        if "reasoning" in entry:
            if include_reasoning:
                entry["reasoning"], _ = _truncate(entry.get("reasoning", ""), max_chars)
            else:
                entry.pop("reasoning", None)
        entry["text"], _ = _truncate(entry.get("text", ""), max_chars)
    elif kind == "tool":
        entry["outputText"], truncated = _truncate(entry.get("outputText", ""), max_chars)
        if truncated:
            entry["truncated"] = True
    elif kind == "user":
        entry["text"], _ = _truncate(entry.get("text", ""), max_chars)
    return entry


def _truncate(text: Any, max_chars: int) -> tuple[str, bool]:
    if not isinstance(text, str):
        text = "" if text is None else str(text)
    if max_chars > 0 and len(text) > max_chars:
        return text[:max_chars], True
    return text, False


def make_assistant_item(
    *,
    text: str,
    reasoning: str,
    tool_call_ids: list[str],
    model: str | None,
    turn: int,
    usage: dict[str, int],
) -> dict[str, Any]:
    return {
        "kind": "assistant",
        "id": __import__("uuid").uuid4().hex,
        "seq": 0,  # filled by the caller
        "time": now_ms(),
        "turn": turn,
        "step": 1,
        "text": text,
        "reasoning": reasoning,
        "toolCallIds": tool_call_ids,
        "model": model,
        "usage": usage,
    }


def make_user_item(text: str) -> dict[str, Any]:
    return {
        "kind": "user",
        "id": __import__("uuid").uuid4().hex,
        "seq": 0,
        "time": now_ms(),
        "text": text,
        "source": {"kind": "user"},
    }


def make_tool_item(
    *,
    call_id: str,
    name: str | None,
    arguments: str | None,
    status: str,
    output_text: str,
    is_error: bool,
    turn: int,
    turn_id: str,
) -> dict[str, Any]:
    return {
        "kind": "tool",
        "callId": call_id,
        "name": name,
        "status": status,
        "isError": is_error,
        "seq": 0,
        "time": now_ms(),
        "turn": turn,
        "step": 1,
        "arguments": arguments,
        "outputText": output_text,
        "turnId": turn_id,
    }


def usage_dict(used: int, size: int) -> dict[str, int]:
    data = dict(EMPTY_USAGE)
    data["totalTokens"] = used
    return data
