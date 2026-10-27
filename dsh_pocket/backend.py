"""Backend interface shared by the mock and real (ACP) implementations.

Routes and the WebSocket layer only talk to this interface; no ACP detail is
hard-coded in the transport layer.
"""

from __future__ import annotations

from typing import Any

from .errors import ApiError


class Backend:
    """Operations required by the REST/WS layer (docs/api.md §3–§5)."""

    async def list_workspaces(self) -> list[dict[str, Any]]:  # pragma: no cover
        raise NotImplementedError

    async def list_sessions(
        self,
        workspace_id: str | None,
        cursor: str | None,
        limit: int,
        include_archived: bool,
    ) -> dict[str, Any]:  # pragma: no cover
        raise NotImplementedError

    async def create_session(
        self, workspace_id: str | None, cwd: str | None = None
    ) -> dict[str, Any]:  # pragma: no cover
        raise NotImplementedError

    async def get_session(self, session_id: str) -> dict[str, Any]:  # pragma: no cover
        raise NotImplementedError

    async def resume_session(self, session_id: str) -> dict[str, Any]:  # pragma: no cover
        raise NotImplementedError

    async def close_session(self, session_id: str) -> dict[str, Any]:  # pragma: no cover
        raise NotImplementedError

    async def get_transcript(
        self,
        session_id: str,
        before: int | None,
        limit: int,
        include_reasoning: bool,
        max_chars: int,
    ) -> dict[str, Any]:  # pragma: no cover
        raise NotImplementedError

    async def get_tool_call(self, session_id: str, call_id: str) -> dict[str, Any]:  # pragma: no cover
        raise NotImplementedError

    async def start_turn(
        self,
        session_id: str,
        text: str,
        client_message_id: str | None,
        request_id: str | None,
    ) -> dict[str, Any]:  # pragma: no cover
        raise NotImplementedError

    async def cancel_turn(self, session_id: str, request_id: str | None) -> None:  # pragma: no cover
        raise NotImplementedError

    async def set_config(self, session_id: str, config_id: str, value: str) -> list[dict[str, Any]]:
        raise NotImplementedError  # pragma: no cover

    async def respond_permission(self, request_id: str, decision: str) -> None:  # pragma: no cover
        raise NotImplementedError

    async def snapshot(self, session_id: str, before: int | None = None, limit: int = 30) -> dict[str, Any]:
        raise NotImplementedError  # pragma: no cover

    async def shutdown(self) -> None:  # pragma: no cover
        return None
