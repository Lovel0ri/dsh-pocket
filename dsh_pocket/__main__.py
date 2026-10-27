"""CLI entry point: ``python3 -m dsh_pocket --mock --host 127.0.0.1 --port 8799``."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import uvicorn

from . import __version__
from .app import create_app
from .config import Settings, _lan_ipv4_addresses
from .logging_setup import setup_logging


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dsh-pocket",
        description="Mobile web backend for a local DeepSeek Harness (REST + WebSocket).",
    )
    parser.add_argument("--mock", action="store_true", help="use the in-memory mock backend (no ACP)")
    parser.add_argument("--host", default="127.0.0.1", help="bind address (use 0.0.0.0 for LAN/phone)")
    parser.add_argument("--port", type=int, default=8799, help="listen port (default 8799)")
    parser.add_argument("--state-dir", type=Path, default=None, help="state dir for token (default ~/.dsh-pocket)")
    parser.add_argument("--profile", action="append", default=None, help="ACP profile name (repeatable, default acp)")
    parser.add_argument("--dsh-bin", default=None, help="path to the dsh CLI (default: auto-detect)")
    parser.add_argument("--dsh-home", type=Path, default=None, help="DSH home (default: $DSH_HOME or ~/.dsh)")
    parser.add_argument("--prompt-timeout", type=float, default=900.0, help="prompt timeout seconds")
    parser.add_argument("--acp-request-timeout", type=float, default=120.0, help="ACP request timeout seconds")
    parser.add_argument("--cancel-grace", type=float, default=10.0, help="wait for a cancelled prompt to settle")
    parser.add_argument("--browse-root", action="append", default=None, help="allowed browse root (repeatable)")
    parser.add_argument("--mock-workspace", action="append", default=None, help="mock workspace dir (repeatable)")
    parser.add_argument("--log-level", default="INFO", help="logging level (default INFO)")
    parser.add_argument("--access-log", dest="access_log", action="store_true", default=True)
    parser.add_argument("--no-access-log", dest="access_log", action="store_false")
    parser.add_argument("--api-rate-limit", type=int, default=60, help="API requests/second/IP")
    parser.add_argument("--permission-timeout", type=float, default=120.0, help="tool permission timeout seconds")
    parser.add_argument("--ws-idle-timeout", type=float, default=60.0, help="close idle WebSockets after N seconds")
    return parser


def _env_state_dir() -> Path:
    raw = os.environ.get("DSH_POCKET_HOME")
    return Path(raw).expanduser() if raw else Path.home() / ".dsh-pocket"


def settings_from_args(args: argparse.Namespace, parser: argparse.ArgumentParser | None = None) -> Settings:
    state_dir = Path(args.state_dir).expanduser() if args.state_dir else _env_state_dir()

    mock_workspaces = tuple(args.mock_workspace or ())
    if args.mock and not mock_workspaces and not os.environ.get("DSH_POCKET_MOCK_WORKSPACES"):
        demo = state_dir / "demo-workspace"
        demo.mkdir(parents=True, exist_ok=True)
        mock_workspaces = (str(Path.cwd()), str(demo))

    return Settings(
        mock=args.mock,
        host=args.host,
        port=args.port,
        state_dir=state_dir,
        profiles=tuple(args.profile) if args.profile else ("acp",),
        dsh_home=Path(args.dsh_home).expanduser() if args.dsh_home else None,
        dsh_command=(args.dsh_bin,) if args.dsh_bin else (),
        prompt_timeout_seconds=args.prompt_timeout,
        acp_request_timeout_seconds=args.acp_request_timeout,
        cancel_grace_seconds=args.cancel_grace,
        permission_timeout_seconds=args.permission_timeout,
        ws_idle_timeout_seconds=args.ws_idle_timeout,
        mock_workspaces=mock_workspaces,
        browse_roots=tuple(args.browse_root or ()),
        log_level=args.log_level,
        access_log=args.access_log,
        api_rate_max=args.api_rate_limit,
    )


def _listen_urls(host: str, port: int) -> list[str]:
    if host in {"127.0.0.1", "localhost", "::1"}:
        return [f"http://127.0.0.1:{port}"]
    urls = [f"http://localhost:{port}"]
    if host not in {"0.0.0.0", "::"}:  # pragma: no cover - explicit LAN bind
        urls.insert(0, f"http://{host}:{port}")
    else:  # pragma: no cover - depends on host interfaces
        urls.extend(f"http://{addr}:{port}" for addr in _lan_ipv4_addresses())
    return urls


def _print_qr(url: str) -> None:
    try:
        import qrcode
    except ImportError:
        return
    try:
        qr = qrcode.QRCode(border=1)
        qr.add_data(url)
        qr.make(fit=True)
        qr.print_ascii(invert=True)
    except Exception:  # noqa: BLE001 - QR rendering is best effort
        return


def print_banner(settings: Settings, runtime, *, stream=sys.stdout) -> None:  # type: ignore[no-untyped-def]
    urls = _listen_urls(settings.host, settings.port)
    mode = "mock" if settings.mock else f"ACP ({settings.resolved_dsh_command()[0]}, profile={','.join(settings.profiles)})"
    print(f"dsh-pocket {__version__} | mode={mode}", file=stream)
    for url in urls:
        print(f"  open   : {url}", file=stream)
    print(f"  state  : {settings.resolved_state_dir()} (ACP stderr logs only)", file=stream)
    print(f"  data   : {settings.resolved_dsh_home()} (read-only registry/history; ACP owns its writes)", file=stream)
    print("  auth   : NONE (personal LAN mode)", file=stream)
    print("  WARNING: no authentication - anyone who can reach this port can use the agent.", file=stream)
    print("           Use only on a trusted LAN; never expose it to the internet or port-forward it.", file=stream)
    if settings.host in {"0.0.0.0", "::"}:
        print("  WARNING: binding to all interfaces over plain HTTP.", file=stream)
    if urls:
        _print_qr(urls[-1])
    stream.flush()


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    settings = settings_from_args(args, parser)
    setup_logging(settings.log_level)
    app = create_app(settings)
    runtime = app.state.runtime
    print_banner(settings, runtime)
    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        log_config=None,
        access_log=settings.access_log,
        server_header=False,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())
