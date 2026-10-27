#!/usr/bin/env python3
"""acp_probe.py — reproducible ACP bridge experiments for dsh-pocket (T1).

Every scenario spawns its own ``dsh --profile <p>`` child against an isolated
DSH_HOME, speaks newline-delimited JSON-RPC 2.0, and writes:

  spikes/_transcripts/<scenario>.jsonl         full duplex frame transcript
  spikes/_transcripts/<scenario>.stderr.log    server stderr
  spikes/_transcripts/<scenario>.summary.json  structured findings (evidence)

Usage:
  sh spikes/setup_dev_home.sh [--with-provider]
  python3 spikes/acp_probe.py --scenario all
  python3 spikes/acp_probe.py --scenario prompt --profile pocket

Scenarios: initialize, new, prompt, permission, list, resume, config, cancel,
close, eof, all.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import time
import traceback
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from acp_client import (  # noqa: E402
    AcpClient,
    AcpError,
    allow_once,
    cancel_permission,
    classify_error,
    reject_once,
)

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PROJECT_KEY = "--Volumes-extra1tb-clawdbot-project--"
WORKSPACE = "/Volumes/extra1tb/clawdbot-project"

#: Root sessions copied by setup_dev_home.sh, with the fact each one proves.
S_OFM = "session-1e813b14-f293-480d-9e0e-4b047212d4eb"  # latest route our-free-model
S_OFFICIAL = "session-3a77d3aa-2332-4758-9408-ada04dd21ed7"  # latest route deepseek-official
S_OFM2 = "session-5f856f7f-f89f-488d-a81b-00c5948dc9a9"  # latest route our-free-model (combo)
S_SUBAGENT = "cad8b179-20a5-4c2d-862e-585a714614c2"  # origin=subagent, bare-UUID dir

SCENARIOS: dict[str, Any] = {}


def scenario(fn):
    SCENARIOS[fn.__name__] = fn
    return fn


class Recorder:
    """Accumulates a scenario's structured findings."""

    def __init__(self, name: str, out_dir: str, profile: str, dsh_home: str):
        self.name = name
        self.out_dir = out_dir
        self.profile = profile
        self.dsh_home = dsh_home
        self.data: dict[str, Any] = {
            "scenario": name,
            "profile": profile,
            "dsh_home": dsh_home,
            "startedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "steps": [],
        }

    def step(self, label: str, **fields: Any) -> None:
        self.data["steps"].append({"label": label, **fields})
        print(f"  [{label}] " + json.dumps(fields, ensure_ascii=False)[:400])

    def save(self) -> str:
        os.makedirs(self.out_dir, exist_ok=True)
        path = os.path.join(self.out_dir, f"{self.name}.summary.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(self.data, fh, ensure_ascii=False, indent=2)
        return path


def new_client(rec: Recorder, *, permission=allow_once, extra_env=None, log_prefix=None) -> AcpClient:
    prefix = log_prefix or rec.name
    return AcpClient(
        dsh_home=rec.dsh_home,
        profile=rec.profile,
        cwd=WORKSPACE,
        transcript_path=os.path.join(rec.out_dir, f"{prefix}.jsonl"),
        stderr_path=os.path.join(rec.out_dir, f"{prefix}.stderr.log"),
        permission_handler=permission,
        extra_env=extra_env or {},
    )


def session_header(dsh_home: str, session_id: str) -> dict[str, Any] | None:
    """Read a session log header via the zstd CLI (Python has no zstandard)."""
    log = os.path.join(dsh_home, "sessions", PROJECT_KEY, session_id, "session.v4.jsonl.zstd")
    if not os.path.exists(log):
        return None
    out = subprocess.run(["zstd", "-dc", log], capture_output=True, text=True, check=True).stdout
    first = out.split("\n", 1)[0]
    return json.loads(first)


def err_of(fn, *args, **kwargs) -> dict[str, Any]:
    """Run a request and normalise its outcome into a JSON-friendly dict."""
    try:
        return {"ok": True, "result": fn(*args, **kwargs)}
    except AcpError as exc:
        return {"ok": False, "error": {"code": exc.code, "message": exc.message, "details": exc.details, "class": classify_error(exc)}}
    except Exception as exc:  # noqa: BLE001 - reported as evidence
        return {"ok": False, "error": {"code": type(exc).__name__, "message": str(exc)}}


# --------------------------------------------------------------------------- 1
@scenario
def initialize(rec: Recorder) -> None:
    c = new_client(rec).start()
    t0 = time.monotonic()
    result = c.initialize()
    rec.step("initialize/protocolVersion=1", elapsedSeconds=round(time.monotonic() - t0, 3), result=result)
    # ACP v1 servers answer with their own version; a bogus client version must
    # still get a stable answer instead of a handshake failure.
    rec.step("authenticate", **err_of(c.request, "authenticate", {"methodId": "none"}, timeout=30))
    # Unknown method: proves the error envelope shape the client must handle.
    rec.step("unknown/method", **err_of(c.request, "session/load", {"sessionId": "x"}, timeout=30))
    rec.step("malformed/params", **err_of(c.request, "session/new", {}, timeout=30))
    rec.step("exit", **c.shutdown())
    rec.save()


# --------------------------------------------------------------------------- 2
@scenario
def new(rec: Recorder) -> None:
    c = new_client(rec).start()
    c.initialize()
    rec.step("session/new", **err_of(c.request, "session/new", {"cwd": WORKSPACE, "mcpServers": []}, timeout=120))
    # mcpServers is optional in the schema; record whether the server enforces it.
    rec.step("session/new/no-mcpServers", **err_of(c.request, "session/new", {"cwd": WORKSPACE}, timeout=120))
    rec.step("session/new/relative-cwd", **err_of(c.request, "session/new", {"cwd": "relative/path", "mcpServers": []}, timeout=60))
    rec.step("session/new/additionalDirectories", **err_of(
        c.request, "session/new", {"cwd": WORKSPACE, "mcpServers": [], "additionalDirectories": [WORKSPACE]}, timeout=60))
    rec.step("session/new/nonexistent-cwd", **err_of(
        c.request, "session/new", {"cwd": "/Volumes/extra1tb/clawdbot-project/__definitely_absent__", "mcpServers": []}, timeout=60))
    # Keep one live session for the exit path: closing stdin must quiesce it.
    made = c.request("session/new", {"cwd": WORKSPACE, "mcpServers": []}, timeout=120)
    rec.step("session/new/id", sessionId=made["sessionId"])
    rec.step("exit/with-live-session", **c.shutdown())
    rec.save()


# --------------------------------------------------------------------------- 3
@scenario
def prompt(rec: Recorder) -> None:
    c = new_client(rec).start()
    c.initialize()
    made = c.request("session/new", {"cwd": WORKSPACE, "mcpServers": []}, timeout=120)
    sid = made["sessionId"]
    rec.step("configOptions", configOptions=made.get("configOptions"))
    t0 = time.monotonic()
    outcome = err_of(c.request, "session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": "只回复 OK"}]}, timeout=300)
    rec.step("session/prompt", elapsedSeconds=round(time.monotonic() - t0, 3), **outcome)
    rec.step("updates", updates=c.updates(sid))
    rec.step("permissions", requests=c.permission_requests())
    rec.step("session/close", **err_of(c.close_session, sid))
    rec.step("exit", **c.shutdown())
    rec.save()


# --------------------------------------------------------------------------- 4
@scenario
def permission(rec: Recorder) -> None:
    """Tool call + approval round-trip, both answers."""
    prompt_text = (
        "用 bash 工具把 hello 写入 /Users/bing/dsh-pocket-probe-outside.txt（工作区之外）。"
        "如果被沙箱拒绝，请按提示用 sandbox_permissions 原样重试一次，不要做别的事。"
    )
    for answer_name, handler in (("allow-once", allow_once), ("reject-once", reject_once), ("cancel", cancel_permission)):
        c = new_client(rec, permission=handler, log_prefix=f"permission-{answer_name}").start()
        c.initialize()
        made = c.request("session/new", {"cwd": WORKSPACE, "mcpServers": []}, timeout=120)
        sid = made["sessionId"]
        out = err_of(c.request, "session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": prompt_text}]}, timeout=300)
        permitted = c.permission_requests()
        tool_results = [
            u for u in c.updates(sid) if u.get("sessionUpdate") == "tool_call_update"
        ]
        rec.step(
            f"prompt/{answer_name}",
            outcome=out,
            permissionRequests=permitted,
            toolUpdateStatuses=[{"toolCallId": u.get("toolCallId"), "status": u.get("status"), "text": _first_text(u)} for u in tool_results],
        )
        rec.step(f"close/{answer_name}", **err_of(c.close_session, sid))
        rec.step(f"exit/{answer_name}", **c.shutdown())
    rec.save()


def _first_text(update: dict[str, Any]) -> str:
    for item in update.get("content") or []:
        content = item.get("content") or {}
        if content.get("type") == "text":
            return content.get("text", "")[:300]
    return ""


# --------------------------------------------------------------------------- 5
@scenario
def list(rec: Recorder) -> None:  # noqa: A001 - scenario name matches the ACP method
    c = new_client(rec).start()
    c.initialize()
    rec.step("session/list/no-filter", **err_of(c.request, "session/list", {}, timeout=120))
    rec.step("session/list/cwd", **err_of(c.request, "session/list", {"cwd": WORKSPACE}, timeout=120))
    rec.step("session/list/cwd-other", **err_of(
        c.request, "session/list", {"cwd": "/Users/bing/Desktop/ClawdbotScripts"}, timeout=120))
    rec.step("session/list/relative-cwd", **err_of(c.request, "session/list", {"cwd": "relative"}, timeout=60))
    rec.step("session/list/bad-cursor", **err_of(c.request, "session/list", {"cwd": WORKSPACE, "cursor": "not-a-cursor!"}, timeout=60))
    # Deterministic cursor test: the cursor is base64url(JSON([createdAt, sessionId]))
    # of the previous page's last entry, so craft one from a copied log's header.
    # Using the newest copied root session proves that paging really resumes
    # strictly *after* the cursor (older createdAt, then byte-order of the id).
    header = session_header(rec.dsh_home, S_OFM)
    if header is not None:
        raw = json.dumps([header["createdAt"], header["id"]], separators=(",", ":")).encode()
        cursor = base64.urlsafe_b64encode(raw).decode().rstrip("=")
        rec.step("session/list/cursor-from-newest", sessionId=S_OFM, createdAt=header["createdAt"], cursor=cursor,
                 **err_of(c.request, "session/list", {"cwd": WORKSPACE, "cursor": cursor}, timeout=120))
    # Non-canonical base64url of a valid payload must be rejected (the server
    # re-encodes and compares), so clients must echo nextCursor verbatim.
    raw = json.dumps([1, "session-x"], separators=(",", ":")).encode()
    padded = base64.urlsafe_b64encode(raw).decode()
    if padded.endswith("="):
        rec.step("session/list/non-canonical-cursor", cursor=padded, **err_of(
            c.request, "session/list", {"cwd": WORKSPACE, "cursor": padded}, timeout=60))
    # A cursor that is canonical JSON but names an unknown session: still legal
    # (keyset pagination is positional, not an existence check).
    raw = json.dumps([0, "session-00000000-0000-0000-0000-000000000000"], separators=(",", ":")).encode()
    cursor0 = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    rec.step("session/list/cursor-before-all", **err_of(c.request, "session/list", {"cwd": WORKSPACE, "cursor": cursor0}, timeout=120))
    rec.step("exit", **c.shutdown())
    rec.save()


# --------------------------------------------------------------------------- 6
@scenario
def resume(rec: Recorder) -> None:
    c = new_client(rec).start()
    c.initialize()
    # 6a. A session whose latest logged route is deepseek-official must resume.
    ok = err_of(c.request, "session/resume", {"sessionId": S_OFFICIAL, "cwd": WORKSPACE, "mcpServers": []}, timeout=180)
    rec.step("session/resume/deepseek-official", **ok)
    if ok["ok"]:
        sid = S_OFFICIAL
        rec.step("session/resume/already-active", **err_of(
            c.request, "session/resume", {"sessionId": sid, "cwd": WORKSPACE, "mcpServers": []}, timeout=60))
        rec.step("session/prompt-after-resume", **err_of(
            c.request, "session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": "只回复 OK"}]}, timeout=300))
        rec.step("updates-after-resume", updates=c.updates(sid))
        rec.step("session/close", **err_of(c.close_session, sid))
    # 6b. Same id, wrong workspace.
    rec.step("session/resume/wrong-cwd", **err_of(
        c.request, "session/resume", {"sessionId": S_OFM, "cwd": "/Volumes/extra1tb/clawdbot-project/04_tools", "mcpServers": []}, timeout=60))
    # 6c. Subagent sessions are not resumable.
    rec.step("session/resume/subagent", **err_of(
        c.request, "session/resume", {"sessionId": S_SUBAGENT, "cwd": WORKSPACE, "mcpServers": []}, timeout=60))
    # 6d. Unknown id.
    rec.step("session/resume/unknown", **err_of(
        c.request, "session/resume", {"sessionId": "session-00000000-0000-0000-0000-000000000000", "cwd": WORKSPACE, "mcpServers": []}, timeout=60))
    # 6e. Latest logged route uses a provider this profile does not mount.
    rec.step("session/resume/our-free-model", **err_of(
        c.request, "session/resume", {"sessionId": S_OFM, "cwd": WORKSPACE, "mcpServers": []}, timeout=120))
    rec.step("session/resume/our-free-model-2", **err_of(
        c.request, "session/resume", {"sessionId": S_OFM2, "cwd": WORKSPACE, "mcpServers": []}, timeout=120))
    # 6f. mcpServers omitted on resume (optional per schema).
    rec.step("session/resume/no-mcpServers", **err_of(
        c.request, "session/resume", {"sessionId": S_SUBAGENT, "cwd": WORKSPACE}, timeout=60))
    rec.step("exit", **c.shutdown())
    rec.save()


# --------------------------------------------------------------------------- 7
@scenario
def config(rec: Recorder) -> None:
    c = new_client(rec).start()
    c.initialize()
    made = c.request("session/new", {"cwd": WORKSPACE, "mcpServers": []}, timeout=120)
    sid = made["sessionId"]
    options = {o["id"]: o for o in made.get("configOptions") or []}
    rec.step("defaults", model=options.get("model", {}).get("currentValue"),
             reasoningEffort=options.get("reasoning_effort", {}).get("currentValue"))
    # Move to a model of the same provider, then flip reasoning effort.
    target = None
    for group in options.get("model", {}).get("options") or []:
        for opt in group.get("options") or []:
            if '"deepseek-official","deepseek-v4-flash"' in opt["value"]:
                target = opt["value"]
    rec.step("set/model", value=target, **err_of(
        c.request, "session/set_config_option", {"sessionId": sid, "configId": "model", "value": target}, timeout=120))
    rec.step("set/reasoning_effort", **err_of(
        c.request, "session/set_config_option", {"sessionId": sid, "configId": "reasoning_effort", "value": "low"}, timeout=120))
    rec.step("set/reasoning_effort-unknown", **err_of(
        c.request, "session/set_config_option", {"sessionId": sid, "configId": "reasoning_effort", "value": "turbo"}, timeout=60))
    rec.step("set/model-unknown-value", **err_of(
        c.request, "session/set_config_option", {"sessionId": sid, "configId": "model", "value": "not-a-model"}, timeout=60))
    rec.step("set/unknown-config-id", **err_of(
        c.request, "session/set_config_option", {"sessionId": sid, "configId": "temperature", "value": "1"}, timeout=60))
    rec.step("set/non-string-value", **err_of(
        c.request, "session/set_config_option", {"sessionId": sid, "configId": "model", "value": 3}, timeout=60))
    rec.step("set/unknown-session", **err_of(
        c.request, "session/set_config_option", {"sessionId": "session-00000000-0000-0000-0000-000000000000", "configId": "model", "value": target}, timeout=60))
    # The change must be visible to the next prompt and reported back on set.
    rec.step("prompt-after-set", **err_of(
        c.request, "session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": "只回复 OK"}]}, timeout=300))
    rec.step("updates", updates=c.updates(sid))
    rec.step("close", **err_of(c.close_session, sid))
    rec.step("exit", **c.shutdown())
    rec.save()


# --------------------------------------------------------------------------- 8
@scenario
def cancel(rec: Recorder) -> None:
    """Cancellation with a deterministic in-flight window (a sleeping bash job)."""
    import threading

    c = new_client(rec).start()
    c.initialize()
    made = c.request("session/new", {"cwd": WORKSPACE, "mcpServers": []}, timeout=120)
    sid = made["sessionId"]

    box: dict[str, Any] = {}

    def sleeping_prompt() -> None:
        box.update(
            err_of(
                c.request,
                "session/prompt",
                {"sessionId": sid, "prompt": [{"type": "text", "text": "用 bash 工具执行 sleep 20，只执行这一条命令，不要做任何别的事。"}]},
                timeout=300,
            )
        )

    th = threading.Thread(target=sleeping_prompt, daemon=True)
    th.start()
    # Wait until the tool call is actually running: that is the deterministic
    # "prompt is in flight" window.
    seen_tool = c.wait_for(
        lambda: any(u.get("sessionUpdate") == "tool_call" for u in c.updates(sid)), timeout=120
    )
    rec.step("inflight-tool-call-seen", seen=seen_tool, updates=c.updates(sid)[:3])
    rec.step("second-prompt-while-inflight", **err_of(
        c.request, "session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": "只回复 OK"}]}, timeout=30))
    t_cancel = time.monotonic()
    c.notify("session/cancel", {"sessionId": sid})
    th.join(timeout=180)
    rec.step("first-prompt-result", cancelledAfterSeconds=round(time.monotonic() - t_cancel, 2), result=box)
    rec.step("updates", updates=c.updates(sid))
    # Unknown session ids are documented no-ops on the cancel notification.
    c.notify("session/cancel", {"sessionId": "session-00000000-0000-0000-0000-000000000000"})
    rec.step("cancel/unknown-session", note="notification sent; no response is defined for notifications")
    # The session must stay usable after a cancel.
    rec.step("prompt-after-cancel", **err_of(
        c.request, "session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": "只回复 OK"}]}, timeout=300))
    rec.step("updates-after-cancel", updates=c.updates(sid)[-3:])
    rec.step("close", **err_of(c.close_session, sid))
    rec.step("exit", **c.shutdown())
    rec.save()


# --------------------------------------------------------------------------- 9
@scenario
def close(rec: Recorder) -> None:
    c = new_client(rec).start()
    c.initialize()
    made = c.request("session/new", {"cwd": WORKSPACE, "mcpServers": []}, timeout=120)
    sid = made["sessionId"]
    rec.step("close", **err_of(c.close_session, sid))
    rec.step("close/again", **err_of(c.close_session, sid))
    rec.step("prompt/after-close", **err_of(
        c.request, "session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": "x"}]}, timeout=60))
    rec.step("set/after-close", **err_of(
        c.request, "session/set_config_option", {"sessionId": sid, "configId": "model", "value": "x"}, timeout=60))
    rec.step("close/unknown", **err_of(c.close_session, "session-00000000-0000-0000-0000-000000000000"))
    # A closed session keeps its persisted log and can be listed/resumed again.
    rec.step("list/after-close", **err_of(c.request, "session/list", {"cwd": WORKSPACE}, timeout=120))
    rec.step("resume/after-close", **err_of(
        c.request, "session/resume", {"sessionId": sid, "cwd": WORKSPACE, "mcpServers": []}, timeout=120))
    rec.step("exit", **c.shutdown())
    rec.save()


# -------------------------------------------------------------------------- 10
@scenario
def eof(rec: Recorder) -> None:
    c = new_client(rec).start()
    c.initialize()
    made = c.request("session/new", {"cwd": WORKSPACE, "mcpServers": []}, timeout=120)
    rec.step("session/new", sessionId=made["sessionId"])
    rec.step("exit/EOF", **c.shutdown(eof=True, timeout=30))
    rec.save()


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--scenario", default="all", choices=sorted(SCENARIOS) + ["all"])
    ap.add_argument("--profile", default="acp", help="dsh profile to boot (acp | pocket)")
    ap.add_argument("--dsh-home", default=os.path.join(ROOT, ".dev-home"))
    ap.add_argument("--out-dir", default=os.path.join(HERE, "_transcripts"))
    args = ap.parse_args(argv)

    dsh_home = os.path.abspath(args.dsh_home)
    if not os.path.isdir(dsh_home):
        print(f"missing DSH_HOME: {dsh_home}\nrun: sh spikes/setup_dev_home.sh", file=sys.stderr)
        return 2
    names = sorted(SCENARIOS) if args.scenario == "all" else [args.scenario]
    failures = []
    for name in names:
        print(f"\n=== scenario: {name} (profile={args.profile}) ===")
        rec = Recorder(name, os.path.abspath(args.out_dir), args.profile, dsh_home)
        t0 = time.monotonic()
        try:
            SCENARIOS[name](rec)
        except Exception:  # noqa: BLE001 - a scenario crash is itself evidence
            rec.data["error"] = traceback.format_exc()
            traceback.print_exc()
            failures.append(name)
        rec.data["durationSeconds"] = round(time.monotonic() - t0, 2)
        print(f"  summary -> {rec.save()}")
    if failures:
        print(f"\nFAILED scenarios: {', '.join(failures)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
