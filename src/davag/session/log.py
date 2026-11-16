"""Session directory management (`~/.davag/sessions/<timestamp>-<id>/`).

Writes `meta.json` and append-only `transcript.jsonl` with automatic API key redaction.
On `--resume`, carries forward prior `transcript.jsonl` lines and accumulated `totals`
so multi-hop resumes (`S1 -> S2 -> S3`) never lose history or session telemetry.
"""

from __future__ import annotations

import datetime
import json
import re
import secrets
import time
from pathlib import Path
from typing import Any

from davag.context.utf8 import sanitize_utf8, sanitize_utf8_str

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
        ro_dirs: list[Path] | None = None,
        resumed_cwds: list[Path] | None = None,
        initial_totals: dict[str, Any] | None = None,
        initial_transcript_lines: list[str] | None = None,
    ) -> None:
        ts = datetime.datetime.now(tz=datetime.UTC).strftime("%Y%m%dT%H%M%S_%fZ")
        self.id = secrets.token_hex(4)
        self.dir = home / "sessions" / f"{ts}-{self.id}"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.meta_path = self.dir / "meta.json"
        self.transcript_path = self.dir / "transcript.jsonl"
        self.started_at = datetime.datetime.now(tz=datetime.UTC).isoformat()

        base = initial_totals or {}
        self._base_totals: dict[str, Any] = {
            "turns": int(base.get("turns", 0) or 0),
            "ops": int(base.get("ops", 0) or 0),
            "cost": round(float(base.get("cost", 0.0) or 0.0), 6),
            "in_tokens": int(base.get("in_tokens", 0) or 0),
            "out_tokens": int(base.get("out_tokens", 0) or 0),
            "cache_read": int(base.get("cache_read", 0) or 0),
        }

        if initial_transcript_lines:
            cleaned_init = [sanitize_utf8_str(ln) for ln in initial_transcript_lines]
            self.transcript_path.write_text(
                "\n".join(cleaned_init) + "\n",
                encoding="utf-8",
            )

        self.meta: dict[str, Any] = {
            "id": self.id,
            "started_at": self.started_at,
            "cwd": str(cwd),
            "ro_dirs": [str(p) for p in (ro_dirs or [])],
            "resumed_cwds": [str(p) for p in (resumed_cwds or [])],
            "provider": provider,
            "model": model_id,
            "resumed_from": resumed_from,
            "transcript_accumulated": True,
            "totals_accumulated": True,
            "totals": dict(self._base_totals),
            "run_totals": {"turns": 0, "ops": 0, "cost": 0.0, "in_tokens": 0, "out_tokens": 0},
        }
        self._flush_meta()

    def _flush_meta(self) -> None:
        self.meta_path.write_text(json.dumps(self.meta, indent=2) + "\n", encoding="utf-8")

    def record(self, event_type: str, data: Any) -> None:
        safe_data = sanitize_utf8(data)
        line = sanitize_utf8_str(
            _redact(json.dumps({"t": time.time(), "type": event_type, "data": safe_data}, ensure_ascii=False))
        )
        with open(self.transcript_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")

    def update_totals(self, stats: dict[str, Any], *, provider: str | None = None, model_id: str | None = None) -> None:
        if provider:
            self.meta["provider"] = provider
        if model_id:
            self.meta["model"] = model_id
        turns = int(stats.get("turns", 0) or 0)
        ops = int(stats.get("ops", 0) or 0)
        cost = round(float(stats.get("cost", 0.0) or 0.0), 6)
        in_tok = int(stats.get("in_tokens", 0) or 0)
        out_tok = int(stats.get("out_tokens", 0) or 0)
        cache_r = int(stats.get("cache_read", 0) or 0)

        self.meta["totals"] = {
            "turns": turns,
            "ops": ops,
            "cost": cost,
            "in_tokens": in_tok,
            "out_tokens": out_tok,
            "cache_read": cache_r,
            "malformed": int(stats.get("malformed", 0) or 0),
            "nudges": int(stats.get("nudges", 0) or 0),
        }
        self.meta["run_totals"] = {
            "turns": max(0, turns - self._base_totals["turns"]),
            "ops": max(0, ops - self._base_totals["ops"]),
            "cost": round(max(0.0, cost - self._base_totals["cost"]), 6),
            "in_tokens": max(0, in_tok - self._base_totals["in_tokens"]),
            "out_tokens": max(0, out_tok - self._base_totals["out_tokens"]),
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
