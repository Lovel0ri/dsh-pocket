#!/usr/bin/env python3
"""lock_probe.py — prove the desktop/ACP coexistence failure mode without
touching ~/.dsh.

The JSONL persistence backend guards each session directory with a kernel
``flock`` on ``session.lock`` (source:
``@deepseek-ai/dsh-session-persistence-jsonl/lib/types/lease.js`` ->
``SessionWriteLease.acquire``, POSIX branch: ``open(path, "w")`` + non-blocking
``flock``; contention maps to ``SessionAlreadyOwnedError``). Writes take the
lock lazily on the first materializing write; reads never touch it.

This probe:
  1. reports which live sessions currently hold the lock (read-only flock test
     against ~/.dsh — no file content is written);
  2. copies a session into the isolated dev home (fresh inode, no lock held),
     holds ``flock`` on the copy from a helper process, and runs
     ``session/resume`` + ``session/prompt`` over ACP to record what a client
     actually sees when another writer owns the session.

Usage:
  python3 spikes/lock_probe.py            # live scan + contention test
  python3 spikes/lock_probe.py --scan-only
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from acp_client import AcpClient, AcpError, allow_once, classify_error  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PROJECT_KEY = "--Volumes-extra1tb-clawdbot-project--"
WORKSPACE = "/Volumes/extra1tb/clawdbot-project"
S_OFFICIAL = "session-3a77d3aa-2332-4758-9408-ada04dd21ed7"


def scan_live(live_home: str) -> list[dict]:
    """Report, per session, whether a live process currently holds the write lock."""
    base = os.path.join(live_home, "sessions", PROJECT_KEY)
    rows = []
    if not os.path.isdir(base):
        return rows
    for name in sorted(os.listdir(base)):
        lock = os.path.join(base, name, "session.lock")
        if not os.path.exists(lock):
            continue
        fd = os.open(lock, os.O_RDONLY)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            state = "free"
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError as exc:
            state = f"held(errno={exc.errno})"
        finally:
            os.close(fd)
        rows.append({"sessionId": name, "lock": state, "logBytes": os.path.getsize(os.path.join(base, name, "session.v4.jsonl.zstd"))})
    return rows


def hold_lock(path: str, seconds: float) -> subprocess.Popen:
    """Spawn a helper that flocks ``path`` for ``seconds`` (exclusive, non-blocking)."""
    code = (
        "import fcntl,os,sys,time\n"
        "fd=os.open(sys.argv[1], os.O_RDWR)\n"
        "fcntl.flock(fd, fcntl.LOCK_EX|fcntl.LOCK_NB)\n"
        "print('locked', flush=True)\n"
        "time.sleep(float(sys.argv[2]))\n"
    )
    proc = subprocess.Popen([sys.executable, "-c", code, path, str(seconds)], stdout=subprocess.PIPE, text=True)
    assert proc.stdout is not None
    assert proc.stdout.readline().strip() == "locked"
    return proc


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live-home", default=os.path.expanduser("~/.dsh"))
    ap.add_argument("--dev-home", default=os.path.join(ROOT, ".dev-home"))
    ap.add_argument("--scan-only", action="store_true")
    ap.add_argument("--out", default=os.path.join(HERE, "_transcripts", "lock.summary.json"))
    args = ap.parse_args(argv)

    report: dict = {"liveHome": args.live_home, "devHome": os.path.abspath(args.dev_home)}
    report["liveLocks"] = scan_live(args.live_home)
    print(json.dumps(report["liveLocks"], indent=2, ensure_ascii=False))
    if args.scan_only:
        _save(args.out, report)
        return 0

    lock_path = os.path.join(os.path.abspath(args.dev_home), "sessions", PROJECT_KEY, S_OFFICIAL, "session.lock")
    if not os.path.exists(lock_path):
        print(f"missing session copy: {lock_path}\nrun: sh spikes/setup_dev_home.sh", file=sys.stderr)
        return 2

    transcript = os.path.join(HERE, "_transcripts")
    os.makedirs(transcript, exist_ok=True)
    client = AcpClient(
        dsh_home=os.path.abspath(args.dev_home),
        profile="acp",
        cwd=WORKSPACE,
        transcript_path=os.path.join(transcript, "lock-contention.jsonl"),
        stderr_path=os.path.join(transcript, "lock-contention.stderr.log"),
        permission_handler=allow_once,
    )
    client.start()
    client.initialize()
    # Baseline: no lock held -> resume works.
    try:
        client.request("session/resume", {"sessionId": S_OFFICIAL, "cwd": WORKSPACE, "mcpServers": []}, timeout=180)
        report["baselineResume"] = {"ok": True}
        client.close_session(S_OFFICIAL)
    except AcpError as exc:
        report["baselineResume"] = {"ok": False, "code": exc.code, "message": exc.message}

    holder = hold_lock(lock_path, seconds=120)
    try:
        try:
            client.request("session/resume", {"sessionId": S_OFFICIAL, "cwd": WORKSPACE, "mcpServers": []}, timeout=180)
            report["resumeWhileLocked"] = {"ok": True}
            try:
                client.request(
                    "session/prompt",
                    {"sessionId": S_OFFICIAL, "prompt": [{"type": "text", "text": "只回复 OK"}]},
                    timeout=240,
                )
                report["promptWhileLocked"] = {"ok": True}
            except AcpError as exc:
                report["promptWhileLocked"] = {"ok": False, "code": exc.code, "message": exc.message,
                                               "details": exc.details, "class": classify_error(exc)}
            client.close_session(S_OFFICIAL)
        except AcpError as exc:
            report["resumeWhileLocked"] = {"ok": False, "code": exc.code, "message": exc.message,
                                           "details": exc.details, "class": classify_error(exc)}
    finally:
        holder.terminate()
        holder.wait(timeout=10)
        time.sleep(0.3)

    # Recovery: lock released -> resume works again.
    try:
        client.request("session/resume", {"sessionId": S_OFFICIAL, "cwd": WORKSPACE, "mcpServers": []}, timeout=180)
        report["resumeAfterUnlock"] = {"ok": True}
        client.close_session(S_OFFICIAL)
    except AcpError as exc:
        report["resumeAfterUnlock"] = {"ok": False, "code": exc.code, "message": exc.message}

    report["exit"] = client.shutdown()
    _save(args.out, report)
    print(json.dumps({k: v for k, v in report.items() if k != "liveLocks"}, indent=2, ensure_ascii=False))
    return 0


def _save(path: str, report: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
    print(f"summary -> {path}")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
