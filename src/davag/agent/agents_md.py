"""Operator-authored standing instructions (`AGENTS.md` / `DAVAG.md`), multiplexed into grounding.

Ported directly from `davai/src/agent/agents.js`:
- Reads BOTH `~/.davag/AGENTS.md` (home-wide conventions) AND the first of
  `AGENTS.md` / `DAVAG.md` / `DAVAI.md` / `CLAUDE.md` in `cwd` (unless `DAVAG_AGENTS_MD`
  explicitly overrides the path list).
- Bounds combined size to `DAVAG_AGENTS_BUDGET` (default 6000 tokens, clamped to 8% of
  context window), clipping at markdown headings (`\n#`) rather than mid-sentence and
  naming the on-disk path so the model can `fs.read` the rest if needed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

PROJECT_AGENTS_NAMES = ("DAVAG.md", "AGENTS.md", "DAVAI.md", "CLAUDE.md")
HOME_AGENTS_NAME = "AGENTS.md"
DEFAULT_AGENTS_BUDGET = 6000
MAX_CONTEXT_SHARE = 0.08
CHARS_PER_TOKEN = 3.8


def compute_agents_budget(*, requested_budget: int | None = None, context_limit: int = 0) -> int:
    ceiling = int(context_limit * MAX_CONTEXT_SHARE) if context_limit > 0 else 10_000_000
    asked = requested_budget if requested_budget is not None else DEFAULT_AGENTS_BUDGET
    return max(1, min(asked, ceiling))


def discover_agents_files(
    *,
    home: Path | None = None,
    cwd: Path | None = None,
    explicit_files: list[str] | None = None,
) -> list[dict[str, Any]]:
    if explicit_files:
        base = cwd or Path.cwd()
        return [{"path": (base / p).resolve(), "source": "env"} for p in explicit_files]

    out: list[dict[str, Any]] = []
    if home is not None:
        hp = (home / HOME_AGENTS_NAME).resolve()
        if hp.is_file():
            out.append({"path": hp, "source": "home"})
    if cwd is not None:
        for name in PROJECT_AGENTS_NAMES:
            pp = (cwd / name).resolve()
            if pp.is_file():
                if not any(e["path"] == pp for e in out):
                    out.append({"path": pp, "source": "project"})
                break
    return out


def clip_at_boundary(text: str, max_chars: int, display_path: str) -> tuple[str, bool, int]:
    if len(text) <= max_chars:
        return text, False, len(text)

    head = text[:max_chars]
    last_heading = head.rfind("\n#")
    cut = last_heading if last_heading > max_chars * 0.5 else head.rfind("\n\n")
    keep = text[: cut if cut > 0 else max_chars].rstrip()
    remaining = len(text) - len(keep)
    clipped = (
        f"{keep}\n\n"
        f"[This file continues for a further {remaining:,} characters, which did not fit the "
        f"grounding budget. The full text is on disk at {display_path} — use fs.read if needed.]"
    )
    return clipped, True, len(keep)


def resolve_agents(
    *,
    home: Path | None = None,
    cwd: Path | None = None,
    explicit_files: list[str] | None = None,
    agents_budget: int | None = None,
    context_limit: int = 0,
) -> tuple[list[dict[str, Any]], list[str]]:
    wanted = discover_agents_files(home=home, cwd=cwd, explicit_files=explicit_files)
    tok_budget = compute_agents_budget(requested_budget=agents_budget, context_limit=context_limit)
    budget_chars = int(tok_budget * CHARS_PER_TOKEN)

    sections: list[dict[str, Any]] = []
    notices: list[str] = []
    spent = 0

    for entry in wanted:
        p: Path = entry["path"]
        try:
            raw = p.read_text(encoding="utf-8", errors="replace").strip()
        except OSError as err:
            if entry["source"] == "env":
                notices.append(f"DAVAG_AGENTS_MD: {p} could not be read ({err})")
            continue
        if not raw:
            continue

        room = budget_chars - spent
        if room <= 0:
            notices.append(
                f"{p} was not included: agents budget ({tok_budget:,} tokens) was already spent. "
                "Raise DAVAG_AGENTS_BUDGET or shorten earlier files."
            )
            continue

        text, truncated, kept_chars = clip_at_boundary(raw, room, str(p))
        if truncated:
            notices.append(
                f"{p} ({len(raw):,} chars) was clipped to {kept_chars:,} chars (budget {tok_budget:,} tokens)."
            )
        spent += len(text)
        sections.append(
            {
                "path": str(p),
                "source": entry["source"],
                "text": text,
                "truncated": truncated,
                "bytes": len(raw.encode("utf-8")),
            }
        )

    return sections, notices


def render_agents(sections: list[dict[str, Any]]) -> list[str]:
    out: list[str] = []
    for s in sections:
        out.append("")
        out.append(f"## Project instructions (from {s['path']})")
        out.append(s["text"])
    return out