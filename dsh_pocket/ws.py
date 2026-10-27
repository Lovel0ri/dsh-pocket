"""WebSocket transport (contract §4).

The socket only translates between the wire format and :class:`Backend`; all
agent specifics stay behind the backend interface.
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import suppress
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from . import __version__
from .errors import ApiError
from .models import (
    WsCancelMessage,
    WsConfigSetMessage,
    WsHistoryPageMessage,
    WsPermissionRespondMessage,
    WsPromptMessage,
    WsSubscribeMessage,
    WsUnsubscribeMessage,
)
from .runtime import Runtime

logger = logging.getLogger("dsh_pocket.ws")

MAX_EVENT_QUEUE = 1000
MIN_LIMIT = 1
MAX_LIMIT = 100


class _Connection:
    def __init__(self, websocket: WebSocket, runtime: Runtime) -> None:
        self.websocket = websocket
        self.runtime = runtime
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=MAX_EVENT_QUEUE)
        self.subscriptions: set[str] = set()

    def send(self, payload: dict[str, Any]) -> None:
        try:
            self.queue.put_nowait(payload)
        except asyncio.QueueFull:
            with suppress(asyncio.QueueEmpty):
                self.queue.get_nowait()
            with suppress(asyncio.QueueFull):
                self.queue.put_nowait(payload)

    def subscribe(self, session_id: str) -> None:
        if session_id in self.subscriptions:
            return
        self.runtime.hub.subscribe(session_id, self.queue)
        self.subscriptions.add(session_id)

    def unsubscribe(self, session_id: str) -> None:
        self.runtime.hub.unsubscribe(session_id, self.queue)
        self.subscriptions.discard(session_id)

    def unsubscribe_all(self) -> None:
        for session_id in list(self.subscriptions):
            self.unsubscribe(session_id)


def _error_payload(exc: ApiError, message: dict[str, Any] | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "type": "error",
        "code": exc.code,
        "message": exc.message,
        "details": exc.details,
        "retryable": exc.retryable,
    }
    if message:
        if isinstance(message.get("sessionId"), str):
            payload["sessionId"] = message["sessionId"]
        if isinstance(message.get("requestId"), str):
            payload["requestId"] = message["requestId"]
    return payload


def _validate(cls: type, message: dict[str, Any]) -> Any:
    try:
        return cls.model_validate(message)
    except ValidationError as exc:
        details = [
            {"field": ".".join(str(part) for part in err.get("loc", ())), "message": err.get("msg", "invalid")}
            for err in exc.errors()
        ]
        raise ApiError("E_INVALID_REQUEST", "invalid WebSocket message", details=details) from exc


def _limit(value: int | None, default: int = 30) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or not MIN_LIMIT <= value <= MAX_LIMIT:
        raise ApiError(
            "E_INVALID_REQUEST",
            f"limit must be an integer between {MIN_LIMIT} and {MAX_LIMIT}",
        )
    return value


def _before(value: int | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ApiError("E_INVALID_REQUEST", "before must be an integer >= 1")
    return value


async def _sender(conn: _Connection) -> None:
    while True:
        payload = await conn.queue.get()
        try:
            await conn.websocket.send_json(payload)
        except (WebSocketDisconnect, RuntimeError, OSError):
            return


async def _handle_subscribe(conn: _Connection, message: dict[str, Any]) -> None:
    data = _validate(WsSubscribeMessage, message)
    before = _before(data.before)
    limit = _limit(data.limit)
    conn.subscribe(data.session_id)
    page = await conn.runtime.backend.snapshot(data.session_id, before=before, limit=limit)
    detail = await conn.runtime.backend.get_session(data.session_id)
    conn.send({"type": "history.snapshot", "sessionId": data.session_id, **page})
    conn.send(
        {
            "type": "session.state",
            "sessionId": data.session_id,
            "state": detail["state"],
            "lockState": detail["lockState"],
        }
    )


async def _handle_unsubscribe(conn: _Connection, message: dict[str, Any]) -> None:
    data = _validate(WsUnsubscribeMessage, message)
    conn.unsubscribe(data.session_id)


async def _handle_history_page(conn: _Connection, message: dict[str, Any]) -> None:
    data = _validate(WsHistoryPageMessage, message)
    limit = _limit(data.limit)
    before = _before(data.before)
    page = await conn.runtime.backend.get_transcript(
        data.session_id,
        before=before,
        limit=limit,
        include_reasoning=True,
        max_chars=conn.runtime.settings.max_chars,
    )
    conn.send(
        {
            "type": "history.page.result",
            "requestId": data.request_id,
            "sessionId": data.session_id,
            **page,
        }
    )


async def _handle_prompt(conn: _Connection, message: dict[str, Any]) -> None:
    data = _validate(WsPromptMessage, message)
    if not data.text.strip():
        raise ApiError("E_INVALID_REQUEST", "prompt text must not be empty")
    conn.subscribe(data.session_id)
    await conn.runtime.backend.start_turn(
        data.session_id,
        data.text,
        client_message_id=data.client_message_id,
        request_id=data.request_id,
    )


async def _handle_cancel(conn: _Connection, message: dict[str, Any]) -> None:
    data = _validate(WsCancelMessage, message)
    await conn.runtime.backend.cancel_turn(data.session_id, data.request_id)


async def _handle_config_set(conn: _Connection, message: dict[str, Any]) -> None:
    data = _validate(WsConfigSetMessage, message)
    conn.subscribe(data.session_id)
    await conn.runtime.backend.set_config(data.session_id, data.config_id, data.value)


async def _handle_permission_respond(conn: _Connection, message: dict[str, Any]) -> None:
    data = _validate(WsPermissionRespondMessage, message)
    await conn.runtime.backend.respond_permission(data.request_id, data.decision)


async def _dispatch(conn: _Connection, message: dict[str, Any]) -> None:
    message_type = message.get("type")
    if not isinstance(message_type, str) or not message_type:
        raise ApiError("E_INVALID_REQUEST", "message must contain a string 'type'")
    if message_type == "ping":
        conn.send({"type": "pong", "nonce": message.get("nonce")})
        return
    handlers = {
        "subscribe": _handle_subscribe,
        "unsubscribe": _handle_unsubscribe,
        "history.page": _handle_history_page,
        "prompt": _handle_prompt,
        "cancel": _handle_cancel,
        "config.set": _handle_config_set,
        "permission.respond": _handle_permission_respond,
    }
    handler = handlers.get(message_type)
    if handler is None:
        raise ApiError("E_INVALID_REQUEST", f"unknown message type: {message_type}")
    await handler(conn, message)


async def _receive_loop(conn: _Connection) -> None:
    runtime = conn.runtime
    while True:
        try:
            raw = await asyncio.wait_for(
                conn.websocket.receive_text(),
                timeout=runtime.settings.ws_idle_timeout_seconds,
            )
        except asyncio.TimeoutError:
            with suppress(Exception):
                await conn.websocket.close(code=1000, reason="idle timeout")
            return
        try:
            message = json.loads(raw)
        except json.JSONDecodeError:
            conn.send(_error_payload(ApiError("E_INVALID_REQUEST", "message must be valid JSON")))
            continue
        if not isinstance(message, dict):
            conn.send(_error_payload(ApiError("E_INVALID_REQUEST", "message must be a JSON object")))
            continue
        try:
            await _dispatch(conn, message)
        except ApiError as exc:
            conn.send(_error_payload(exc, message))
        except Exception:  # noqa: BLE001 - keep the socket alive
            logger.exception("ws message failed", extra={"event": "ws_message_failed"})
            conn.send(_error_payload(ApiError("E_INTERNAL", "internal server error"), message))


def create_ws_router() -> APIRouter:
    router = APIRouter()

    @router.websocket("/api/v1/ws")
    async def ws_chat(websocket: WebSocket) -> None:
        runtime: Runtime = websocket.app.state.runtime
        # Personal LAN mode: no authentication or WS ticket (captain decision T7).
        await websocket.accept()
        conn = _Connection(websocket, runtime)
        try:
            try:
                workspaces = await runtime.backend.list_workspaces()
            except ApiError:
                workspaces = []
            await websocket.send_json(
                {
                    "type": "hello",
                    "apiVersion": 1,
                    "serverVersion": __version__,
                    "capabilities": {"image": False},
                    "workspaces": workspaces,
                }
            )
            sender = asyncio.create_task(_sender(conn), name="dsh-pocket-ws-sender")
            try:
                await _receive_loop(conn)
            finally:
                sender.cancel()
                with suppress(asyncio.CancelledError):
                    await sender
        except WebSocketDisconnect:
            pass
        finally:
            conn.unsubscribe_all()

    return router
