"""Reconstructs `Ledger`, accumulated stats, `/tps` perf, and read-only cwds across `--resume` chains."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from davag.context.ledger import Ledger
from davag.context.utf8 import sanitize_utf8_str
from davag.session.perf import TurnPerf


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
        # Prefer the most recent session with recorded conversation, actions, or resumed lineage
        for d in candidates:
            tr = d / "transcript.jsonl"
            act = d / "actions.jsonl"
            meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
            if (
                (tr.is_file() and tr.stat().st_size > 0)
                or (act.is_file() and act.stat().st_size > 0)
                or bool(meta.get("resumed_from"))
            ):
                return d, meta
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


def _index_sessions_by_id(home: Path) -> dict[str, tuple[Path, dict[str, Any]]]:
    sessions_root = home / "sessions"
    by_id: dict[str, tuple[Path, dict[str, Any]]] = {}
    if not sessions_root.is_dir():
        return by_id
    for d in sorted(sessions_root.iterdir(), key=lambda p: p.name, reverse=True):
        mf = d / "meta.json"
        if not d.is_dir() or not mf.is_file():
            continue
        try:
            meta = json.loads(mf.read_text(encoding="utf-8"))
            sid = str(meta.get("id") or "")
            if sid and sid not in by_id:
                by_id[sid] = (d, meta)
        except (OSError, ValueError):
            continue
    return by_id


def resolve_lineage(home: Path, resume_dir: Path, meta: dict[str, Any]) -> dict[str, Any]:
    """Walk `resumed_from` chain (supporting both self-contained and legacy 1-hop sessions)."""
    by_id = _index_sessions_by_id(home)

    # Walk newest -> oldest chain (bounded & cycle-safe)
    chain: list[tuple[Path, dict[str, Any]]] = [(resume_dir, meta)]
    seen: set[str] = {str(meta.get("id") or resume_dir.name)}
    cur_meta = meta
    while cur_meta.get("resumed_from"):
        parent_id = str(cur_meta["resumed_from"])
        if parent_id in seen or parent_id not in by_id:
            break
        seen.add(parent_id)
        p_dir, p_meta = by_id[parent_id]
        chain.append((p_dir, p_meta))
        cur_meta = p_meta

    # 1. Transcript lines (oldest -> newest; stop walking once a self-contained transcript is hit)
    transcript_chunks: list[list[str]] = []
    for s_dir, s_meta in chain:
        tr_file = s_dir / "transcript.jsonl"
        lines = (
            [ln for ln in tr_file.read_text(encoding="utf-8", errors="replace").splitlines() if ln.strip()]
            if tr_file.is_file()
            else []
        )
        transcript_chunks.append(lines)
        if s_meta.get("transcript_accumulated"):
            break
    transcript_chunks.reverse()
    transcript_lines = [ln for chunk in transcript_chunks for ln in chunk]

    # 2. Resumed cwds (newest -> oldest, deduplicated)
    resumed_cwds: list[str] = []
    for _s_dir, s_meta in chain:
        candidates = [
            s_meta.get("cwd"),
            *(s_meta.get("resumed_cwds") or s_meta.get("ro_dirs") or []),
        ]
        for c in candidates:
            if c and str(c) not in resumed_cwds:
                resumed_cwds.append(str(c))

    # 3. Accumulated totals (sum un-accumulated legacy hops up to first totals_accumulated hop)
    totals: dict[str, Any] = {
        "turns": 0,
        "ops": 0,
        "cost": 0.0,
        "in_tokens": 0,
        "out_tokens": 0,
        "cache_read": 0,
        "malformed": 0,
        "nudges": 0,
    }
    for _s_dir, s_meta in chain:
        t = s_meta.get("totals") or {}
        totals["turns"] += int(t.get("turns", 0) or 0)
        totals["ops"] += int(t.get("ops", 0) or 0)
        totals["cost"] = round(float(totals["cost"]) + float(t.get("cost", 0.0) or 0.0), 6)
        totals["in_tokens"] += int(t.get("in_tokens", 0) or 0)
        totals["out_tokens"] += int(t.get("out_tokens", 0) or 0)
        totals["cache_read"] += int(t.get("cache_read", 0) or 0)
        totals["malformed"] += int(t.get("malformed", 0) or 0)
        totals["nudges"] += int(t.get("nudges", 0) or 0)
        if s_meta.get("totals_accumulated"):
            break

    # 4. Parse perf turns + Web UI transcript replay + fallback cache_read from transcript_lines
    perf_turns: list[TurnPerf] = []
    web_replay: list[dict[str, Any]] = []
    usage_cache_read = 0
    for raw in transcript_lines:
        try:
            ev = json.loads(raw)
        except ValueError:
            continue
        etype = ev.get("type")
        data = ev.get("data") or {}
        if etype == "usage":
            usage_cache_read += int(data.get("cache_read", 0) or 0)
        elif etype == "perf" and isinstance(data, dict) and data.get("turn") is not None:
            perf_turns.append(
                TurnPerf(
                    turn=int(data.get("turn", len(perf_turns) + 1)),
                    model=str(data.get("model", "")),
                    effort=str(data.get("effort", "")),
                    ttft=float(data["ttft"]) if data.get("ttft") is not None else None,
                    elapsed=float(data.get("elapsed", 0.0)),
                    out_tokens=int(data.get("out_tokens", 0)),
                    context_in=int(data.get("context_in", 0)),
                    tps=float(data.get("tps", 0.0)),
                    buckets=[float(x) for x in (data.get("buckets") or [])],
                )
            )
        elif etype == "user" and data.get("text"):
            web_replay.append({"type": "user", "text": sanitize_utf8_str(str(data["text"])), "origin": "resume"})
        elif etype == "assistant" and data.get("text"):
            web_replay.append({"type": "assistant", "text": sanitize_utf8_str(str(data["text"])), "origin": "resume"})

    if totals["cache_read"] == 0 and usage_cache_read > 0:
        totals["cache_read"] = usage_cache_read

    return {
        "transcript_lines": transcript_lines,
        "resumed_cwds": resumed_cwds,
        "totals": totals,
        "perf_turns": perf_turns,
        "web_replay": web_replay[-120:],
    }


def restore_ledger_from_lines(lines: list[str], ledger: Ledger) -> int:
    restored = 0
    for raw in lines:
        if not raw.strip():
            continue
        try:
            ev = json.loads(raw)
        except ValueError:
            continue
        etype = ev.get("type")
        data = ev.get("data") or {}
        if etype == "user" and data.get("text"):
            u_txt = sanitize_utf8_str(str(data["text"]))
            ledger.add(
                seg_type="user",
                label=u_txt[:48],
                role="user",
                text=u_txt,
            )
            restored += 1
        elif etype == "assistant" and data.get("text"):
            a_txt = sanitize_utf8_str(str(data["text"]))
            ledger.add(
                seg_type="assistant",
                label=f"restored turn #{data.get('turn', '?')}",
                role="assistant",
                text=a_txt,
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


def restore_ledger_from_session(session_dir: Path, ledger: Ledger) -> int:
    transcript = session_dir / "transcript.jsonl"
    if not transcript.is_file():
        return 0
    return restore_ledger_from_lines(
        transcript.read_text(encoding="utf-8", errors="replace").splitlines(),
        ledger,
    )
