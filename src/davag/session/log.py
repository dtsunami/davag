"""Session directory management (`~/.davag/sessions/<timestamp>-<id>/`).

Writes `meta.json` and append-only `transcript.jsonl` with automatic API key redaction.
"""

from __future__ import annotations

import datetime
import json
import re
import secrets
import time
from pathlib import Path
from typing import Any

SECRET_PAT = re.compile(r"(sk-ant-[A-Za-z0-9_-]{12,}|sk-or-v1-[A-Za-z0-9_-]{12,}|sk-[A-Za-z0-9_-]{16,}|AIza[A-Za-z0-9_-]{20,})")


def _redact(s: str) -> str:
    return SECRET_PAT.sub("[REDACTED_KEY]", s)


class SessionLog:
    def __init__(
        self,
        *,
        home: Path,
        cwd: Path,
        provider: str,
        model_id: str,
        resumed_from: str | None = None,
    ) -> None:
        ts = datetime.datetime.now(tz=datetime.UTC).strftime("%Y%m%dT%H%M%SZ")
        self.id = secrets.token_hex(4)
        self.dir = home / "sessions" / f"{ts}-{self.id}"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.meta_path = self.dir / "meta.json"
        self.transcript_path = self.dir / "transcript.jsonl"
        self.started_at = datetime.datetime.now(tz=datetime.UTC).isoformat()

        self.meta: dict[str, Any] = {
            "id": self.id,
            "started_at": self.started_at,
            "cwd": str(cwd),
            "provider": provider,
            "model": model_id,
            "resumed_from": resumed_from,
            "totals": {"turns": 0, "ops": 0, "cost": 0.0, "in_tokens": 0, "out_tokens": 0},
        }
        self._flush_meta()

    def _flush_meta(self) -> None:
        self.meta_path.write_text(json.dumps(self.meta, indent=2) + "\n", encoding="utf-8")

    def record(self, event_type: str, data: Any) -> None:
        line = _redact(json.dumps({"t": time.time(), "type": event_type, "data": data}, ensure_ascii=False))
        with open(self.transcript_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")

    def update_totals(self, stats: dict[str, Any], *, provider: str | None = None, model_id: str | None = None) -> None:
        if provider:
            self.meta["provider"] = provider
        if model_id:
            self.meta["model"] = model_id
        self.meta["totals"] = {
            "turns": stats.get("turns", 0),
            "ops": stats.get("ops", 0),
            "cost": round(float(stats.get("cost", 0.0)), 6),
            "in_tokens": stats.get("in_tokens", 0),
            "out_tokens": stats.get("out_tokens", 0),
        }
        self._flush_meta()


def list_sessions(home: Path, limit: int = 30) -> list[dict[str, Any]]:
    sessions_root = home / "sessions"
    if not sessions_root.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for sub in sorted(sessions_root.iterdir(), key=lambda p: p.name, reverse=True):
        if not sub.is_dir():
            continue
        meta_file = sub / "meta.json"
        if not meta_file.is_file():
            continue
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
            meta["dir"] = str(sub)
            out.append(meta)
        except (OSError, ValueError):
            continue
        if len(out) >= limit:
            break
    return out