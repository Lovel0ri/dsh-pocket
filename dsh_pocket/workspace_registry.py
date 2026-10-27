"""Read-only view of ``$DSH_HOME/storages/workspace.json`` (protocol §11.5)."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("dsh_pocket.workspace")


@dataclass(frozen=True)
class WorkspaceEntry:
    id: str
    path: str
    title: str
    created_at: str | None
    updated_at: str | None
    session_ids: tuple[str, ...]


class WorkspaceRegistry:
    def __init__(self, dsh_home: Path) -> None:
        self.dsh_home = Path(dsh_home)
        self._cache: tuple[tuple[int, int], dict] | None = None

    @property
    def path(self) -> Path:
        return self.dsh_home / "storages" / "workspace.json"

    def _load_raw(self) -> dict:
        try:
            stat = self.path.stat()
        except OSError:
            return {}
        key = (stat.st_mtime_ns, stat.st_size)
        if self._cache is not None and self._cache[0] == key:
            return self._cache[1]
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("workspace registry unreadable: %s", exc, extra={"event": "workspace_registry_error"})
            return {}
        if not isinstance(raw, dict):
            return {}
        self._cache = (key, raw)
        return raw

    def workspaces(self) -> list[WorkspaceEntry]:
        raw = self._load_raw()
        tables = ((raw.get("tables") or {}).get("workspaces") or {})
        entries: list[WorkspaceEntry] = []
        if not isinstance(tables, dict):
            return entries
        for workspace_id, entry in tables.items():
            if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
                continue
            session_ids = entry.get("sessionIds") or []
            entries.append(
                WorkspaceEntry(
                    id=workspace_id,
                    path=entry["path"],
                    title=entry.get("title") or os.path.basename(entry["path"]) or workspace_id,
                    created_at=entry.get("createdAt"),
                    updated_at=entry.get("updatedAt"),
                    session_ids=tuple(i for i in session_ids if isinstance(i, str)),
                )
            )
        entries.sort(key=lambda item: item.title.lower())
        return entries

    def by_id(self, workspace_id: str) -> WorkspaceEntry | None:
        return next((entry for entry in self.workspaces() if entry.id == workspace_id), None)

    def by_path(self, path: str) -> WorkspaceEntry | None:
        try:
            wanted = os.path.realpath(path)
        except OSError:  # pragma: no cover
            wanted = path
        for entry in self.workspaces():
            try:
                if os.path.realpath(entry.path) == wanted:
                    return entry
            except OSError:  # pragma: no cover
                continue
        return None

    def archived_ids(self) -> set[str]:
        raw = self._load_raw()
        values = (raw.get("global") or {}).get("archivedSessionIds") or []
        return {value for value in values if isinstance(value, str)}

    def pinned_ids(self) -> set[str]:
        raw = self._load_raw()
        values = (raw.get("global") or {}).get("pinnedSessionIds") or []
        return {value for value in values if isinstance(value, str)}
