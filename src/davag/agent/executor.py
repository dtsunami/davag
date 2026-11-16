"""Two-phase `dr_ops` batch planner & executor (`davag.agent.executor`).

Supports:
- Resolve domain ops (`davag.resolve_ops`)
- Media Intelligence ops (`media.probe`, `media.contact_sheet`, `media.scene_cuts`,
  `media.detect_silence`, `media.proxy_clip`, `media.generate_resolve_proxy`,
  `media.analyze_clip` [Scout model], `media.save_digest`, `media.transcribe`, `otio.build`)
  with automatic `clip_id` -> Media Pool file path resolution so clips outside `cwd` work seamlessly!
- Workspace filesystem ops (`fs.read`, `fs.write`, `fs.replace`, `fs.list`)
- Segregated approval gate for `resolve.eval`, `shell`, and `destructive` ops.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import subprocess
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from davag.config.env import Config
from davag.media_intel import (
    CutSegment,
    analyze_clip_with_scout,
    build_timeline_otio,
    detect_scene_cuts,
    detect_speech_segments,
    extract_proxy_clip,
    generate_contact_sheet,
    generate_editing_proxy,
    master_voiceover_audio,
    probe_media,
    save_clip_digest,
)
from davag.media_intel.transcribe import transcribe_media
from davag.resolve_ops import OpError, get_op
from davag.transport.base import ResolveTransport

from .journal import FileEntry, Journal, ResolveCheckpoint
from .sandbox import Sandbox, SandboxError

FS_OPS = ("fs.read", "fs.write", "fs.replace", "fs.list")
MEDIA_OPS = (
    "media.probe",
    "media.contact_sheet",
    "media.scene_cuts",
    "media.detect_silence",
    "media.proxy_clip",
    "media.generate_resolve_proxy",
    "media.analyze_clip",
    "media.save_digest",
    "media.transcribe",
    "media.master_vo",
    "motion.render_overlay",
    "motion.qr_callout",
    "youtube.thumbnail",
    "youtube.stage",
    "otio.build",
)
GATED_TIERS = ("eval", "destructive")


@dataclass
class OpCall:
    index: int
    op: str
    params: dict[str, Any]


@dataclass
class ApprovalDecision:
    allow: bool
    reason: str | None = None
    checkpoint_first: bool = False


ApproveHook = Callable[[OpCall], Awaitable[ApprovalDecision]]


@dataclass
class BatchOutcome:
    status: str  # "ok" | "plan-failed" | "apply-failed" | "denied"
    results: list[dict[str, Any]] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    checkpoint: ResolveCheckpoint | None = None
    attached_media: list[dict[str, Any]] = field(default_factory=list)


def _plan_fs_op(call: OpCall, sandbox: Sandbox) -> str | None:
    p = call.params
    if call.op == "fs.read":
        if not p.get("path"):
            return "fs.read requires 'path'"
        target = sandbox.resolve_for_read(str(p["path"]))
        if not target.is_file():
            return f"{sandbox.rel(target)} does not exist"
    elif call.op == "fs.list":
        target = sandbox.resolve_for_read(str(p.get("path", ".")))
        if not target.is_dir():
            return f"{sandbox.rel(target)} is not a directory"
    elif call.op == "fs.write":
        if not p.get("path") or "text" not in p:
            return "fs.write requires 'path' and 'text'"
        target = sandbox.resolve_for_write(str(p["path"]))
        if target.exists() and not bool(p.get("overwrite", False)):
            return f"{sandbox.rel(target)} already exists (pass overwrite: true or use fs.replace)"
    elif call.op == "fs.replace":
        if not p.get("path") or "old" not in p or "new" not in p:
            return "fs.replace requires 'path', 'old', and 'new'"
        target = sandbox.resolve_for_write(str(p["path"]))
        if not target.is_file():
            return f"{sandbox.rel(target)} does not exist"
        if str(p["old"]) == "":
            return "fs.replace 'old' cannot be empty"
        content = target.read_text(encoding="utf-8")
        count = content.count(str(p["old"]))
        if count == 0:
            return f"'old' text not found in {sandbox.rel(target)}"
        if count > 1 and not bool(p.get("all", False)):
            return f"'old' text occurs {count} times in {sandbox.rel(target)} (include context or set all: true)"
    return None


def plan_batch(
    ops: list[OpCall],
    *,
    sandbox: Sandbox,
    has_transport: bool,
    resolve_media_paths: set[Path] | None = None,
    role: str = "master",
) -> list[dict[str, Any]]:
    from .roles import is_op_allowed_for_role

    errors: list[dict[str, Any]] = []
    allowed_media = resolve_media_paths or set()

    for call in ops:
        spec = get_op(call.op)
        if not is_op_allowed_for_role(role, call.op, spec.tier if spec else None):
            errors.append(
                {
                    "index": call.index,
                    "op": call.op,
                    "message": f'op "{call.op}" is not permitted under active role "{role}" (switch via `/role master`)',
                }
            )
            continue

        try:
            if call.op == "shell":
                if not str(call.params.get("cmd", "")).strip():
                    errors.append({"index": call.index, "op": call.op, "message": "shell requires 'cmd'"})
            elif call.op in ("youtube.upload", "youtube.update"):
                if not call.params.get("manifest_path"):
                    errors.append({"index": call.index, "op": call.op, "message": "youtube.upload requires 'manifest_path'"})
                else:
                    sandbox.resolve_for_read(str(call.params["manifest_path"]))
            elif call.op in FS_OPS:
                err = _plan_fs_op(call, sandbox)
                if err:
                    errors.append({"index": call.index, "op": call.op, "message": err})
            elif call.op in MEDIA_OPS:
                p = call.params
                if call.op == "otio.build":
                    if not p.get("output_path") or not isinstance(p.get("video_segments"), list):
                        errors.append(
                            {
                                "index": call.index,
                                "op": call.op,
                                "message": "otio.build requires 'output_path' and list 'video_segments'",
                            }
                        )
                    else:
                        sandbox.resolve_for_write(str(p["output_path"]))
                elif call.op == "motion.qr_callout":
                    if not p.get("url"):
                        errors.append({"index": call.index, "op": call.op, "message": "motion.qr_callout requires 'url'"})
                elif call.op == "motion.render_overlay":
                    if not p.get("html") and not p.get("path"):
                        errors.append(
                            {"index": call.index, "op": call.op, "message": "motion.render_overlay requires 'html' (or 'path')"}
                        )
                elif call.op == "youtube.thumbnail":
                    if not p.get("prompt"):
                        errors.append({"index": call.index, "op": call.op, "message": "youtube.thumbnail requires 'prompt'"})
                elif call.op == "youtube.stage":
                    if not has_transport:
                        errors.append({"index": call.index, "op": call.op, "message": "youtube.stage requires DaVinci Resolve"})
                    elif not p.get("title"):
                        errors.append({"index": call.index, "op": call.op, "message": "youtube.stage requires 'title'"})
                elif call.op == "media.save_digest" and not p.get("summary"):
                    errors.append({"index": call.index, "op": call.op, "message": "media.save_digest requires 'summary'"})
                elif p.get("clip_id") or p.get("clip_name"):
                    if not has_transport:
                        errors.append(
                            {
                                "index": call.index,
                                "op": call.op,
                                "message": f"{call.op} with clip_id/clip_name requires DaVinci Resolve connection",
                            }
                        )
                elif p.get("path"):
                    cand = Path(str(p["path"])).resolve()
                    if cand not in allowed_media:
                        sandbox.resolve_for_read(str(p["path"]))
                else:
                    errors.append(
                        {
                            "index": call.index,
                            "op": call.op,
                            "message": f"{call.op} requires 'path' or 'clip_id'",
                        }
                    )
            else:
                spec = get_op(call.op)
                if spec is None:
                    errors.append(
                        {"index": call.index, "op": call.op, "message": f'unknown op "{call.op}"'}
                    )
                elif not has_transport:
                    errors.append(
                        {
                            "index": call.index,
                            "op": call.op,
                            "message": "DaVinci Resolve is not connected (start Resolve or run `/resolve`)",
                        }
                    )
        except SandboxError as s_err:
            errors.append({"index": call.index, "op": call.op, "message": str(s_err)})
    return errors


async def _fetch_resolve_clip_map(transport: ResolveTransport | None) -> tuple[dict[str, dict[str, Any]], set[Path]]:
    """Map `clip_id` & `name` -> clip dict, and collect all Media Pool file paths for read allowlisting."""
    if transport is None:
        return {}, set()
    try:
        res = await transport.call("mediapool.clips", {"recursive": True}, timeout=3.0)
        by_key: dict[str, dict[str, Any]] = {}
        paths: set[Path] = set()
        for c in res.get("clips") or []:
            if c.get("clip_id"):
                by_key[str(c["clip_id"])] = c
            if c.get("name"):
                by_key[str(c["name"])] = c
            fp = str(c.get("file_path") or "").strip()
            if fp:
                with contextlib.suppress(OSError):
                    paths.add(Path(fp).resolve())
        return by_key, paths
    except Exception:  # noqa: BLE001
        return {}, set()


def _resolve_media_source(
    p: dict[str, Any],
    sandbox: Sandbox,
    clip_map: dict[str, dict[str, Any]],
    allowed_media: set[Path],
) -> tuple[Path, str | None]:
    """Resolve `clip_id` / `clip_name` / `path` into `(abs_file_path, clip_id)`."""
    cid = str(p.get("clip_id") or "").strip()
    cname = str(p.get("clip_name") or "").strip()
    if cid or cname:
        entry = clip_map.get(cid) or clip_map.get(cname)
        if not entry or not entry.get("file_path"):
            raise OpError(f'Media Pool clip "{cid or cname}" has no on-disk file path', code="CLIP_NOT_FOUND")
        return Path(str(entry["file_path"])).resolve(), str(entry.get("clip_id") or cid or None)

    raw_p = str(p["path"])
    cand = Path(raw_p).resolve()
    if cand in allowed_media:
        matched_id = next(
            (str(v["clip_id"]) for v in clip_map.values() if v.get("file_path") and Path(str(v["file_path"])).resolve() == cand),
            None,
        )
        return cand, matched_id
    return sandbox.resolve_for_read(raw_p), None


def _exec_fs_op(call: OpCall, sandbox: Sandbox, journal: Journal) -> tuple[FileEntry | None, dict[str, Any]]:
    p = call.params
    if call.op == "fs.read":
        target = sandbox.resolve_for_read(str(p["path"]))
        lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
        line_range = p.get("lines")
        if isinstance(line_range, str) and "-" in line_range:
            parts = line_range.split("-", 1)
            if parts[0].strip().isdigit() and parts[1].strip().isdigit():
                line_range = [int(parts[0].strip()), int(parts[1].strip())]
        if isinstance(line_range, list) and len(line_range) == 2:
            lo = max(1, int(line_range[0]))
            hi = min(len(lines), int(line_range[1]))
        else:
            lo, hi = 1, min(len(lines), 400)
        sliced = lines[lo - 1 : hi]
        return None, {
            "path": sandbox.rel(target),
            "lines": [lo, hi],
            "total_lines": len(lines),
            "truncated": hi < len(lines) and line_range is None,
            "text": "\n".join(sliced),
        }

    if call.op == "fs.list":
        target = sandbox.resolve_for_read(str(p.get("path", ".")))
        max_items = int(p.get("max", 200))
        entries = []
        for child in sorted(target.iterdir(), key=lambda c: (not c.is_dir(), c.name.lower())):
            if sandbox.is_ignored(child):
                continue
            entries.append(f"{child.name}/" if child.is_dir() else f"{child.name} ({child.stat().st_size}b)")
            if len(entries) >= max_items:
                break
        return None, {"path": sandbox.rel(target), "entries": entries}

    if call.op == "fs.write":
        target = sandbox.resolve_for_write(str(p["path"]))
        entry = journal.snapshot_file(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f"{target.name}.davag-tmp")
        text = str(p["text"])
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, target)
        return entry, {
            "path": sandbox.rel(target),
            "action": "overwritten" if entry.existed else "created",
            "bytes": len(text.encode("utf-8")),
        }

    if call.op == "fs.replace":
        target = sandbox.resolve_for_write(str(p["path"]))
        entry = journal.snapshot_file(target)
        raw = target.read_text(encoding="utf-8")
        old_s = str(p["old"])
        new_s = str(p["new"])
        replaced = raw.replace(old_s, new_s) if bool(p.get("all", False)) else raw.replace(old_s, new_s, 1)
        tmp = target.with_name(f"{target.name}.davag-tmp")
        tmp.write_text(replaced, encoding="utf-8")
        os.replace(tmp, target)
        return entry, {"path": sandbox.rel(target), "action": "replaced"}

    raise ValueError(f"Unhandled fs op: {call.op}")


async def _exec_media_op(
    call: OpCall,
    sandbox: Sandbox,
    *,
    cache_dir: Path,
    cfg: Config | None,
    openai_api_key: str | None,
    transport: ResolveTransport | None,
    clip_map: dict[str, dict[str, Any]],
    allowed_media: set[Path],
) -> dict[str, Any]:
    p = call.params
    loop = asyncio.get_running_loop()

    if call.op == "otio.build":
        out_p = sandbox.resolve_for_write(str(p["output_path"]))

        def _seg_path(raw_mp: str) -> str:
            cp = Path(raw_mp).resolve()
            return str(cp) if cp in allowed_media else str(sandbox.resolve_for_read(raw_mp))

        v_segs = [
            CutSegment(
                name=str(s.get("name", "Clip")),
                media_path=_seg_path(str(s["media_path"])),
                start_frame=int(s["start_frame"]),
                duration_frames=int(s["duration_frames"]),
                source_fps=float(s.get("source_fps", p.get("fps", 24.0))),
                track_index=int(s.get("track_index", 1)),
            )
            for s in p["video_segments"]
        ]
        a_segs = [
            CutSegment(
                name=str(s.get("name", "Audio")),
                media_path=_seg_path(str(s["media_path"])),
                start_frame=int(s["start_frame"]),
                duration_frames=int(s["duration_frames"]),
                source_fps=float(s.get("source_fps", p.get("fps", 24.0))),
                track_index=int(s.get("track_index", 1)),
            )
            for s in (p.get("audio_segments") or [])
        ]
        return build_timeline_otio(
            timeline_name=str(p.get("timeline_name", "davag Cut")),
            fps=float(p.get("fps", 24.0)),
            video_segments=v_segs,
            audio_segments=a_segs,
            output_path=out_p,
        )

    if call.op == "motion.qr_callout":
        from davag.youtube.qr import build_qr_callout_html

        title_txt = str(p.get("title") or "Explore the Code & Demo")
        url_txt = str(p["url"]).strip()
        qr_html = build_qr_callout_html(
            title=title_txt,
            url=url_txt,
            kicker=str(p.get("kicker") or "SCAN OR CLICK LINK IN DESCRIPTION"),
            position=str(p.get("position") or "bottom-right"),
            accent=str(p.get("accent") or "#4cc9f0"),
        )
        if transport is not None and p.get("record_tc") and bool(p.get("add_marker", True)):
            with contextlib.suppress(Exception):
                await transport.call(
                    "marker.add",
                    {
                        "tc": str(p["record_tc"]),
                        "color": "Purple",
                        "name": title_txt,
                        "note": url_txt,
                        "custom_data": "davag:link",
                    },
                )
        delegated = OpCall(
            index=call.index,
            op="motion.render_overlay",
            params={
                "name": p.get("name") or f"qr_{re.sub(r'[^a-zA-Z0-9]+', '_', title_txt).strip('_').lower()[:28]}",
                "html": qr_html,
                "duration_sec": float(p.get("duration_sec", 6.5)),
                "static": bool(p.get("static", False)),
                **({"codec": p["codec"]} if p.get("codec") else {}),
                **({"record_tc": p["record_tc"]} if p.get("record_tc") else {}),
                **({"track_index": p["track_index"]} if p.get("track_index") is not None else {}),
                **({"output_path": p["output_path"]} if p.get("output_path") else {}),
            },
        )
        return await _exec_media_op(
            delegated,
            sandbox,
            cache_dir=cache_dir,
            cfg=cfg,
            openai_api_key=openai_api_key,
            transport=transport,
            clip_map=clip_map,
            allowed_media=allowed_media,
        )

    if call.op == "motion.render_overlay":
        from davag.media_intel.motion import render_html_svg_overlay

        html_src = str(p["html"]) if p.get("html") else sandbox.resolve_for_read(str(p["path"])).read_text(encoding="utf-8")
        name_slug = re.sub(r"[^a-zA-Z0-9_-]+", "_", str(p.get("name") or "overlay")).strip("_") or "overlay"
        out_default = sandbox.cwd / "overlays" / f"{name_slug}.mov"
        out_p = sandbox.resolve_for_write(str(p["output_path"])) if p.get("output_path") else out_default

        # Match open Resolve project FPS/resolution automatically unless overridden!
        width = int(p.get("width", 1920))
        height = int(p.get("height", 1080))
        fps = float(p.get("fps", 30.0))
        if transport is not None and ("width" not in p or "fps" not in p):
            with contextlib.suppress(Exception):
                st = await transport.call("resolve.status", timeout=2.0)
                proj = st.get("project") or {}
                if "width" not in p and proj.get("width"):
                    width = int(proj["width"])
                    height = int(proj["height"])
                if "fps" not in p and proj.get("fps"):
                    fps = float(proj["fps"])

        with contextlib.suppress(OSError):
            out_p.parent.mkdir(parents=True, exist_ok=True)
            out_p.with_suffix(".html").write_text(html_src, encoding="utf-8")

        res = await render_html_svg_overlay(
            html_or_svg=html_src,
            output_path=out_p,
            width=width,
            height=height,
            fps=fps,
            duration_sec=float(p.get("duration_sec", 4.0)),
            static=bool(p.get("static", False)),
            codec=str(p.get("codec", "png")),
        )

        # Optionally import into Resolve `Overlays` bin & place onto an unobstructed Overlay Video track!
        if transport is not None and (p.get("record_tc") or p.get("insert_on_timeline")):
            from davag.resolve_ops.helpers import tc_to_frames

            imp = await transport.call("mediapool.import", {"paths": [res["path"]], "bin_path": "Overlays"})
            imp_clips = imp.get("clips") or []
            if imp_clips:
                ov_cid = imp_clips[0]["clip_id"]
                explicit_track = p.get("track_index") is not None
                target_track = int(p.get("track_index", 2))
                tl_st = await transport.call("timeline.get_state", {"track_types": ["video"]})
                v_tracks = tl_st.get("tracks", {}).get("video", [])
                tl_fps = float(tl_st.get("fps") or fps)
                ov_frames = max(1, int(res.get("frames") or 1))
                rec_f = tc_to_frames(str(p["record_tc"]), tl_fps) if p.get("record_tc") else None

                # If track_index wasn't explicitly specified and Video 2 is occupied (e.g. by a Facecam PiP),
                # automatically bump to the next track so `AppendToTimeline` never silently collides!
                if rec_f is not None and not explicit_track:
                    while target_track <= len(v_tracks):
                        tr_items = v_tracks[target_track - 1].get("items") or []
                        has_overlap = any(
                            int(it.get("record_start", 0)) < rec_f + ov_frames
                            and int(it.get("record_end", 0)) > rec_f
                            for it in tr_items
                        )
                        if not has_overlap:
                            break
                        target_track += 1

                v_count = len(v_tracks)
                while v_count < target_track:
                    await transport.call("timeline.track_state", {"track_type": "video", "add_track": True, "name": "Overlays"})
                    v_count += 1

                # If an explicit track has an older overlay item at the same window, remove it before placing the update
                if rec_f is not None and explicit_track and target_track <= len(v_tracks):
                    stale_ids = [
                        it["item_id"]
                        for it in (v_tracks[target_track - 1].get("items") or [])
                        if it.get("item_id")
                        and int(it.get("record_start", 0)) < rec_f + ov_frames
                        and int(it.get("record_end", 0)) > rec_f
                        and int(it.get("duration", 0)) <= max(ov_frames * 3, 900)
                    ]
                    if stale_ids:
                        await transport.call("timeline.delete_clips", {"item_ids": stale_ids, "ripple": False})

                app_spec: dict[str, Any] = {
                    "clip_id": ov_cid,
                    "track_index": target_track,
                    "media_type": 1,
                }
                if not bool(p.get("static", False)) and ov_frames > 1:
                    app_spec["start_frame"] = 0
                    app_spec["end_frame"] = ov_frames - 1  # Resolve AppendToTimeline `endFrame` is 0-indexed inclusive!
                if p.get("record_tc"):
                    app_spec["record_tc"] = str(p["record_tc"])
                app_res = await transport.call("timeline.append_clips", {"clips": [app_spec]})
                placed = (app_res.get("items") or [{}])[0]
                res["inserted_clip_id"] = ov_cid
                res["inserted_item_id"] = placed.get("item_id") or None
                res["inserted_ok"] = bool(placed.get("item_id"))
                res["inserted_track"] = target_track
                res["inserted_tc"] = p.get("record_tc")
        return res

    if call.op == "youtube.thumbnail":
        if cfg is None:
            raise RuntimeError("youtube.thumbnail requires session Config")
        from davag.youtube.studio import generate_youtube_thumbnail

        refs: list[Path] = []
        for r_item in p.get("reference_images") or []:
            if str(r_item) == "grab_current" and transport is not None:
                grab = await transport.call("timeline.grab_still", {"attach_to_context": False})
                refs.append(Path(str(grab["path"])))
            else:
                rp = Path(str(r_item)).resolve()
                refs.append(rp if rp in allowed_media else sandbox.resolve_for_read(str(r_item)))
        if p.get("grab_tc") and transport is not None:
            grab = await transport.call("timeline.grab_still", {"tc": str(p["grab_tc"]), "attach_to_context": False})
            refs.append(Path(str(grab["path"])))

        out_p = (
            sandbox.resolve_for_write(str(p["output_path"]))
            if p.get("output_path")
            else (sandbox.cwd / "youtube_staging" / "thumbnail.png")
        )
        res = await generate_youtube_thumbnail(
            prompt=str(p["prompt"]),
            output_path=out_p,
            cfg=cfg,
            reference_images=refs,
            model_override=str(p["model"]) if p.get("model") else None,
            aspect_ratio=str(p.get("aspect_ratio", "16:9")),
        )
        res["attach_to_context"] = bool(p.get("attach_to_context", True))
        return res

    if call.op == "youtube.stage":
        assert transport is not None
        from davag.youtube.studio import stage_youtube_package

        return await stage_youtube_package(
            transport=transport,
            staging_root=sandbox.cwd / "youtube_staging",
            title=str(p["title"]),
            description_intro=str(p.get("description") or ""),
            tags=list(p.get("tags") or []),
            privacy_status=str(p.get("privacy_status", "private")),
            category_id=str(p.get("category_id", "28")),
            thumbnail_path=(
                str(Path(str(p["thumbnail_path"])).resolve())
                if p.get("thumbnail_path") and Path(str(p["thumbnail_path"])).resolve() in allowed_media
                else (str(sandbox.resolve_for_read(str(p["thumbnail_path"]))) if p.get("thumbnail_path") else None)
            ),
            primary_url=str(p["primary_url"]) if p.get("primary_url") else None,
            first_comment=str(p["first_comment"]) if p.get("first_comment") else None,
            render_qr_markers=bool(p.get("render_qr_markers", True)),
            append_end_screen=bool(p.get("append_end_screen", True)),
            end_screen_sec=float(p.get("end_screen_sec", 8.0)),
            queue_render=bool(p.get("queue_render", True)),
            start_render=bool(p.get("start_render", False)),
        )

    src, resolved_clip_id = _resolve_media_source(p, sandbox, clip_map, allowed_media)

    if call.op == "media.probe":
        return await loop.run_in_executor(None, probe_media, src)

    if call.op == "media.contact_sheet":
        out_p = sandbox.resolve_for_write(str(p["output_path"])) if p.get("output_path") else None
        res = await loop.run_in_executor(
            None,
            lambda: generate_contact_sheet(
                src,
                output_path=out_p,
                start_sec=float(p["start_sec"]) if p.get("start_sec") is not None else None,
                duration_sec=float(p["duration_sec"]) if p.get("duration_sec") is not None else None,
                cols=int(p.get("cols", 4)),
                rows=int(p.get("rows", 4)),
                tile_width=int(p.get("tile_width", 384)),
            ),
        )
        res["attach_to_context"] = bool(p.get("attach_to_context", True))
        return res

    if call.op == "media.scene_cuts":
        return await loop.run_in_executor(
            None,
            lambda: detect_scene_cuts(
                src,
                threshold=float(p.get("threshold", 0.30)),
                start_sec=float(p.get("start_sec", 0.0)),
                duration_sec=float(p["duration_sec"]) if p.get("duration_sec") is not None else None,
                max_cuts=int(p.get("max_cuts", 60)),
            ),
        )

    if call.op == "media.detect_silence":
        return await loop.run_in_executor(
            None,
            lambda: detect_speech_segments(
                src,
                noise_db=float(p.get("noise_db", -35.0)),
                min_silence_sec=float(p.get("min_silence_sec", 0.45)),
                pad_sec=float(p.get("pad_sec", 0.08)),
                fps_override=p.get("fps"),
            ),
        )

    if call.op == "media.proxy_clip":
        out_p = sandbox.resolve_for_write(str(p["output_path"])) if p.get("output_path") else None
        res = await loop.run_in_executor(
            None,
            lambda: extract_proxy_clip(
                src,
                output_path=out_p,
                start_sec=float(p["start_sec"]) if p.get("start_sec") is not None else None,
                duration_sec=float(p["duration_sec"]) if p.get("duration_sec") is not None else None,
                height=int(p.get("height", 480)),
                fps=int(p.get("fps", 12)),
                video_kbps=int(p.get("video_kbps", 450)),
                prefer_av1=bool(p.get("prefer_av1", False)),
            ),
        )
        res["attach_to_context"] = bool(p.get("attach_to_context", True))
        return res

    if call.op == "media.generate_resolve_proxy":
        out_p = sandbox.resolve_for_write(str(p["output_path"])) if p.get("output_path") else None
        proxy_dir = cache_dir / "resolve_proxies"
        proxy_dir.mkdir(parents=True, exist_ok=True)
        default_dst = proxy_dir / f"{src.stem}_720p_proxy.mp4"
        res = await loop.run_in_executor(
            None,
            lambda: generate_editing_proxy(
                src,
                output_path=out_p or default_dst,
                height=int(p.get("height", 720)),
                video_kbps=int(p.get("video_kbps", 3500)),
            ),
        )
        linked = False
        if transport is not None and resolved_clip_id and bool(p.get("link_in_resolve", True)):
            link_res = await transport.call(
                "mediapool.link_proxy",
                {"clip_id": resolved_clip_id, "proxy_path": res["path"]},
            )
            linked = bool(link_res.get("linked"))
        res["linked_in_resolve"] = linked
        res["clip_id"] = resolved_clip_id
        return res

    if call.op == "media.analyze_clip":
        if cfg is None:
            raise RuntimeError("media.analyze_clip requires session Config")
        return await analyze_clip_with_scout(
            src,
            cfg=cfg,
            cache_dir=cache_dir,
            clip_id=resolved_clip_id,
            start_sec=float(p["start_sec"]) if p.get("start_sec") is not None else None,
            duration_sec=float(p["duration_sec"]) if p.get("duration_sec") is not None else None,
            cols=int(p.get("cols", 4)),
            rows=int(p.get("rows", 4)),
            include_video_proxy=bool(p.get("include_video_proxy", False)),
            force_refresh=bool(p.get("force_refresh", False)),
            question=str(p["question"]) if p.get("question") else None,
        )

    if call.op == "media.save_digest":
        return save_clip_digest(
            cache_dir,
            src,
            summary=str(p["summary"]),
            clip_id=resolved_clip_id,
            scenes=p.get("scenes"),
        )

    if call.op == "media.transcribe":
        gem_key = cfg.keys.get("gemini") if cfg else None
        return await transcribe_media(
            src,
            cache_dir=cache_dir / "transcripts",
            gemini_api_key=gem_key,
            openai_api_key=openai_api_key,
            language=p.get("language"),
            start_sec=float(p["start_sec"]) if p.get("start_sec") is not None else None,
            duration_sec=float(p["duration_sec"]) if p.get("duration_sec") is not None else None,
        )

    if call.op == "media.master_vo":
        out_p = sandbox.resolve_for_write(str(p["output_path"])) if p.get("output_path") else None
        return await loop.run_in_executor(
            None,
            lambda: master_voiceover_audio(
                src,
                output_path=out_p,
                target_lufs=float(p.get("target_lufs", -14.0)),
                denoise=bool(p.get("denoise", True)),
            ),
        )

    raise ValueError(f"Unhandled media op: {call.op}")


def _is_gated_op(call: OpCall) -> bool:
    if call.op in ("shell", "youtube.upload", "youtube.update"):
        return True
    spec = get_op(call.op)
    return bool(spec and spec.tier in GATED_TIERS)


def _needs_resolve_checkpoint(ops: list[OpCall]) -> bool:
    for call in ops:
        spec = get_op(call.op)
        if spec and spec.checkpoint_timeline:
            return True
    return False


async def _run_shell_cmd(cmd: str, cwd: Path, timeout: float) -> dict[str, Any]:
    loop = asyncio.get_running_loop()

    def _run() -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            cmd,
            shell=True,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )

    proc = await loop.run_in_executor(None, _run)
    return {
        "exit_code": proc.returncode,
        "stdout": proc.stdout[-16_000:],
        "stderr": proc.stderr[-8_000:],
    }


async def execute_batch(
    ops: list[OpCall],
    *,
    sandbox: Sandbox,
    journal: Journal,
    transport: ResolveTransport | None,
    cache_dir: Path,
    cfg: Config | None = None,
    openai_api_key: str | None = None,
    yolo: bool = False,
    shell_timeout: float = 120.0,
    approve: ApproveHook | None = None,
    auto_checkpoint: bool = True,
) -> BatchOutcome:
    clip_map, allowed_media = (
        await _fetch_resolve_clip_map(transport)
        if any(c.op in MEDIA_OPS for c in ops)
        else ({}, set())
    )

    eff_role = getattr(cfg, "role", "master") if cfg is not None else "master"
    plan_errors = plan_batch(
        ops,
        sandbox=sandbox,
        has_transport=transport is not None,
        resolve_media_paths=allowed_media,
        role=eff_role,
    )
    if plan_errors:
        return BatchOutcome(status="plan-failed", errors=plan_errors)

    results: list[dict[str, Any]] = []
    file_entries: list[FileEntry] = []
    attached_media: list[dict[str, Any]] = []
    gated_queue: list[OpCall] = []
    checkpoint: ResolveCheckpoint | None = None

    pass1_ops = [c for c in ops if not _is_gated_op(c)]
    if auto_checkpoint and transport is not None and _needs_resolve_checkpoint(pass1_ops):
        with contextlib.suppress(Exception):
            checkpoint = await journal.create_resolve_checkpoint(transport)

    # --- Pass 1: Read, Media, FS, and standard Resolve mutations ---
    for call in ops:
        if _is_gated_op(call):
            gated_queue.append(call)
            continue

        try:
            if call.op in FS_OPS:
                f_entry, res = _exec_fs_op(call, sandbox, journal)
                if f_entry is not None:
                    file_entries.append(f_entry)
            elif call.op in MEDIA_OPS:
                res = await _exec_media_op(
                    call,
                    sandbox,
                    cache_dir=cache_dir,
                    cfg=cfg,
                    openai_api_key=openai_api_key,
                    transport=transport,
                    clip_map=clip_map,
                    allowed_media=allowed_media,
                )
                if call.op == "media.proxy_clip" and res.get("attach_to_context"):
                    attached_media.append({"kind": "video", "path": res["path"]})
                elif call.op in ("media.contact_sheet", "youtube.thumbnail") and res.get("attach_to_context"):
                    attached_media.append({"kind": "image", "path": res["path"]})
                elif call.op in ("motion.render_overlay", "motion.qr_callout"):
                    attached_media.append(
                        {"kind": "image" if res.get("format") == "png_rgba" else "preview_only", "path": res["path"]}
                    )
                elif call.op == "media.analyze_clip" and res.get("contact_sheet_path"):
                    attached_media.append(
                        {"kind": "preview_only", "path": res["contact_sheet_path"], "proxy_path": res.get("proxy_path")}
                    )
            else:
                assert transport is not None
                res = await transport.call(call.op, call.params)
                if call.op == "timeline.grab_still" and res.get("attach_to_context"):
                    attached_media.append({"kind": "image", "path": res["path"]})

            results.append({"index": call.index, "op": call.op, "ok": True, "result": res})
        except Exception as err:  # noqa: BLE001 — catch SDK/HTTP errors (e.g. 401/429) cleanly into dr_results!
            journal.rollback_files(file_entries)
            if checkpoint is not None and transport is not None:
                with contextlib.suppress(Exception):
                    await journal.undo_last_resolve(transport)
                    checkpoint = None
            results.append({"index": call.index, "op": call.op, "ok": False, "error": str(err)})
            return BatchOutcome(
                status="apply-failed",
                results=results,
                errors=[{"index": call.index, "op": call.op, "message": str(err)}],
            )

    # --- Pass 2: Segregated gated ops ---
    for call in gated_queue:
        decision = ApprovalDecision(allow=True)
        if not yolo:
            if approve is None:
                decision = ApprovalDecision(
                    allow=False,
                    reason="gated operation requires interactive approval (or --yolo)",
                )
            else:
                decision = await approve(call)

        if not decision.allow:
            msg = decision.reason or "denied by operator"
            results.append({"index": call.index, "op": call.op, "ok": False, "error": msg})
            return BatchOutcome(
                status="denied",
                results=results,
                errors=[{"index": call.index, "op": call.op, "message": msg}],
                checkpoint=checkpoint,
                attached_media=attached_media,
            )

        if (
            (decision.checkpoint_first or (auto_checkpoint and _needs_resolve_checkpoint([call])))
            and checkpoint is None
            and transport is not None
        ):
            with contextlib.suppress(Exception):
                checkpoint = await journal.create_resolve_checkpoint(transport)

        try:
            if call.op == "shell":
                res = await _run_shell_cmd(str(call.params["cmd"]), sandbox.cwd, shell_timeout)
                ok = res["exit_code"] == 0
                results.append({"index": call.index, "op": call.op, "ok": ok, "result": res})
                if not ok:
                    return BatchOutcome(
                        status="apply-failed",
                        results=results,
                        errors=[
                            {
                                "index": call.index,
                                "op": "shell",
                                "message": f"command exited {res['exit_code']}",
                            }
                        ],
                        checkpoint=checkpoint,
                        attached_media=attached_media,
                    )
            elif call.op == "youtube.upload":
                from davag.youtube.studio import upload_staged_to_youtube

                mf_path = sandbox.resolve_for_read(str(call.params["manifest_path"]))
                res = await upload_staged_to_youtube(
                    manifest_path=mf_path,
                    home=cfg.home if cfg else cache_dir.parent,
                )
                results.append({"index": call.index, "op": call.op, "ok": True, "result": res})
            elif call.op == "youtube.update":
                from davag.youtube.studio import update_youtube_video

                mf_path = sandbox.resolve_for_read(str(call.params["manifest_path"]))
                res = await update_youtube_video(
                    manifest_path=mf_path,
                    home=cfg.home if cfg else cache_dir.parent,
                    video_id=str(call.params["video_id"]) if call.params.get("video_id") else None,
                    privacy_status=str(call.params["privacy_status"]) if call.params.get("privacy_status") else None,
                    post_first_comment=bool(call.params.get("post_first_comment", True)),
                )
                results.append({"index": call.index, "op": call.op, "ok": True, "result": res})
            else:
                assert transport is not None
                res = await transport.call(call.op, call.params)
                results.append({"index": call.index, "op": call.op, "ok": True, "result": res})
        except Exception as err:  # noqa: BLE001
            results.append({"index": call.index, "op": call.op, "ok": False, "error": str(err)})
            return BatchOutcome(
                status="apply-failed",
                results=results,
                errors=[{"index": call.index, "op": call.op, "message": str(err)}],
                checkpoint=checkpoint,
                attached_media=attached_media,
            )

    results.sort(key=lambda r: int(r["index"]))
    return BatchOutcome(
        status="ok",
        results=results,
        checkpoint=checkpoint,
        attached_media=attached_media,
    )


def format_batch_results(outcome: BatchOutcome) -> str:
    lines: list[str] = ["```dr_results"]
    if outcome.status == "plan-failed":
        lines.append("BATCH REJECTED — nothing was applied. Fix and resubmit the whole batch.")
        for err in outcome.errors:
            lines.append(f"  [{err['index']}] {err['op']}: {err['message']}")
        lines.append("```")
        return "\n".join(lines)

    if outcome.checkpoint is not None:
        lines.append(
            f"[checkpoint] duplicated timeline '{outcome.checkpoint.original_name}' -> "
            f"'{outcome.checkpoint.snapshot_name}' (use /undo to restore)"
        )

    for item in outcome.results:
        idx = item["index"]
        op = item["op"]
        if item.get("ok"):
            body = json.dumps(item.get("result"), ensure_ascii=False)
            lines.append(f"[{idx}] {op}: {body}")
        else:
            lines.append(f"[{idx}] {op}: ERROR {item.get('error')}")

    if outcome.status != "ok":
        for err in outcome.errors:
            lines.append(f"[{err['index']}] {err['op']}: FAILED — {err['message']}")

    lines.append("```")
    return "\n".join(lines)