#!/usr/bin/env python3
"""Minimal fake ACP agent for offline tests (newline-delimited JSON-RPC 2.0).

Not a model: emits deterministic block-level updates, asks for permission once
per turn unless FAKE_NO_PERMISSION=1, and supports injected errors/crashes.
Environment knobs:
  FAKE_DELAY=0.005            seconds between emitted updates
  FAKE_RESUME_ERROR=provider|locked|not-resumable
  FAKE_PROMPT_ERROR=rate-limit|provider
  FAKE_NO_PERMISSION=1
  FAKE_PREFIX=session-fake
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import uuid

DELAY = float(os.environ.get("FAKE_DELAY", "0.005"))
RESUME_ERROR = os.environ.get("FAKE_RESUME_ERROR", "")
PROMPT_ERROR = os.environ.get("FAKE_PROMPT_ERROR", "")
NO_PERMISSION = os.environ.get("FAKE_NO_PERMISSION", "") == "1"
PREFIX = os.environ.get("FAKE_PREFIX", f"session-fake-{uuid.uuid4().hex[:8]}")

_lock = threading.Lock()
_out_lock = threading.Lock()
_sessions: dict[str, str] = {}
_counter = 0
_cancelled: dict[str, threading.Event] = {}
_responses: dict[int, "queue.Queue[dict]"] = {}
_permission_waits: dict[str, "queue.Queue[dict]"] = {}


def send(obj: dict) -> None:
    with _out_lock:
        sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
        sys.stdout.flush()


def config_options(model: str = "fake-fast", reasoning: str = "low") -> list[dict]:
    def value(name: str) -> str:
        return json.dumps(["fake-provider", name], separators=(",", ":"))

    return [
        {
            "id": "model",
            "name": "Model",
            "category": "model",
            "type": "select",
            "currentValue": value(model),
            "options": [
                {
                    "group": "fake-provider",
                    "name": "Fake Provider",
                    "options": [
                        {"value": value("fake-fast"), "name": "Fake Fast"},
                        {"value": value("fake-smart"), "name": "Fake Smart"},
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
            "options": [
                {"value": "off", "name": "Off"},
                {"value": "low", "name": "Low"},
                {"value": "high", "name": "High"},
            ],
        },
    ]


def update(session_id: str, payload: dict) -> None:
    send({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": session_id, "update": payload}})


def ask_permission(session_id: str, call_id: str, options: list[dict]) -> str:
    request_id = 9000 + uuid.uuid4().int % 1000
    queue: "queue.Queue[dict]" = __import__("queue").Queue()
    _responses[request_id] = queue
    _permission_waits[session_id] = queue
    send(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "session/request_permission",
            "params": {
                "sessionId": session_id,
                "toolCall": {"toolCallId": call_id},
                "options": options,
            },
        }
    )
    try:
        response = queue.get(timeout=30)
    except Exception:  # noqa: BLE001
        return "cancelled"
    finally:
        _responses.pop(request_id, None)
        _permission_waits.pop(session_id, None)
    outcome = (response.get("result") or {}).get("outcome") or {}
    if outcome.get("outcome") == "cancelled":
        return "cancelled"
    return str(outcome.get("optionId") or "cancelled")


def run_prompt(request_id, params: dict) -> None:
    session_id = params.get("sessionId", "")
    blocks = params.get("prompt") or []
    text = "".join(block.get("text", "") for block in blocks if isinstance(block, dict) and block.get("type") == "text")
    if "__crash__" in text:
        send({"jsonrpc": "2.0", "id": request_id,
              "error": {"code": -32603, "message": "Internal error", "data": {"details": "fake crash"}}})
        time.sleep(0.02)
        os._exit(42)
    if PROMPT_ERROR == "rate-limit":
        send({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32603, "message": "Internal error",
              "data": {"details": "Free usage limit reached (RATE_LIMIT, status 429)"}}})
        return
    if PROMPT_ERROR == "provider":
        send({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32603, "message": "Internal error",
              "data": {"details": 'no adapter registered for provider "fake-provider"'}}})
        return
    cancelled = _cancelled.setdefault(session_id, threading.Event())
    cancelled.clear()
    message_id = f"msg_{uuid.uuid4().hex[:8]}"
    call_id = f"call_{uuid.uuid4().hex[:8]}"
    tool_title = "bash"
    raw_input = {"command": "echo fake-tool", "description": "fake tool call"}
    options = [
        {"optionId": "allow-once", "name": "Allow once", "kind": "allow_once"},
        {"optionId": "reject-once", "name": "Reject", "kind": "reject_once"},
    ]

    update(session_id, {"sessionUpdate": "agent_thought_chunk", "messageId": f"th_{message_id}",
                        "content": {"type": "text", "text": "thinking about " + text[:40]}})
    time.sleep(DELAY)
    update(session_id, {"sessionUpdate": "tool_call", "toolCallId": call_id, "title": tool_title,
                        "kind": "other", "status": "in_progress", "rawInput": raw_input})
    time.sleep(DELAY)
    decision = "allow-once"
    if not NO_PERMISSION:
        decision = ask_permission(session_id, call_id, options)
        if decision == "cancelled" or cancelled.is_set():
            update(session_id, {"sessionUpdate": "tool_call_update", "toolCallId": call_id,
                                "status": "failed", "content": [{"type": "content", "content": {"type": "text", "text": "cancelled"}}]})
            send({"jsonrpc": "2.0", "id": request_id, "result": {"stopReason": "cancelled"}})
            return
    status = "completed" if decision == "allow-once" else "failed"
    output = "fake-tool-ok" if status == "completed" else "user rejected"
    update(session_id, {"sessionUpdate": "tool_call_update", "toolCallId": call_id, "status": status,
                        "content": [{"type": "content", "content": {"type": "text", "text": output}}]})
    time.sleep(DELAY)
    if cancelled.is_set():
        send({"jsonrpc": "2.0", "id": request_id, "result": {"stopReason": "cancelled"}})
        return
    for chunk in ("fake reply: ", text, "\n(done)"):
        if cancelled.is_set():
            send({"jsonrpc": "2.0", "id": request_id, "result": {"stopReason": "cancelled"}})
            return
        update(session_id, {"sessionUpdate": "agent_message_chunk", "messageId": message_id,
                            "content": {"type": "text", "text": chunk}})
        time.sleep(DELAY)
    update(session_id, {"sessionUpdate": "usage_update", "used": 1234, "size": 1000000})
    send({"jsonrpc": "2.0", "id": request_id, "result": {"stopReason": "end_turn"}})


def handle_request(message: dict) -> None:
    global _counter
    method = message.get("method")
    request_id = message.get("id")
    params = message.get("params") or {}
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": request_id, "result": {
            "protocolVersion": 1,
            "agentInfo": {"name": "fake-acp", "version": "0.0.1"},
            "agentCapabilities": {"promptCapabilities": {"image": False},
                                  "sessionCapabilities": {"close": {}, "list": {}, "resume": {}}},
            "authMethods": [],
        }})
    elif method == "session/new":
        _counter += 1
        session_id = f"{PREFIX}-{_counter}"
        _sessions[session_id] = params.get("cwd", "")
        send({"jsonrpc": "2.0", "id": request_id,
              "result": {"sessionId": session_id, "configOptions": config_options()}})
    elif method == "session/list":
        cwd = params.get("cwd")
        sessions = [{"sessionId": sid, "cwd": path} for sid, path in _sessions.items()
                    if cwd in (None, path)]
        send({"jsonrpc": "2.0", "id": request_id, "result": {"sessions": sessions}})
    elif method == "session/resume":
        session_id = params.get("sessionId")
        if RESUME_ERROR == "provider":
            send({"jsonrpc": "2.0", "id": request_id, "error": {
                "code": -32603, "message": "Internal error",
                "data": {"details": 'no adapter registered for provider "our-free-model"'}}})
            return
        if RESUME_ERROR == "locked":
            send({"jsonrpc": "2.0", "id": request_id, "error": {
                "code": -32603, "message": "Internal error",
                "data": {"details": f'session "{session_id}" is already owned by an active write handle'}}})
            return
        if RESUME_ERROR == "not-resumable":
            send({"jsonrpc": "2.0", "id": request_id, "error": {
                "code": -32602, "message": f"Invalid params: session is not resumable: {session_id}"}})
            return
        if session_id in _sessions:
            send({"jsonrpc": "2.0", "id": request_id, "result": {"configOptions": config_options()}})
        else:
            # Simulate a restart: the phone session continues with default options.
            _sessions[session_id] = params.get("cwd", "")
            send({"jsonrpc": "2.0", "id": request_id, "result": {"configOptions": config_options()}})
    elif method == "session/close":
        session_id = params.get("sessionId")
        if session_id in _sessions:
            _sessions.pop(session_id, None)
            send({"jsonrpc": "2.0", "id": request_id, "result": {}})
        else:
            send({"jsonrpc": "2.0", "id": request_id, "error": {
                "code": -32602, "message": f"Invalid params: unknown session: {session_id}"}})
    elif method == "session/set_config_option":
        session_id = params.get("sessionId")
        config_id = params.get("configId")
        value = params.get("value")
        options = config_options()
        if config_id not in ("model", "reasoning_effort"):
            send({"jsonrpc": "2.0", "id": request_id, "error": {
                "code": -32602, "message": f"Invalid params: unknown session config option: {config_id}"}})
            return
        for option in options:
            if option["id"] == config_id:
                option["currentValue"] = value
        send({"jsonrpc": "2.0", "id": request_id, "result": {"configOptions": options}})
    elif method == "session/prompt":
        threading.Thread(target=run_prompt, args=(request_id, params), daemon=True).start()
    else:
        send({"jsonrpc": "2.0", "id": request_id, "error": {
            "code": -32601, "message": f"Method not found: {method}"}})


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        if "method" in message:
            threading.Thread(target=handle_request, args=(message,), daemon=True).start()
        elif message.get("method") == "session/cancel":
            session_id = (message.get("params") or {}).get("sessionId")
            event = _cancelled.setdefault(session_id, threading.Event())
            event.set()
            wait_queue = _permission_waits.get(session_id)
            if wait_queue is not None:
                wait_queue.put({"result": {"outcome": {"outcome": "cancelled"}}})
        elif "id" in message:
            queue = _responses.get(message["id"])
            if queue is not None:
                queue.put(message)
    os._exit(0)


if __name__ == "__main__":
    main()
