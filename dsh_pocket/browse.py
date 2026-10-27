"""Directory browsing with traversal protection (additive extension A1)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .config import Settings
from .errors import ApiError


def _within_any(path: Path, roots: list[Path]) -> bool:
    for root in roots:
        if path == root:
            return True
        try:
            if path.is_relative_to(root):
                return True
        except ValueError:  # pragma: no cover - defensive
            continue
    return False


def browse(settings: Settings, path: str | None, show_hidden: bool = False) -> dict[str, Any]:
    roots = settings.resolved_browse_roots()
    home = Path.home().resolve()

    if path is None or not str(path).strip():
        target = roots[0]
    else:
        raw = str(path).strip()
        if not os.path.isabs(raw):
            raise ApiError("E_PATH_INVALID", "path must be an absolute directory")
        target = Path(raw).expanduser()

    try:
        resolved = target.resolve()
    except OSError:
        raise ApiError("E_PATH_INVALID", "path cannot be resolved")
    if not resolved.exists() or not resolved.is_dir():
        raise ApiError("E_PATH_INVALID", "path is not an existing directory")
    if not _within_any(resolved, roots):
        raise ApiError("E_PATH_FORBIDDEN", "path is outside the allowed roots")

    entries: list[dict[str, Any]] = []
    try:
        children = sorted(resolved.iterdir(), key=lambda child: child.name.lower())
    except (PermissionError, OSError):
        raise ApiError("E_PATH_FORBIDDEN", "directory is not readable")

    for child in children:
        name = child.name
        if not show_hidden and name.startswith("."):
            continue
        try:
            if not child.is_dir():
                continue
            child_resolved = child.resolve()
        except OSError:  # pragma: no cover - race
            continue
        if not _within_any(child_resolved, roots):
            # Do not advertise symlinks that escape the allowed roots.
            continue
        entries.append(
            {
                "name": name,
                "path": str(child_resolved),
                "type": "directory",
                "symlink": child.is_symlink(),
            }
        )

    parent_path = resolved.parent
    parent = str(parent_path) if _within_any(parent_path, roots) else None
    return {
        "path": str(resolved),
        "parent": parent,
        "home": str(home),
        "roots": [str(root) for root in roots],
        "entries": entries,
    }
