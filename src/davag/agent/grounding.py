"""Activity-ranked workspace grounding + upfront DaVinci Resolve project grounding (`grounding.py`).

Matches `davai/src/agent/grounding.js` AND adds a compact snapshot of the open DaVinci Resolve
Project (bins, clips with `clip_id` & frames, timelines, active timeline tracks, and markers)
into the initial cached prefix so the model NEVER wastes Turn 1 on spurious discovery ops!
"""

from __future__ import annotations

import contextlib
import datetime
import math
import os
import platform
import subprocess
import time
from pathlib import Path
from typing import Any

from davag.config.env import Config
from davag.transport.base import ResolveTransport

from .agents_md import render_agents, resolve_agents
from .prompt import build_system_prompt
from .sandbox import Sandbox

MAX_TOP_ENTRIES = 40
MAX_PREVIEW_DIRS = 6
MAX_PREVIEW_FILES = 6
MAX_GROUNDING_CLIPS = 40


def _ago(mtime_sec: float) -> str:
    s = max(0.0, time.time() - mtime_sec)
    if s < 90:
        return "just now"
    if s < 5400:
        return f"{round(s / 60)}m ago"
    if s < 129600:
        return f"{round(s / 3600)}h ago"
    return f"{round(s / 86400)}d ago"


def _activity_score(dir_path: Path, mtime_sec: float) -> float:
    try:
        n = sum(1 for _ in dir_path.iterdir())
    except OSError:
        n = 0
    age_days = max(0.0, (time.time() - mtime_sec) / 86400.0)
    return math.log1p(n) * 2.0 + 10.0 / (1.0 + age_days)


def _preview_dir(dir_path: Path, sandbox: Sandbox) -> list[str]:
    kept: list[tuple[str, float]] = []
    try:
        for child in dir_path.iterdir():
            if sandbox.is_ignored(child):
                continue
            try:
                mt = child.stat().st_mtime
            except OSError:
                continue
            suffix = "/" if child.is_dir() else ""
            kept.append((f"{child.name}{suffix}", mt))
    except OSError:
        return []
    kept.sort(key=lambda x: x[1], reverse=True)
    out = [name for name, _ in kept[:MAX_PREVIEW_FILES]]
    if len(kept) > MAX_PREVIEW_FILES:
        out.append(f"… {len(kept) - MAX_PREVIEW_FILES} more")
    return out


def _activity_listing(root: Path, sandbox: Sandbox) -> str:
    dirs: list[tuple[str, Path, float, float]] = []
    files: list[tuple[str, float]] = []
    try:
        for child in root.iterdir():
            if sandbox.is_ignored(child):
                continue
            try:
                mt = child.stat().st_mtime
            except OSError:
                continue
            if child.is_dir():
                dirs.append((child.name, child, mt, _activity_score(child, mt)))
            else:
                files.append((child.name, mt))
    except OSError as err:
        return f"  (cannot read: {err})"

    dirs.sort(key=lambda d: d[3], reverse=True)
    files.sort(key=lambda f: f[1], reverse=True)

    out: list[str] = []
    for name, mt in files[:MAX_TOP_ENTRIES]:
        out.append(f"  {name}  ({_ago(mt)})")
    if len(files) > MAX_TOP_ENTRIES:
        out.append(f"  … {len(files) - MAX_TOP_ENTRIES} more files")

    for name, _p, mt, _sc in dirs:
        out.append(f"  {name}/  ({_ago(mt)})")

    for name, p, _mt, _sc in dirs[:MAX_PREVIEW_DIRS]:
        prev = _preview_dir(p, sandbox)
        if prev:
            out.append("")
            out.append(f"  {name}/")
            for ln in prev:
                out.append(f"    {ln}")
    return "\n".join(out)


def _git_info(root: Path) -> dict[str, Any] | None:
    def _run(args: list[str]) -> str:
        res = subprocess.run(
            ["git", *args],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=2,
            check=True,
        )
        return res.stdout.strip()

    try:
        branch = _run(["rev-parse", "--abbrev-ref", "HEAD"])
        status = _run(["status", "--porcelain"])
        dirty = len([ln for ln in status.splitlines() if ln.strip()])
        try:
            recent = [ln for ln in _run(["log", "-3", "--oneline", "--no-decorate"]).splitlines() if ln]
        except (OSError, subprocess.SubprocessError):
            recent = []
        return {"branch": branch, "dirty": dirty, "recent": recent}
    except (OSError, subprocess.SubprocessError):
        return None


async def snapshot_resolve_grounding(
    transport: ResolveTransport | None,
    *,
    cache_dir: Path | None = None,
) -> str:
    """Gather a compact, one-time initial snapshot of the open DaVinci Resolve project for Grounding.

    Includes cached Clip Digests (`~/.davag/cache/digests/`) automatically under each clip so
    previously indexed footage is immediately understood at session start!
    """
    from davag.media_intel.digest import load_clip_digest

    if transport is None:
        return "## DaVinci Resolve Session\n  Status: disconnected (launch Resolve or run `/resolve`)"

    try:
        st = await transport.call("resolve.status", timeout=3.0)
    except Exception as err:  # noqa: BLE001
        return f"## DaVinci Resolve Session\n  Status: unreachable ({err})"

    lines = [
        "## DaVinci Resolve Session (Snapshot at Session Start)",
        f"  Product: {st.get('product')} {st.get('version')} (transport={st.get('mode')}, page={st.get('page')})",
    ]
    proj = st.get("project")
    if not proj:
        lines.append("  Open Project: (none)")
        return "\n".join(lines)

    lines.append(
        f"  Project: \"{proj.get('name')}\" — {proj.get('width')}x{proj.get('height')} @ {proj.get('fps')} fps"
    )

    # Media Pool Clips (up to MAX_GROUNDING_CLIPS) + Cached Clip Digests!
    with contextlib.suppress(Exception):
        mp_clips = await transport.call("mediapool.clips", {"recursive": True}, timeout=3.0)
        clips = mp_clips.get("clips") or []
        lines.append(f"  Media Pool Clips ({mp_clips.get('count', len(clips))} total):")
        for c in clips[:MAX_GROUNDING_CLIPS]:
            proxy_tag = " | proxy=linked" if c.get("proxy_path") else ""
            lines.append(
                f"    - clip_id={c['clip_id']} | \"{c['name']}\" | bin={c['bin_path']} | "
                f"{c['resolution']} @ {c['fps']}fps | {c['frames']} frames{proxy_tag} | path={c['file_path']}"
            )
            if cache_dir is not None and c.get("file_path"):
                digest = load_clip_digest(cache_dir, str(c["file_path"]))
                if digest and digest.get("summary"):
                    short_sum = str(digest["summary"]).strip()[:600].replace("\n", " / ")
                    lines.append(f"      [Cached Digest ({digest.get('scout_model', 'scout')}): {short_sum}]")
        if len(clips) > MAX_GROUNDING_CLIPS:
            lines.append(f"    … {len(clips) - MAX_GROUNDING_CLIPS} more clips (use mediapool.clips)")

    # Timelines + Active Timeline Markers
    with contextlib.suppress(Exception):
        tl_list = await transport.call("timeline.list", timeout=3.0)
        tls = tl_list.get("timelines") or []
        if not tls:
            lines.append("  Timelines: 0 (use `timeline.create` or `timeline.append_clips` to start one)")
        else:
            lines.append(f"  Timelines ({len(tls)}):")
            for t in tls:
                act = " [ACTIVE]" if t.get("active") else ""
                lines.append(
                    f"    - timeline_id={t['timeline_id']} | \"{t['name']}\"{act} | {t['duration_frames']} frames"
                )
        if st.get("timeline"):
            m_res = await transport.call("marker.list", timeout=2.5)
            markers = m_res.get("markers") or []
            if markers:
                lines.append(f"  Active Timeline Markers ({len(markers)}):")
                for m in markers[:25]:
                    author = "davag" if str(m.get("custom_data", "")).startswith("davag") else "OPERATOR"
                    note_s = f" — {m['note']}" if m.get("note") else ""
                    lines.append(f"    - [{author}] {m['tc']} ({m['color']}) {m['name']}{note_s}")

    return "\n".join(lines)


def build_grounding_text(
    *,
    cfg: Config,
    sandbox: Sandbox,
    resolve_section: str = "",
) -> tuple[str, list[dict[str, Any]], list[str]]:
    root = sandbox.cwd
    shell_name = os.environ.get("COMSPEC", "cmd.exe") if os.name == "nt" else "/bin/sh"
    lines: list[str] = [
        "# Project grounding",
        "",
        f"Working directory: {root}",
        f"Platform: {platform.system()} ({platform.machine()}), shell: {shell_name}",
        f"Date: {datetime.date.today().isoformat()}",
    ]

    if cfg.ro_dirs:
        lines.append("")
        lines.append("Readable outside working directory (DAVAG_RO_DIRS), NOT writable:")
        for d in cfg.ro_dirs:
            lines.append(f"  {d}")

    git = _git_info(root)
    if git:
        lines.append("")
        lines.append(f"Git: branch {git['branch']}, {git['dirty']} uncommitted change(s)")
        if git["recent"]:
            lines.append("Recent commits:")
            for c in git["recent"]:
                lines.append(f"  {c}")

    if resolve_section:
        lines.extend(["", resolve_section])

    lines.extend(["", "## Workspace Directory", _activity_listing(root, sandbox)])

    sections, notices = resolve_agents(
        home=cfg.home,
        cwd=root,
        explicit_files=getattr(cfg, "agents_files", None),
        agents_budget=getattr(cfg, "agents_budget", None),
        context_limit=cfg.context_limit,
    )
    lines.extend(render_agents(sections))
    return "\n".join(lines), sections, notices


def build_static_system_prompt(
    *,
    prompts: dict[str, str],
    home: Path,
    cwd: Path,
    cfg: Config | None = None,
    sandbox: Sandbox | None = None,
    resolve_section: str = "",
) -> str:
    """Backwards-compatible builder for the byte-stable cached system prompt."""
    if cfg is not None and sandbox is not None:
        g_text, _, _ = build_grounding_text(
            cfg=cfg, sandbox=sandbox, resolve_section=resolve_section
        )
    else:
        sb = sandbox or Sandbox(cwd)
        sections, _ = resolve_agents(home=home, cwd=cwd)
        parts = [
            "# Project grounding",
            f"Working directory: {cwd}",
            f"Platform: {platform.system()} ({platform.machine()})",
            f"Date: {datetime.date.today().isoformat()}",
        ]
        if resolve_section:
            parts.extend(["", resolve_section])
        parts.extend(["", "## Workspace Directory", _activity_listing(cwd, sb), *render_agents(sections)])
        g_text = "\n".join(parts)

    return build_system_prompt(prompts=prompts, grounding=g_text)


async def poll_live_resolve_banner(transport: ResolveTransport | None) -> str | None:
    """Fast (<15ms) poll of Resolve's live UI state + operator markers, injected ONLY when changed!

    If you move the playhead, switch timelines, or press `M` in Resolve to leave a marker note
    for davag, this surfaces the exact delta without touching the cached system prompt!
    """
    if transport is None:
        return "[Resolve State: disconnected — start Resolve or run `/resolve`]"
    try:
        st: dict[str, Any] = await transport.call("resolve.status", timeout=2.5)
    except Exception:  # noqa: BLE001
        return "[Resolve State: unreachable]"

    proj = st.get("project")
    tl = st.get("timeline")
    proj_name = proj.get("name") if isinstance(proj, dict) else None
    fps = proj.get("fps") if isinstance(proj, dict) else None
    res_wh = f"{proj.get('width')}x{proj.get('height')}" if isinstance(proj, dict) and proj.get("width") else ""

    if not tl or not isinstance(tl, dict):
        return (
            f'[Resolve Live: mode={st.get("mode")} | page={st.get("page")} | '
            f'project="{proj_name or "none"}" ({fps}fps {res_wh}) | timeline=none]'
        )

    # Also check for operator markers (`custom_data == ""`) on the active timeline!
    op_marker_str = ""
    with contextlib.suppress(Exception):
        m_res = await transport.call("marker.list", timeout=2.0)
        user_markers = [
            f"{m['tc']} ({m['color']}) \"{m['name']}\"{': ' + m['note'] if m.get('note') else ''}"
            for m in (m_res.get("markers") or [])
            if not str(m.get("custom_data", "")).startswith("davag")
        ]
        if user_markers:
            op_marker_str = "\n[Operator Timeline Markers: " + " ; ".join(user_markers[:10]) + "]"

    return (
        f'[Resolve Live: mode={st.get("mode")} | page={st.get("page")} | '
        f'project="{proj_name}" ({fps}fps {res_wh}) | '
        f'timeline="{tl.get("name")}" ({tl.get("duration_frames")}f) | playhead={tl.get("playhead_tc")} | '
        f'tracks={tl.get("video_tracks")}V/{tl.get("audio_tracks")}A]{op_marker_str}'
    )


async def format_live_timeline_state(transport: ResolveTransport | None) -> str | None:
    """Build a compact (~350-600 tok) ephemeral summary of the active timeline attached ONLY to the
    latest outgoing message (`to_messages()[-1]`).

    Preserves 100% prompt-prefix caching on all historical Ledger segments, prevents stale
    `timeline.get_state` JSON dumps from accumulating across turns, and eliminates extra
    discovery round-trips after edits!
    """
    import json

    if transport is None:
        return None
    try:
        tl_st = await transport.call(
            "timeline.get_state",
            {"track_types": ["video", "audio", "subtitle"], "include_properties": True},
            timeout=2.5,
        )
    except Exception:  # noqa: BLE001
        return None

    tracks = tl_st.get("tracks") or {}
    sub_tracks = tracks.get("subtitle") or []
    sub_desc = (
        ", ".join(
            f"ST{t['track_index']} \"{t['name']}\" ({'on' if t['enabled'] else 'off'}, {t['item_count']} cues)"
            for t in sub_tracks
        )
        if sub_tracks
        else "none"
    )

    lines = [
        "[Live Timeline State — authoritative current tracks & item_ids; do NOT call `timeline.get_state` unless inspecting individual subtitle cues]",
        (
            f"Timeline: \"{tl_st.get('name')}\" | fps={tl_st.get('fps')} | "
            f"frames={tl_st.get('start_frame')}..{tl_st.get('end_frame')} ({tl_st.get('duration_frames')}f) | "
            f"playhead={tl_st.get('playhead_tc')} | markers={tl_st.get('markers_count')} | subtitles={sub_desc}"
        ),
    ]

    for ttype, prefix in (("video", "V"), ("audio", "A")):
        for tr in tracks.get(ttype) or []:
            t_state = "on" if tr.get("enabled", True) else "MUTED/OFF"
            if tr.get("locked"):
                t_state += ",LOCKED"
            items = tr.get("items") or []
            lines.append(f"  {prefix}{tr['track_index']} \"{tr['name']}\" ({t_state}, {len(items)} items):")
            if not items:
                continue
            # Collapse disabled/muted tracks with >6 items into 1 line to save tokens
            if not tr.get("enabled", True) and len(items) > 6:
                lines.append(
                    f"    • [{len(items)} muted items spanning {items[0]['record_start_tc']}..{items[-1]['record_end_tc']}]"
                )
                continue
            show_items = items if len(items) <= 30 else [*items[:15], None, *items[-10:]]
            for it in show_items:
                if it is None:
                    lines.append(f"    • … ({len(items) - 25} middle items omitted) …")
                    continue
                props = it.get("properties") or {}
                prop_s = f" props={json.dumps(props, separators=(',', ':'))}" if props else ""
                dis_s = " [DISABLED]" if not it.get("enabled", True) else ""
                lines.append(
                    f"    • id={it['item_id']} \"{it['name']}\" rec={it['record_start_tc']}..{it['record_end_tc']} "
                    f"({it['duration']}f) src={it['source_start']}..{it['source_end']}{dis_s}{prop_s}"
                )

    return "\n".join(lines)