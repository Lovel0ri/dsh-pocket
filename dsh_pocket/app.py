"""FastAPI application: auth middleware, REST routes, WS mount, static mount.

Contract: docs/api.md (frozen v1.0). No ACP specifics live in this module.
"""

from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from typing import Any

from fastapi import APIRouter, FastAPI, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import API_VERSION, __version__
from .auth import client_ip
from .browse import browse
from .config import Settings
from .errors import ApiError, install_error_handlers
from .models import (
    CancelRequest,
    ConfigRequest,
    CreateSessionRequest,
    PermissionResponseRequest,
    PromptRequest,
)
from .runtime import Runtime, build_runtime
from .ws import create_ws_router

logger = logging.getLogger("dsh_pocket.app")

def _create_api_router(runtime: Runtime) -> APIRouter:
    settings = runtime.settings
    router = APIRouter()

    @router.get("/api/v1/health")
    async def health() -> dict[str, Any]:
        return {
            "ok": True,
            "version": __version__,
            "acpProfiles": [] if settings.mock else list(settings.profiles),
            "uptimeSec": int(time.monotonic() - runtime.started_at),
            "mode": "mock" if settings.mock else "acp",
            "apiVersion": API_VERSION,
            "auth": "none",
        }

    @router.get("/api/v1/workspaces")
    async def workspaces() -> dict[str, Any]:
        return {"workspaces": await runtime.backend.list_workspaces()}

    @router.get("/api/v1/browse")
    async def browse_dirs(
        path: str | None = Query(default=None),
        show_hidden: bool = Query(default=False, alias="showHidden"),
    ) -> dict[str, Any]:
        return browse(settings, path, show_hidden)

    @router.get("/api/v1/sessions")
    async def sessions(
        workspace_id: str | None = Query(default=None, alias="workspaceId"),
        cursor: str | None = Query(default=None),
        limit: int = Query(default=30, ge=1, le=100),
        include_archived: bool = Query(default=False, alias="includeArchived"),
    ) -> dict[str, Any]:
        return await runtime.backend.list_sessions(workspace_id, cursor, limit, include_archived)

    @router.post("/api/v1/sessions", status_code=201)
    async def create_session(body: CreateSessionRequest) -> dict[str, Any]:
        if not body.workspace_id and not body.cwd:
            raise ApiError("E_INVALID_REQUEST", "workspaceId or cwd is required")
        return {
            "session": await runtime.backend.create_session(body.workspace_id, body.cwd)
        }

    @router.get("/api/v1/sessions/{session_id}")
    async def get_session(session_id: str) -> dict[str, Any]:
        return await runtime.backend.get_session(session_id)

    @router.post("/api/v1/sessions/{session_id}/resume")
    async def resume_session(session_id: str) -> dict[str, Any]:
        return await runtime.backend.resume_session(session_id)

    @router.post("/api/v1/sessions/{session_id}/close")
    async def close_session(session_id: str) -> dict[str, Any]:
        return await runtime.backend.close_session(session_id)

    @router.get("/api/v1/sessions/{session_id}/transcript")
    async def transcript(
        session_id: str,
        before: int | None = Query(default=None, ge=1),
        limit: int = Query(default=30, ge=1, le=100),
        include_reasoning: bool = Query(default=True, alias="includeReasoning"),
        max_chars: int = Query(default=4000, ge=1, le=1_000_000, alias="maxChars"),
    ) -> dict[str, Any]:
        return await runtime.backend.get_transcript(
            session_id,
            before=before,
            limit=limit,
            include_reasoning=include_reasoning,
            max_chars=max_chars,
        )

    @router.get("/api/v1/sessions/{session_id}/tool-calls/{call_id}")
    async def tool_call(session_id: str, call_id: str) -> dict[str, Any]:
        return await runtime.backend.get_tool_call(session_id, call_id)

    @router.post("/api/v1/sessions/{session_id}/messages", status_code=202)
    async def send_message(session_id: str, body: PromptRequest) -> dict[str, Any]:
        if not body.text.strip():
            raise ApiError("E_INVALID_REQUEST", "message text must not be empty")
        return await runtime.backend.start_turn(
            session_id,
            body.text,
            client_message_id=body.client_message_id,
            request_id=None,
        )

    @router.post("/api/v1/sessions/{session_id}/cancel")
    async def cancel_turn(session_id: str, body: CancelRequest | None = None) -> dict[str, Any]:
        await runtime.backend.cancel_turn(session_id, body.request_id if body else None)
        return {"ok": True}

    @router.put("/api/v1/sessions/{session_id}/config")
    async def set_config(session_id: str, body: ConfigRequest) -> dict[str, Any]:
        options = await runtime.backend.set_config(session_id, body.config_id, body.value)
        return {"configOptions": options}

    @router.post("/api/v1/sessions/{session_id}/permissions/{request_id}")
    async def respond_permission(
        session_id: str, request_id: str, body: PermissionResponseRequest
    ) -> dict[str, Any]:
        await runtime.backend.respond_permission(request_id, body.decision)
        return {"ok": True}

    @router.api_route(
        "/api/{rest:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        include_in_schema=False,
    )
    async def unknown_api_path(rest: str) -> dict[str, Any]:
        raise ApiError("E_NOT_FOUND", f"unknown API path: /api/{rest}")

    return router


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings if settings is not None else Settings()
    runtime = build_runtime(settings)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        logger.info(
            "server started",
            extra={"event": "server_started", "mode": "mock" if settings.mock else "acp"},
        )
        try:
            yield
        finally:
            await runtime.backend.shutdown()
            logger.info("server stopped", extra={"event": "server_stopped"})

    app = FastAPI(
        title="dsh-pocket",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.runtime = runtime
    install_error_handlers(app)

    @app.middleware("http")
    async def rate_limit_middleware(request: Request, call_next):  # type: ignore[no-untyped-def]
        path = request.url.path
        if not path.startswith("/api/v1") or path == "/api/v1/health" or request.method == "OPTIONS":
            return await call_next(request)
        ip = client_ip(request.client)
        try:
            runtime.api_limiter.hit(ip)
        except ApiError as exc:
            return exc.response()
        started = time.perf_counter()
        response = await call_next(request)
        logger.info(
            "api request",
            extra={
                "event": "api_request",
                "method": request.method,
                "path": path,
                "status": response.status_code,
                "durationMs": round((time.perf_counter() - started) * 1000, 1),
                "client": ip,
            },
        )
        return response

    # CORS must be outermost so preflight requests never hit the auth check.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.resolved_cors_origins(),
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "OPTIONS"],
        allow_headers=["authorization", "content-type"],
    )

    app.include_router(_create_api_router(runtime))
    app.include_router(create_ws_router())

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon() -> Response:
        return Response(status_code=204)

    static_dir = settings.resolved_static_dir()
    if static_dir.is_dir():
        app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")
    else:
        @app.get("/", include_in_schema=False)
        async def index() -> JSONResponse:
            return JSONResponse(
                {
                    "name": "dsh-pocket",
                    "version": __version__,
                    "mode": "mock" if settings.mock else "acp",
                    "api": "/api/v1/health",
                    "note": "static/ not found: add the mobile frontend there",
                }
            )

    return app
