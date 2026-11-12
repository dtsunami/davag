"""Workspace filesystem sandbox with realpath containment and `.daignore` filtering."""

from __future__ import annotations

import fnmatch
from pathlib import Path


class SandboxError(PermissionError):
    """Raised when a path escapes the allowed working directory or matches `.daignore`."""


class Sandbox:
    def __init__(self, cwd: str | Path, *, ro_dirs: list[Path] | None = None) -> None:
        self.cwd = Path(cwd).resolve()
        self.ro_dirs = [Path(p).resolve() for p in (ro_dirs or [])]
        self.ignore_patterns = self._load_daignore()

    def _load_daignore(self) -> list[str]:
        daignore = self.cwd / ".daignore"
        if not daignore.is_file():
            return []
        patterns: list[str] = []
        for raw in daignore.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.strip()
            if line and not line.startswith("#"):
                patterns.append(line.rstrip("/"))
        return patterns

    def is_ignored(self, abs_path: Path) -> bool:
        try:
            rel_parts = abs_path.relative_to(self.cwd).parts
            rel_posix = "/".join(rel_parts)
        except ValueError:
            return False

        for pat in self.ignore_patterns:
            if fnmatch.fnmatch(rel_posix, pat) or any(fnmatch.fnmatch(p, pat) for p in rel_parts):
                return True
        return False

    def _is_within(self, target: Path, root: Path) -> bool:
        try:
            target.relative_to(root)
            return True
        except ValueError:
            return False

    def resolve_for_read(self, user_path: str | Path) -> Path:
        raw = Path(user_path)
        resolved = (raw if raw.is_absolute() else (self.cwd / raw)).resolve()
        allowed = self._is_within(resolved, self.cwd) or any(
            self._is_within(resolved, ro) for ro in self.ro_dirs
        )
        if not allowed:
            raise SandboxError(
                f"read denied: {user_path} resolves outside {self.cwd} and DAVAG_RO_DIRS"
            )
        if self.is_ignored(resolved):
            raise SandboxError(f"read denied: {self.rel(resolved)} is excluded by .daignore")
        return resolved

    def resolve_for_write(self, user_path: str | Path) -> Path:
        raw = Path(user_path)
        resolved = (raw if raw.is_absolute() else (self.cwd / raw)).resolve()
        if not self._is_within(resolved, self.cwd):
            raise SandboxError(f"write denied: {user_path} resolves outside working directory {self.cwd}")
        if self.is_ignored(resolved):
            raise SandboxError(f"write denied: {self.rel(resolved)} is excluded by .daignore")
        return resolved

    def rel(self, abs_path: str | Path) -> str:
        p = Path(abs_path).resolve()
        try:
            return p.relative_to(self.cwd).as_posix()
        except ValueError:
            return str(p)