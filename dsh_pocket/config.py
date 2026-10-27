"""Runtime configuration for dsh-pocket (personal LAN, no authentication)."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

_PACKAGE_ROOT = Path(__file__).resolve().parent
_PROJECT_ROOT = _PACKAGE_ROOT.parent
_MACOS_DSH = Path("/Applications/DeepSeek Harness.app/Contents/Resources/runtime/cli/bin/dsh")


def _env_state_dir() -> Path:
    raw = os.environ.get("DSH_POCKET_HOME")
    return Path(raw).expanduser() if raw else Path.home() / ".dsh-pocket"


def _env_browse_roots() -> tuple[str, ...]:
    raw = os.environ.get("DSH_POCKET_BROWSE_ROOTS")
    return tuple(part for part in raw.split(os.pathsep) if part) if raw else ()


def _env_mock_workspaces() -> tuple[str, ...]:
    raw = os.environ.get("DSH_POCKET_MOCK_WORKSPACES")
    return tuple(part for part in raw.split(os.pathsep) if part) if raw else ()


@dataclass(frozen=True)
class Settings:
    mock: bool = False
    host: str = "127.0.0.1"
    port: int = 8799
    state_dir: Path = field(default_factory=_env_state_dir)
    profiles: tuple[str, ...] = ("acp",)

    # Real ACP mode (T7)
    dsh_home: Path | None = None
    dsh_command: tuple[str, ...] = ()
    db_path: Path | None = None
    acp_initialize_timeout_seconds: float = 90.0
    acp_request_timeout_seconds: float = 120.0
    prompt_timeout_seconds: float = 900.0
    cancel_grace_seconds: float = 10.0
    permission_timeout_seconds: float = 120.0

    # Behaviour
    ws_idle_timeout_seconds: float = 60.0
    max_chars: int = 4000
    log_level: str = "INFO"
    access_log: bool = True

    # Non-auth flood limiting (personal LAN)
    api_rate_max: int = 60
    api_rate_window_seconds: float = 1.0

    # Filesystem roots
    browse_roots: tuple[str, ...] = field(default_factory=_env_browse_roots)
    mock_workspaces: tuple[str, ...] = field(default_factory=_env_mock_workspaces)
    static_dir: Path | None = None
    cors_origins: tuple[str, ...] = ()

    # ---- resolved helpers -------------------------------------------------

    def resolved_state_dir(self) -> Path:
        return Path(self.state_dir).expanduser()

    def resolved_dsh_home(self) -> Path:
        if self.dsh_home is not None:
            return Path(self.dsh_home).expanduser()
        env = os.environ.get("DSH_HOME")
        return Path(env).expanduser() if env else Path.home() / ".dsh"

    def acp_stderr_dir(self) -> Path:
        return self.resolved_state_dir() / "acp"

    def resolved_db_path(self) -> Path:
        return Path(self.db_path).expanduser() if self.db_path else self.resolved_state_dir() / "pocket.db"

    def resolved_dsh_command(self) -> list[str]:
        if self.dsh_command:
            return list(self.dsh_command)
        found = shutil.which("dsh")
        if found:
            return [found]
        if _MACOS_DSH.exists():
            return [str(_MACOS_DSH)]
        return ["dsh"]

    def resolved_static_dir(self) -> Path:
        return Path(self.static_dir).expanduser() if self.static_dir else _PROJECT_ROOT / "static"

    def resolved_browse_roots(self) -> list[Path]:
        if self.browse_roots:
            roots = [Path(p).expanduser() for p in self.browse_roots]
        else:
            roots = [Path.home()]
            if self.mock:
                roots.extend(Path(p).expanduser() for p in self.resolved_mock_workspace_paths())
        resolved: list[Path] = []
        seen: set[str] = set()
        for root in roots:
            try:
                real = root.resolve()
            except OSError:  # pragma: no cover - defensive
                continue
            key = str(real)
            if key not in seen:
                seen.add(key)
                resolved.append(real)
        return resolved

    def resolved_mock_workspace_paths(self) -> list[Path]:
        raw = list(self.mock_workspaces)
        if raw:
            return [Path(p).expanduser() for p in raw]
        return [Path.cwd(), self.resolved_state_dir() / "demo-workspace"]

    def resolved_cors_origins(self) -> list[str]:
        if self.cors_origins:
            return list(self.cors_origins)
        origins = [
            "http://localhost:%d" % self.port,
            "http://127.0.0.1:%d" % self.port,
        ]
        if self.host not in {"127.0.0.1", "localhost", "::1"}:
            for addr in _lan_ipv4_addresses():  # pragma: no cover - env dependent
                origins.append("http://%s:%d" % (addr, self.port))
        return origins


def _lan_ipv4_addresses() -> list[str]:
    """Best-effort enumeration of this host's non-loopback IPv4 addresses."""
    import socket

    addresses: set[str] = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            addresses.add(info[4][0])
    except OSError:  # pragma: no cover - defensive
        pass
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect(("8.8.8.8", 80))
            addresses.add(sock.getsockname()[0])
        finally:
            sock.close()
    except OSError:  # pragma: no cover - offline hosts
        pass
    return sorted(a for a in addresses if not a.startswith("127."))
