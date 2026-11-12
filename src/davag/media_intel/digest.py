"""Persistent Clip Digest cache & Scout-Model Video/Storyboard Analyzer (`digest.py`).

Lets a fast, cheap multimodal "Scout" model (default: `gemini / gemini-3.8-flash`, configurable via
`DAVAG_INDEX_PROVIDER` / `DAVAG_INDEX_MODEL`) inspect burned-in-timecode contact sheets, scene cuts,
speech segments, and/or QSV video proxies, then persist a frame-accurate editorial summary in
`~/.davag/cache/digests/` so the Lead Editor (`claude-opus-5-5` / `gpt-5.1`) gets rich clip
understanding in ~400 tokens instead of 500,000+ tokens!
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

from davag.config.env import Config
from davag.context.media import load_image_part, load_video_part
from davag.providers import create_provider

from .ffmpeg import (
    detect_scene_cuts,
    detect_speech_segments,
    extract_proxy_clip,
    generate_contact_sheet,
    probe_media,
)

SCOUT_SYSTEM_PROMPT = """You are a precision assistant video editor preparing a frame-accurate Clip Digest for a Lead Editor in DaVinci Resolve.

Given a clip's technical metadata, visual contact sheet(s) with burned-in timecode/frame labels, scene-cut timestamps, and/or proxy video:
1. Write a 2-sentence `overview` of what the footage shows (subject, setting, camera style, audio content).
2. Break the footage into chronological `chapters` with exact `[start_sec, end_sec]`, `[start_frame, end_frame]`, visual description, spoken/action highlights, and editorial rating (`select`, `usable`, `dead_air`, `cut`).
3. Note any technical/editorial warnings (exposure shifts, long dead-air stretches, mic issues, screen-share transitions).

Respond with concise, structured markdown containing exact timecodes (`HH:MM:SS:FF`) and source frame numbers (`fNNNN`) read directly from the burned-in tile labels and metadata."""


def _digest_key(file_path: str | Path) -> str:
    p = Path(file_path).resolve()
    try:
        st = p.stat()
        raw = f"{p}|{st.st_size}|{st.st_mtime_ns}"
    except OSError:
        raw = str(p)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def digest_file_for(cache_dir: Path, file_path: str | Path) -> Path:
    d_dir = cache_dir / "digests"
    d_dir.mkdir(parents=True, exist_ok=True)
    return d_dir / f"{_digest_key(file_path)}.json"


def load_clip_digest(cache_dir: Path, file_path: str | Path) -> dict[str, Any] | None:
    if not file_path:
        return None
    f = digest_file_for(cache_dir, file_path)
    if not f.is_file():
        return None
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def save_clip_digest(
    cache_dir: Path,
    file_path: str | Path,
    *,
    summary: str,
    clip_id: str | None = None,
    scenes: list[dict[str, Any]] | None = None,
    contact_sheet_path: str | None = None,
    scout_model: str | None = None,
) -> dict[str, Any]:
    p = Path(file_path).resolve()
    f = digest_file_for(cache_dir, p)
    payload: dict[str, Any] = {
        "file_path": str(p),
        "clip_name": p.name,
        "clip_id": clip_id,
        "summary": summary.strip(),
        "scenes": scenes or [],
        "contact_sheet_path": contact_sheet_path,
        "scout_model": scout_model,
        "updated_at": time.time(),
    }
    f.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return payload


def pick_scout_provider_and_model(cfg: Config) -> tuple[str, str]:
    """Choose the optimal fast multimodal Scout model from configured keys.

    1. Explicit `cfg.index_provider` / `cfg.index_model` (`DAVAG_INDEX_PROVIDER` / `DAVAG_INDEX_MODEL`)
    2. `gemini` / `gemini-3.8-flash` if Gemini key is configured (native video + cheap vision!)
    3. `anthropic` / `claude-haiku-5-5` if Anthropic key is configured
    4. `openai` / `gpt-5-mini` if OpenAI key is configured
    5. Fall back to the session's active `cfg.provider` / `cfg.model.id`
    """
    idx_prov = getattr(cfg, "index_provider", None)
    idx_mod = getattr(cfg, "index_model", None)
    if idx_prov and cfg.keys.get(idx_prov):
        default_for = {
            "gemini": "gemini-3.8-flash",
            "anthropic": "claude-haiku-5-5",
            "openai": "gpt-5-mini",
            "openrouter": "google/gemini-2.5-pro",
            "grok": "grok-4",
        }
        return idx_prov, idx_mod or default_for.get(idx_prov, cfg.model.id)

    if cfg.keys.get("gemini"):
        return "gemini", idx_mod or "gemini-3.8-flash"
    if cfg.keys.get("anthropic"):
        return "anthropic", idx_mod or "claude-haiku-5-5"
    if cfg.keys.get("openai"):
        return "openai", idx_mod or "gpt-5-mini"
    return cfg.provider, idx_mod or cfg.model.id


async def analyze_clip_with_scout(
    file_path: str | Path,
    *,
    cfg: Config,
    cache_dir: Path,
    clip_id: str | None = None,
    start_sec: float | None = None,
    duration_sec: float | None = None,
    cols: int = 4,
    rows: int = 4,
    include_video_proxy: bool = False,
    force_refresh: bool = False,
    question: str | None = None,
) -> dict[str, Any]:
    """Run the full Scout pipeline on a clip (or sub-window) and cache/return its Digest."""
    import asyncio
    from dataclasses import replace

    src = Path(file_path).resolve()
    is_full_clip = start_sec is None and duration_sec is None and not question
    if is_full_clip and not force_refresh:
        existing = load_clip_digest(cache_dir, src)
        if existing:
            return {**existing, "cached": True}

    loop = asyncio.get_running_loop()
    info = await loop.run_in_executor(None, probe_media, src)
    dur_total = float(info["duration_sec"])

    # 1. Always generate a burned-in-timecode Contact Sheet (fast & works across all vision models)
    sheets_dir = cache_dir / "contact_sheets"
    sheets_dir.mkdir(parents=True, exist_ok=True)
    win_tag = f"_{int(start_sec or 0)}_{int(duration_sec or dur_total)}"
    sheet_out = sheets_dir / f"{_digest_key(src)}{win_tag}_{cols}x{rows}.png"

    sheet = await loop.run_in_executor(
        None,
        lambda: generate_contact_sheet(
            src,
            output_path=sheet_out,
            start_sec=start_sec,
            duration_sec=duration_sec,
            cols=cols,
            rows=rows,
        ),
    )

    # 2. Fast scene-cut & speech summary (bounded to <=600s window so 3hr files stay snappy)
    analysis_dur = duration_sec if duration_sec is not None else min(dur_total, 600.0)
    scenes_res = await loop.run_in_executor(
        None,
        lambda: detect_scene_cuts(
            src, start_sec=start_sec or 0.0, duration_sec=analysis_dur, max_cuts=40
        ),
    )
    speech_res = await loop.run_in_executor(
        None,
        lambda: detect_speech_segments(src, min_silence_sec=0.8),
    ) if dur_total <= 900 else None

    scout_prov, scout_model_id = pick_scout_provider_and_model(cfg)
    from davag.config.models import resolve_model

    scout_spec = resolve_model(scout_prov, scout_model_id)
    scout_cfg = replace(cfg, provider=scout_prov, model=scout_spec, max_tokens=4096, effort="medium")
    adapter = create_provider(scout_cfg)

    content_parts: list[dict[str, Any]] = []
    img_part = load_image_part(sheet["path"])
    content_parts.append(
        {
            "type": "image",
            "media_type": img_part["media_type"],
            "data": img_part["data"],
        }
    )

    proxy_path: str | None = None
    # If Scout is Gemini and video proxy requested (or short window <= 180s), attach QSV video + audio!
    eff_win = duration_sec if duration_sec is not None else dur_total
    if scout_spec.video and (include_video_proxy or eff_win <= 180.0):
        clip_dur = min(eff_win, 300.0)
        proxy = await loop.run_in_executor(
            None,
            lambda: extract_proxy_clip(
                src, start_sec=start_sec, duration_sec=clip_dur, height=360, fps=8, video_kbps=300, prefer_av1=True
            ),
        )
        proxy_path = proxy["path"]
        vid_part = load_video_part(proxy_path)
        if vid_part.get("file_path"):
            content_parts.append({"type": "video", "media_type": vid_part["media_type"], "file_path": vid_part["file_path"]})
        else:
            content_parts.append({"type": "video", "media_type": vid_part["media_type"], "data": vid_part["data"]})

    meta_lines = [
        f"Clip: {src.name} (clip_id={clip_id or 'unknown'})",
        f"Technical: {json.dumps(info.get('video'))} | duration={dur_total:.2f}s",
        f"Contact sheet window: start={sheet['start_sec']}s, duration={sheet['duration_sec']}s, grid={cols}x{rows} ({sheet['tile_count']} tiles, interval={sheet['interval_sec']}s)",
        f"Visual scene cuts detected (first {analysis_dur:.0f}s): {json.dumps(scenes_res.get('cuts', [])[:25])}",
    ]
    if speech_res is not None:
        meta_lines.append(
            f"Audio activity: kept_frames={speech_res['kept_frames']}/{speech_res['total_frames']}, "
            f"silences={speech_res['silence_count']}"
        )
    if question:
        meta_lines.append(f"\nSpecific Lead Editor question to answer: {question}")

    content_parts.append({"type": "text", "text": "\n".join(meta_lines)})

    chunks: list[str] = []
    async for ev in adapter.send(
        system=SCOUT_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": content_parts}],
    ):
        if ev.t == "text":
            chunks.append(ev.delta)

    digest_text = "".join(chunks).strip()
    if is_full_clip:
        saved = save_clip_digest(
            cache_dir,
            src,
            summary=digest_text,
            clip_id=clip_id,
            scenes=scenes_res.get("cuts", []),
            contact_sheet_path=sheet["path"],
            scout_model=f"{scout_prov}/{scout_model_id}",
        )
        return {**saved, "cached": False, "proxy_path": proxy_path}

    return {
        "file_path": str(src),
        "clip_id": clip_id,
        "window": {"start_sec": sheet["start_sec"], "duration_sec": sheet["duration_sec"]},
        "scout_model": f"{scout_prov}/{scout_model_id}",
        "contact_sheet_path": sheet["path"],
        "proxy_path": proxy_path,
        "summary": digest_text,
        "scenes": scenes_res.get("cuts", []),
        "cached": False,
    }