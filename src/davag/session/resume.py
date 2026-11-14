"""Reconstructs `Ledger` conversation history from an existing session's `transcript.jsonl`."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from davag.context.ledger import Ledger


def find_session_dir(home: Path, resume_arg: str | bool) -> tuple[Path, dict[str, Any]]:
    sessions_root = home / "sessions"
    if not sessions_root.is_dir():
        raise FileNotFoundError("No past sessions found in ~/.davag/sessions")

    candidates = sorted(
        [d for d in sessions_root.iterdir() if d.is_dir() and (d / "meta.json").is_file()],
        key=lambda p: p.name,
        reverse=True,
    )
    if not candidates:
        raise FileNotFoundError("No past sessions found in ~/.davag/sessions")

    if resume_arg is True:
        # Prefer the most recent session that actually recorded conversation turns
        for d in candidates:
            tr = d / "transcript.jsonl"
            if tr.is_file() and tr.stat().st_size > 0:
                return d, json.loads((d / "meta.json").read_text(encoding="utf-8"))
        chosen = candidates[0]
        return chosen, json.loads((chosen / "meta.json").read_text(encoding="utf-8"))

    prefix = str(resume_arg).strip().lower()
    matches = []
    for d in candidates:
        meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
        sid = str(meta.get("id", "")).lower()
        if sid.startswith(prefix) or d.name.lower().endswith(prefix):
            matches.append((d, meta))

    if not matches:
        raise FileNotFoundError(f'No session matching "{resume_arg}"')
    if len(matches) > 1:
        ids = ", ".join(m[1].get("id", m[0].name) for m in matches[:5])
        raise ValueError(f'Ambiguous session prefix "{resume_arg}" (matches: {ids})')
    return matches[0]


def restore_ledger_from_session(session_dir: Path, ledger: Ledger) -> int:
    transcript = session_dir / "transcript.jsonl"
    if not transcript.is_file():
        return 0

    restored = 0
    for raw in transcript.read_text(encoding="utf-8", errors="replace").splitlines():
        if not raw.strip():
            continue
        try:
            ev = json.loads(raw)
        except ValueError:
            continue
        etype = ev.get("type")
        data = ev.get("data") or {}
        if etype == "user" and data.get("text"):
            ledger.add(
                seg_type="user",
                label=str(data["text"])[:48],
                role="user",
                text=str(data["text"]),
            )
            restored += 1
        elif etype == "assistant" and data.get("text"):
            ledger.add(
                seg_type="assistant",
                label=f"restored turn #{data.get('turn', '?')}",
                role="assistant",
                text=str(data["text"]),
            )
            restored += 1
        elif etype == "ops-result":
            summary = json.dumps(data.get("results") or [], ensure_ascii=False)[:4000]
            ledger.add(
                seg_type="op-result",
                label=f"restored ops ({data.get('status')})",
                role="user",
                text=f"[Restored dr_ops results ({data.get('status')})]\n{summary}",
            )
            restored += 1
    return restored