"""Request/response models and shared constants (contract §2–§4)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from . import API_VERSION  # noqa: F401  (re-exported for convenience)

SESSION_STATES = (
    "detached",
    "readonly",
    "idle",
    "streaming",
    "awaiting_permission",
    "cancelling",
    "error",
)
STOP_REASONS = ("end_turn", "max_tokens", "cancelled")
PERMISSION_DECISIONS = ("allow-once", "reject-once", "cancel")
CONFIG_IDS = ("model", "reasoning_effort")


class _Body(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)


class CreateSessionRequest(_Body):
    workspace_id: str | None = Field(default=None, min_length=1, alias="workspaceId")
    cwd: str | None = Field(default=None, min_length=1)


class PromptRequest(_Body):
    client_message_id: str = Field(min_length=1, alias="clientMessageId")
    text: str = Field(min_length=1)


class CancelRequest(_Body):
    request_id: str | None = Field(default=None, alias="requestId")


class ConfigRequest(_Body):
    config_id: str = Field(min_length=1, alias="configId")
    value: str


class PermissionResponseRequest(_Body):
    decision: Literal["allow-once", "reject-once", "cancel"]


class WsPromptMessage(_Body):
    request_id: str | None = Field(default=None, alias="requestId")
    session_id: str = Field(alias="sessionId")
    client_message_id: str | None = Field(default=None, alias="clientMessageId")
    text: str


class WsSubscribeMessage(_Body):
    session_id: str = Field(alias="sessionId")
    before: int | None = None
    limit: int | None = None


class WsHistoryPageMessage(_Body):
    request_id: str = Field(alias="requestId")
    session_id: str = Field(alias="sessionId")
    before: int
    limit: int = 30


class WsUnsubscribeMessage(_Body):
    session_id: str = Field(alias="sessionId")


class WsCancelMessage(_Body):
    session_id: str = Field(alias="sessionId")
    request_id: str | None = Field(default=None, alias="requestId")


class WsConfigSetMessage(_Body):
    request_id: str | None = Field(default=None, alias="requestId")
    session_id: str = Field(alias="sessionId")
    config_id: str = Field(alias="configId")
    value: str


class WsPermissionRespondMessage(_Body):
    request_id: str = Field(alias="requestId")
    decision: Literal["allow-once", "reject-once", "cancel"]


class WsPingMessage(_Body):
    nonce: int | str | None = None
