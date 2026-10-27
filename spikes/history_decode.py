#!/usr/bin/env python3
"""history_decode.py — turn DSH desktop session logs into a UI transcript.

Background (all verified against real logs; see docs/protocol.md §4):

* a session lives at ``$DSH_HOME/sessions/<projectKey>/<encodedId>/session.v4.jsonl.zstd``;
  the first line is the session header, every later line is one event;
* ``projectKey`` replaces ``/ \\ :`` runs with ``-`` and escapes every other
  unsafe code unit as ``~XXXX`` (source: dsh-session-persistence-jsonl
  ``projectKey``), so ``/Volumes/extra1tb/clawdbot-project`` becomes
  ``--Volumes-extra1tb-clawdbot-project--``;
* Python has no zstandard module in this environment, hence ``zstd -dc``;
* only five event types are *surface* events that carry a ``surfaceOp``:
  ``system/message``, ``developer/message``, ``user/message``,
  ``assistant/message``, ``tool/result``. Everything else (``tool/call``,
  ``turn/*``, ``job/*``, ``request/*``, ...) is log-only;
* a replacement copy (``surfaceOp = {"op":"replace","startSeq":…,"endSeq":…}``)
  deliberately shadows a range for the *model*; the human transcript must be
  built from append-origin events only (source: dsh-session
  ``lib/types/surface.js`` -> ``isAppendSurfaceEvent`` doc comment).

Usage:
  python3 spikes/history_decode.py --index                     # all workspaces
  python3 spikes/history_decode.py --list /Volumes/extra1tb/clawdbot-project
  python3 spikes/history_decode.py --session <id-or-dir> --limit 20
  python3 spikes/history_decode.py --session <id> --before <seq> --limit 20
  python3 spikes/history_decode.py --stats <id>                # event histogram
  python3 spikes/history_decode.py --session <id> --out /tmp/x.json
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from typing import Any, Iterable, Iterator

HERE = os.path.dirname(os.path.abspath(__file__))
WIRE_KEYS = ("type", "seq", "time", "data")
SURFACE_TYPES = {"system/message", "developer/message", "user/message", "assistant/message", "tool/result"}
#: Types that carry UI meaning; everything else is trace/telemetry.
TRANSCRIPT_TYPES = {"user/message", "assistant/message", "tool/result", "tool/call"}


# --------------------------------------------------------------------- storage
def project_key(cwd: str) -> str:
    """Mirror of the harness project-directory key (see module docstring)."""
    out, sep_run = "", False
    for ch in cwd:
        if ch in "/\\:":
            if not sep_run:
                out += "-"
            sep_run = True
        elif ch != "~" and (ch.isalnum() or ch in "._-"):
            out += ch
            sep_run = False
        else:
            out += "~" + format(ord(ch), "04X")
            sep_run = False
    return "--" + (out.lstrip("-") or "root") + "--"


def iter_lines(path: str) -> Iterator[str]:
    """Stream decompressed JSONL without loading the whole log into memory."""
    if path.endswith(".zstd"):
        proc = subprocess.Popen(["zstd", "-dc", path], stdout=subprocess.PIPE, text=True)
        assert proc.stdout is not None
        try:
            for line in proc.stdout:
                if line.strip():
                    yield line
        finally:
            proc.stdout.close()
            rc = proc.wait()
            if rc != 0:
                raise RuntimeError(f"zstd failed ({rc}) for {path}")
    else:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    yield line


def log_path(home: str, cwd: str, session_id: str) -> str:
    from urllib.parse import quote  # noqa: F401 - no path escaping needed for our ids

    return os.path.join(home, "sessions", project_key(cwd), session_id, "session.v4.jsonl.zstd")


def read_session(path: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Parse header + events, tolerating a torn final line.

    A live writer appends whole lines, but a read racing a write can still see
    a partial tail. Only the *last* line may be incomplete; anything earlier is
    real corruption and raises.
    """
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    lines = list(iter_lines(path))
    if not lines:
        raise ValueError(f"empty session log: {path}")
    header = json.loads(lines[0])
    events: list[dict[str, Any]] = []
    for index, line in enumerate(lines[1:], start=1):
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            if index == len(lines) - 1:
                break  # torn tail from a concurrent append
            raise
    return header, events


# ------------------------------------------------------------------ normalizer
def blocks_text(
    blocks: Iterable[dict[str, Any]], *, kind: str = "text", max_chars: int | None = None
) -> str:
    """Join the text of one block kind ("text" or "reasoning"); tool calls are not text."""
    parts = [block.get("text", "") for block in blocks if block.get("type") == kind]
    text = "\n".join(parts)
    return text if max_chars is None else text[:max_chars]


def _clip(value: Any, max_chars: int | None) -> Any:
    if max_chars is None or not isinstance(value, str) or len(value) <= max_chars:
        return value
    return value[:max_chars] + f"…(+{len(value) - max_chars} chars)"


def normalize(
    header: dict[str, Any],
    events: list[dict[str, Any]],
    *,
    max_chars: int | None = None,
    include_reasoning: bool = True,
    include_approvals: bool = True,
) -> dict[str, Any]:
    """Fold a v4 event log into a UI transcript.

    Returns a dict with ``session`` metadata, ``items`` (chronological UI
    items), ``turns``, ``approvals``, ``usage`` and ``ignored`` (counts per
    filtered event type, so a client can prove it filtered rather than lost
    data).
    """
    items: list[dict[str, Any]] = []
    turns: list[dict[str, Any]] = []
    approvals: list[dict[str, Any]] = []
    pending_approvals: dict[str, dict[str, Any]] = {}
    usage: dict[str, Any] | None = None
    route: dict[str, Any] | None = None
    title: str | None = None
    ignored: dict[str, int] = {}
    replacements = 0
    tool_calls: dict[str, dict[str, Any]] = {}
    tool_results: dict[str, dict[str, Any]] = {}

    for ev in events:
        etype = ev.get("type", "")
        data = ev.get("data") if isinstance(ev.get("data"), dict) else {}
        surface_op = ev.get("surfaceOp")

        if etype not in TRANSCRIPT_TYPES:
            ignored[etype] = ignored.get(etype, 0) + 1
            if etype == "session/title":
                title = data.get("title")
            elif etype == "model/selection":
                route = {"provider": data.get("provider"), "model": data.get("model"),
                         "reasoningEffort": data.get("reasoningEffort")}
            elif etype == "turn/start":
                turns.append({"turn": data.get("turn"), "startSeq": ev.get("seq"), "endSeq": None, "reason": None})
            elif etype == "turn/end":
                for turn in reversed(turns):
                    if turn["turn"] == data.get("turn"):
                        turn["endSeq"] = ev.get("seq")
                        turn["reason"] = data.get("reason")
                        break
            elif etype == "approval/asked":
                record = {
                    "id": data.get("id"),
                    "toolName": data.get("toolName"),
                    "callId": data.get("callId"),
                    "reason": data.get("reason"),
                    "seq": ev.get("seq"),
                    "time": ev.get("time"),
                    "outcome": None,
                }
                approvals.append(record)
                if data.get("id"):
                    pending_approvals[data["id"]] = record
            elif etype == "approval/decided":
                record = pending_approvals.get(data.get("id"))
                if record is not None:
                    record["outcome"] = data.get("outcome")
                    record["decidedSeq"] = ev.get("seq")
            elif etype == "tool/call":
                tool_calls[data.get("callId")] = {
                    "kind": "tool",
                    "callId": data.get("callId"),
                    "name": data.get("name"),
                    "arguments": _clip(data.get("arguments"), max_chars),
                    "status": "running",  # resolved by the matching tool/result
                    "seq": ev.get("seq"),
                    "time": ev.get("time"),
                    "turn": data.get("turn"),
                    "step": data.get("step"),
                    "output": None,
                    "isError": None,
                }
            continue

        # --- surface events ---------------------------------------------------
        if surface_op is not None and surface_op != "append":
            # Model-only replacement copy: never surfaced in a human transcript.
            replacements += 1
            ignored[f"{etype}(replacement)"] = ignored.get(f"{etype}(replacement)", 0) + 1
            continue
        if etype in ("system/message", "developer/message"):
            ignored[etype] = ignored.get(etype, 0) + 1
            continue

        if etype == "user/message":
            content = data.get("content") or []
            items.append({
                "kind": "user",
                "id": data.get("id"),
                "seq": ev.get("seq"),
                "time": ev.get("time"),
                "blocks": content,
                "text": _clip(blocks_text(content), max_chars),
                "source": data.get("source"),
            })
        elif etype == "assistant/message":
            message = data.get("message") or {}
            blocks = message.get("content") or []
            text_blocks = [b for b in blocks if b.get("type") in ("text", "reasoning")]
            call_blocks = [b for b in blocks if b.get("type") == "tool-call"]
            if not include_reasoning:
                text_blocks = [b for b in text_blocks if b.get("type") != "reasoning"]
            # A tool-call block duplicates the log-only tool/call row; prefer the
            # row and only synthesize when the row is missing.
            for block in call_blocks:
                if block.get("id") not in tool_calls:
                    tool_calls[block["id"]] = {
                        "kind": "tool",
                        "callId": block.get("id"),
                        "name": block.get("name"),
                        "arguments": _clip(block.get("arguments"), max_chars),
                        "status": "running",
                        "seq": ev.get("seq"),
                        "time": ev.get("time"),
                        "turn": data.get("turn"),
                        "step": data.get("step"),
                        "output": None,
                        "isError": None,
                    }
            item = {
                "kind": "assistant",
                "id": message.get("id"),
                "seq": ev.get("seq"),
                "time": ev.get("time"),
                "turn": data.get("turn"),
                "step": data.get("step"),
                "blocks": [
                    {**b, "text": _clip(b.get("text"), max_chars)} if b.get("type") in ("text", "reasoning") else b
                    for b in text_blocks
                ],
                "text": _clip(blocks_text(text_blocks, kind="text"), max_chars),
                "reasoning": _clip(blocks_text(text_blocks, kind="reasoning"), max_chars),
                "toolCallIds": [b.get("id") for b in call_blocks],
                "model": (message.get("source") or {}).get("model"),
                "usage": data.get("usage"),
            }
            items.append(item)
            if data.get("usage"):
                usage = data["usage"]
        elif etype == "tool/result":
            message = data.get("message") or {}
            call_id = message.get("toolCallId") or data.get("toolCallId")
            content = message.get("content") or []
            clipped_content = [
                {**b, "text": _clip(b.get("text"), max_chars)} if b.get("type") == "text" else b for b in content
            ]
            tool_results[call_id] = {
                "seq": ev.get("seq"),
                "time": ev.get("time"),
                "output": clipped_content,
                "isError": message.get("isError"),
                "text": _clip(blocks_text(content), max_chars),
            }
            if call_id in tool_calls:
                tool_calls[call_id]["status"] = "failed" if message.get("isError") else "completed"
                tool_calls[call_id]["output"] = clipped_content
                tool_calls[call_id]["isError"] = bool(message.get("isError"))
                tool_calls[call_id]["resultSeq"] = ev.get("seq")
            else:
                # Orphan result (e.g. a call from a forked prefix): keep it addressable.
                tool_calls[call_id] = {
                    "kind": "tool",
                    "callId": call_id,
                    "name": None,
                    "arguments": None,
                    "status": "failed" if message.get("isError") else "completed",
                    "seq": ev.get("seq"),
                    "time": ev.get("time"),
                    "turn": data.get("turn"),
                    "step": data.get("step"),
                    "output": clipped_content,
                    "isError": bool(message.get("isError")),
                }

    # Interleave tool items at their call position. One seq can host an
    # assistant message plus the tool calls it announced, so order by
    # (seq, kind rank) to keep "assistant -> its tools -> result" stable.
    rank = {"user": 0, "assistant": 1, "tool": 2}
    items.extend(tool_calls.values())
    items.sort(key=lambda item: (item.get("seq") or 0, rank.get(item.get("kind"), 3)))

    return {
        "session": {
            "id": header.get("id"),
            "cwd": header.get("cwd"),
            "createdAt": header.get("createdAt"),
            "origin": header.get("origin", "root"),
            "parentSession": header.get("parentSession"),
            "delegationDepth": header.get("delegationDepth"),
            "agentPreset": header.get("agentPreset"),
            "isSeeded": header.get("isSeeded"),
            "title": title,
            "route": route,
            "formatVersion": header.get("version"),
            "eventCount": len(events),
            "replacementEvents": replacements,
        },
        "items": items,
        "turns": turns,
        "approvals": approvals if include_approvals else [],
        "usage": usage,
        "ignored": dict(sorted(ignored.items())),
    }


def page(items: list[dict[str, Any]], *, limit: int, before: int | None = None) -> dict[str, Any]:
    """Newest page over ``items``, grouped by ``seq`` so a seq is never split.

    ``limit`` counts seq groups (a group is one assistant message plus the tool
    calls/results that share its seq). ``nextBefore`` is the first returned
    group's seq: pass it back as ``before`` for the next older page, which keeps
    the cursor stable even though one message spans several items.
    """
    groups: list[list[dict[str, Any]]] = []
    for item in items:
        seq = item.get("seq") or 0
        if before is not None and seq >= before:
            continue
        if groups and (groups[-1][0].get("seq") or 0) == seq:
            groups[-1].append(item)
        else:
            groups.append([item])
    window = groups[-limit:] if limit > 0 else groups
    flat = [item for group in window for item in group]
    return {
        "items": flat,
        "hasMore": len(groups) > len(window),
        "nextBefore": window[0][0]["seq"] if window else None,
    }


def compact(doc: dict[str, Any], *, max_chars: int | None = None) -> dict[str, Any]:
    """Drop raw provider blocks so a page is small enough for a phone.

    The wire contract (docs/api-contract.md) returns this shape: text and
    reasoning joined, tool output joined and clipped, no ``blocks`` arrays.
    """
    items = []
    for item in doc["items"]:
        if item["kind"] == "assistant":
            items.append({
                "kind": "assistant",
                "id": item["id"],
                "seq": item["seq"],
                "time": item["time"],
                "turn": item.get("turn"),
                "step": item.get("step"),
                "text": item.get("text", ""),
                "reasoning": item.get("reasoning", ""),
                "toolCallIds": item.get("toolCallIds") or [],
                "model": item.get("model"),
                "usage": item.get("usage"),
            })
        elif item["kind"] == "tool":
            items.append({
                "kind": "tool",
                "callId": item["callId"],
                "name": item.get("name"),
                "arguments": _clip(item.get("arguments"), max_chars),
                "status": item.get("status"),
                "isError": item.get("isError"),
                "seq": item["seq"],
                "time": item["time"],
                "turn": item.get("turn"),
                "step": item.get("step"),
                "outputText": _clip(blocks_text(item.get("output") or [], kind="text"), max_chars),
            })
        else:
            items.append({
                "kind": "user",
                "id": item["id"],
                "seq": item["seq"],
                "time": item["time"],
                "text": item.get("text", ""),
                "source": item.get("source"),
            })
    return {**{k: v for k, v in doc.items() if k != "items"}, "items": items}


# ---------------------------------------------------------------------- index
def load_workspace_registry(home: str) -> dict[str, Any]:
    path = os.path.join(home, "storages", "workspace.json")
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def workspace_index(home: str) -> list[dict[str, Any]]:
    """Join workspace.json with the on-disk session directories."""
    registry = load_workspace_registry(home)
    global_state = registry.get("global") or {}
    archived = set(global_state.get("archivedSessionIds") or [])
    pinned = set(global_state.get("pinnedSessionIds") or [])
    tables = (registry.get("tables") or {}).get("workspaces") or {}
    result = []
    for workspace_id, entry in tables.items():
        cwd = entry.get("path")
        sessions = []
        key_dir = os.path.join(home, "sessions", project_key(cwd))
        on_disk = set(os.listdir(key_dir)) if os.path.isdir(key_dir) else set()
        for session_id in entry.get("sessionIds") or []:
            log = os.path.join(key_dir, session_id, "session.v4.jsonl.zstd")
            sessions.append({
                "sessionId": session_id,
                "logExists": os.path.exists(log),
                "logBytes": os.path.getsize(log) if os.path.exists(log) else 0,
                "archived": session_id in archived,
                "pinned": session_id in pinned,
            })
        result.append({
            "workspaceId": workspace_id,
            "path": cwd,
            "title": entry.get("title"),
            "createdAt": entry.get("createdAt"),
            "updatedAt": entry.get("updatedAt"),
            "projectKey": project_key(cwd),
            "sessionsInRegistry": len(entry.get("sessionIds") or []),
            "sessionsOnDisk": len(on_disk),
            "unregisteredOnDisk": sorted(on_disk - set(entry.get("sessionIds") or [])),
            "sessions": sessions,
        })
    return result


def resolve(home: str, ref: str) -> tuple[str, str]:
    """Resolve ``<session-id>`` or a log path to (logPath, sessionId)."""
    if os.path.isdir(ref) or os.path.isfile(ref):
        path = ref if os.path.isfile(ref) else os.path.join(ref, "session.v4.jsonl.zstd")
        return path, os.path.basename(os.path.dirname(path) if os.path.isfile(ref) else ref)
    root = os.path.join(home, "sessions")
    for key in sorted(os.listdir(root)) if os.path.isdir(root) else []:
        candidate = os.path.join(root, key, ref, "session.v4.jsonl.zstd")
        if os.path.exists(candidate):
            return candidate, ref
    raise FileNotFoundError(f"session {ref!r} not found under {root}")


def event_stats(events: list[dict[str, Any]]) -> dict[str, Any]:
    types: dict[str, int] = {}
    surface: dict[str, int] = {}
    for ev in events:
        types[ev.get("type", "?")] = types.get(ev.get("type", "?"), 0) + 1
        op = ev.get("surfaceOp")
        if op is not None:
            key = "append" if op == "append" else "replace"
            surface[key] = surface.get(key, 0) + 1
    return {
        "events": len(events),
        "types": dict(sorted(types.items(), key=lambda kv: (-kv[1], kv[0]))),
        "surfaceOps": surface,
        "firstSeq": events[0]["seq"] if events else None,
        "lastSeq": events[-1]["seq"] if events else None,
        "timeSpan": [events[0].get("time"), events[-1].get("time")] if events else None,
    }


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--home", default=os.environ.get("DSH_HOME", os.path.expanduser("~/.dsh")))
    ap.add_argument("--index", action="store_true", help="index every registered workspace")
    ap.add_argument("--list", metavar="CWD", help="list sessions of one workspace")
    ap.add_argument("--session", help="session id or session directory")
    ap.add_argument("--stats", metavar="SESSION", help="event histogram for one session")
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--before", type=int, default=None)
    ap.add_argument("--max-chars", type=int, default=None)
    ap.add_argument("--no-reasoning", action="store_true")
    ap.add_argument("--compact", action="store_true", help="drop raw blocks (API-shaped transcript)")
    ap.add_argument("--out")
    args = ap.parse_args(argv)

    home = os.path.abspath(os.path.expanduser(args.home))
    payload: Any
    if args.index:
        payload = workspace_index(home)
    elif args.list:
        payload = next((w for w in workspace_index(home) if w["path"] == args.list), None)
        if payload is None:
            print(f"workspace not registered: {args.list}", file=sys.stderr)
            return 2
    elif args.stats:
        path, _ = resolve(home, args.stats)
        _, events = read_session(path)
        payload = {"sessionId": args.stats, "log": path, **event_stats(events)}
    elif args.session:
        path, session_id = resolve(home, args.session)
        header, events = read_session(path)
        doc = normalize(header, events, max_chars=args.max_chars, include_reasoning=not args.no_reasoning)
        if args.compact:
            doc = compact(doc, max_chars=args.max_chars)
        doc["paging"] = page(doc["items"], limit=args.limit, before=args.before)
        payload = doc
    else:
        ap.print_help()
        return 2

    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
        print(f"wrote {args.out} ({len(text)} bytes)")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
