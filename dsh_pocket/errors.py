"""Error codes and JSON error envelopes (contract §3.5).

The table below is the frozen mapping from api-contract §4.2 plus the additive
codes documented in docs/api.md §0.2 (A3/A4).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

# code -> (HTTP status, default message, retryable, suggestedAction)
ERROR_TABLE: dict[str, tuple[int, str, bool, str | None]] = {
    "E_RATE_LIMITED": (429, "too many requests", True, "retry-later"),
    "E_NOT_FOUND": (404, "not found", False, None),
    "E_WORKSPACE_UNKNOWN": (404, "unknown workspace", False, "refresh-workspaces"),
    "E_WORKSPACE_MISSING": (409, "workspace directory is missing", False, "refresh-workspaces"),
    "E_SESSION_LOCKED": (409, "session is locked by another process", False, "open-readonly"),
    "E_PROVIDER_UNAVAILABLE": (409, "provider is unavailable in this profile", False,
                               "change-model-or-new-session"),
    "E_NOT_RESUMABLE": (409, "session is not resumable", False, "open-readonly"),
    "E_CWD_MISMATCH": (409, "session cwd does not match", False, "refresh-workspaces"),
    "E_ALREADY_ACTIVE": (409, "session is already active", False, "attach"),
    "E_PROMPT_IN_FLIGHT": (409, "a prompt is already in flight for this session", False,
                           "wait-or-cancel"),
    "E_ACP_INVALID": (400, "invalid params", False, None),
    "E_ACP_INTERNAL": (502, "internal agent error", True, "retry"),
    "E_ACP_UNAVAILABLE": (503, "ACP agent is not available", True, "retry"),
    "E_TIMEOUT": (504, "upstream timeout", True, "retry"),
    "E_INVALID_REQUEST": (400, "invalid request", False, None),
    "E_PATH_FORBIDDEN": (403, "path is outside the allowed roots", False, None),
    "E_PATH_INVALID": (400, "path is invalid", False, None),
    "E_INTERNAL": (500, "internal server error", False, "retry"),
}


@dataclass
class ApiError(Exception):
    """A contract-shaped API error.

    ``details`` may carry the ACP ``error.data.details`` string (T3). Never put
    secrets in ``message``/``details``: both are returned to the client.
    """

    code: str
    message: str | None = None
    details: Any = None
    http_status: int | None = None
    retryable: bool | None = None
    suggested_action: str | None = None
    headers: Mapping[str, str] | None = None
    acp_code: int | None = None

    def __post_init__(self) -> None:
        if self.code not in ERROR_TABLE:
            raise ValueError(f"unknown error code: {self.code}")
        status, default_message, retryable, action = ERROR_TABLE[self.code]
        if self.message is None:
            self.message = default_message
        if self.http_status is None:
            self.http_status = status
        if self.retryable is None:
            self.retryable = retryable
        if self.suggested_action is None:
            self.suggested_action = action
        super().__init__(self.message)

    def payload(self) -> dict[str, Any]:
        error: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "details": self.details,
            "retryable": self.retryable,
            "suggestedAction": self.suggested_action,
        }
        if self.acp_code is not None:
            error["acpCode"] = self.acp_code
        return {"error": error}

    def response(self) -> JSONResponse:
        return JSONResponse(self.payload(), status_code=self.http_status, headers=dict(self.headers or {}))


def _http_code(status: int) -> str:
    if status == 404:
        return "E_NOT_FOUND"
    if status == 403:
        return "E_PATH_FORBIDDEN"
    if status == 429:
        return "E_RATE_LIMITED"
    if 400 <= status < 500:
        return "E_INVALID_REQUEST"
    return "E_INTERNAL"


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _handle_api_error(_request: Request, exc: ApiError) -> JSONResponse:
        return exc.response()

    @app.exception_handler(RequestValidationError)
    async def _handle_validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
        details = []
        for err in exc.errors():
            loc = ".".join(str(part) for part in err.get("loc", ()) if part != "body")
            details.append({"field": loc or "(body)", "message": err.get("msg", "invalid")})
        return ApiError("E_INVALID_REQUEST", "request body/query validation failed", details=details).response()

    @app.exception_handler(StarletteHTTPException)
    async def _handle_http_error(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = _http_code(exc.status_code)
        message = exc.detail if isinstance(exc.detail, str) and exc.detail else None
        return ApiError(code, message, http_status=exc.status_code).response()

    @app.exception_handler(Exception)
    async def _handle_unexpected(_request: Request, exc: Exception) -> JSONResponse:
        import logging

        logging.getLogger("dsh_pocket.error").exception(
            "unhandled request error", extra={"event": "unhandled_error"}
        )
        return ApiError("E_INTERNAL").response()
