"""FastAPI Overlays, Interactive Timeline, Lazy-Loaded Frame Thumbnails & Marker/Overlay Adjust API."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Response
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from .state import StateDep

router = APIRouter()


class RenderOverlayBody(BaseModel):
    kind: str = "qr"
    title: str = "Explore the Code & Demo"
    url: str = ""
    name: str = "custom_overlay"
    html: str = ""
    record_tc: str | None = None
    duration_sec: float = 6.5
    position: str = "bottom-right"
    static: bool = False
    codec: str = "png"
    add_marker: bool = True


class AdjustTimelineBody(BaseModel):
    action: str  # "move_marker" | "delete_marker" | "move_overlay" | "delete_overlay" | "add_marker"
    old_frame: int | None = None
    new_frame: int | None = None
    new_tc: str | None = None
    duration: int = 1
    color: str = "Cyan"
    name: str = ""
    note: str = ""
    custom_data: str = ""
    item_id: str | None = None
    track_index: int | None = None
    extend_end_frames: int | None = None


@router.get("/api/overlays")
async def api_overlays_get(st: StateDep) -> dict[str, Any]:
    from davag.youtube.studio import extract_link_markers

    session = st.session
    ov_dir = session.cfg.cwd / "overlays"
    files_out: list[dict[str, Any]] = []
    if ov_dir.is_dir():
        for f in sorted(ov_dir.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            if f.is_file() and f.suffix.lower() in (".mov", ".png", ".webm", ".html", ".svg"):
                files_out.append(
                    {
                        "name": f.name,
                        "bytes": f.stat().st_size,
                        "ext": f.suffix.lower(),
                        "preview_url": f"/api/overlays/preview/{f.name}",
                    }
                )

    timeline_info: dict[str, Any] = {
        "connected": False,
        "name": None,
        "fps": 30.0,
        "start_frame": 108000,
        "end_frame": 109800,
        "duration_frames": 1800,
        "playhead_tc": "01:00:00:00",
        "playhead_frame": 108000,
        "tracks": [],
        "markers": [],
    }
    timeline_overlays: list[dict[str, Any]] = []
    link_markers: list[dict[str, Any]] = []

    if session.transport is not None:
        with contextlib.suppress(Exception):
            tl_st = await session.transport.call(
                "timeline.get_state", {"track_types": ["video"], "include_properties": False}, timeout=3.0
            )
            fps = float(tl_st.get("fps") or 30.0)
            tl_start = int(tl_st.get("start_frame", 0))
            tl_end = int(tl_st.get("end_frame", tl_start + 300))
            v_tracks = tl_st.get("tracks", {}).get("video", [])
            mk_res = await session.transport.call("marker.list", timeout=2.5)
            all_markers = mk_res.get("markers") or []
            link_markers = extract_link_markers(all_markers, tl_start, fps)

            for tr in v_tracks:
                tidx = int(tr.get("track_index", 1))
                if tidx < 2 and "overlay" not in str(tr.get("name", "")).lower():
                    continue
                for it in tr.get("items") or []:
                    it_name = str(it.get("name") or "")
                    stem = Path(it_name).stem
                    html_sidecar = ov_dir / f"{stem}.html"
                    mov_file = ov_dir / it_name
                    timeline_overlays.append(
                        {
                            "track_index": tidx,
                            "track_name": tr.get("name"),
                            "item_id": it.get("item_id"),
                            "name": it_name,
                            "record_start": it.get("record_start"),
                            "record_end": it.get("record_end"),
                            "record_start_tc": it.get("record_start_tc"),
                            "record_end_tc": it.get("record_end_tc"),
                            "duration": it.get("duration"),
                            "clip_color": it.get("clip_color", ""),
                            "enabled": it.get("enabled", True),
                            "html_source": html_sidecar.read_text(encoding="utf-8") if html_sidecar.is_file() else None,
                            "preview_url": f"/api/overlays/preview/{it_name}" if mov_file.is_file() else None,
                        }
                    )

            timeline_info = {
                "connected": True,
                "name": tl_st.get("name"),
                "fps": fps,
                "start_frame": tl_start,
                "end_frame": tl_end,
                "duration_frames": max(1, int(tl_st.get("duration_frames") or (tl_end - tl_start))),
                "playhead_tc": str(tl_st.get("playhead_tc") or "01:00:00:00"),
                "playhead_frame": int(tl_st.get("playhead_frame") or tl_start),
                "tracks": v_tracks,
                "markers": all_markers,
            }

    yt_packages: list[dict[str, Any]] = []
    yt_root = session.cfg.cwd / "youtube_staging"
    if yt_root.is_dir():
        for mf in sorted(yt_root.glob("*/youtube_manifest.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            with contextlib.suppress(Exception):
                data = json.loads(mf.read_text(encoding="utf-8"))
                yt_packages.append(
                    {
                        "slug": data.get("slug"),
                        "title": data.get("title"),
                        "privacy_status": data.get("privacy_status"),
                        "primary_url": data.get("primary_url"),
                        "chapters_count": len(data.get("chapters") or []),
                        "links_count": len(data.get("links") or []),
                        "end_screen": data.get("end_screen"),
                        "video_id": data.get("video_id"),
                        "studio_url": data.get("studio_url"),
                        "watch_url": data.get("watch_url"),
                        "comment_id": data.get("comment_id"),
                        "manifest_path": str(mf.relative_to(session.cfg.cwd)),
                    }
                )

    return {
        "playhead_tc": timeline_info["playhead_tc"],
        "timeline": timeline_info,
        "files": files_out,
        "timeline_overlays": timeline_overlays,
        "link_markers": link_markers,
        "youtube_packages": yt_packages,
    }


_SPAN_MEMO_LOCK = asyncio.Lock()
_LIVE_GRAB_LOCK = asyncio.Lock()


def _extract_jpeg_frame(src: Path, sec: float, out_jpg: Path, width: int) -> bool:
    import os
    import uuid

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return False
    out_jpg.parent.mkdir(parents=True, exist_ok=True)
    tmp_jpg = out_jpg.with_name(f".tmp_{uuid.uuid4().hex[:10]}.jpg")
    is_still = src.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp")
    cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error"]
    if not is_still and sec > 0.01:
        cmd.extend(["-ss", f"{sec:.3f}"])
    qv = "2" if width >= 960 else "4"
    cmd.extend(["-i", str(src), "-vf", f"scale={width}:-2,format=yuvj420p", "-frames:v", "1", "-q:v", qv, str(tmp_jpg)])
    subprocess.run(cmd, capture_output=True, timeout=20, check=False)
    if tmp_jpg.is_file() and tmp_jpg.stat().st_size > 0:
        with contextlib.suppress(OSError):
            os.replace(tmp_jpg, out_jpg)
        with contextlib.suppress(OSError):
            if tmp_jpg.is_file():
                tmp_jpg.unlink()
        return out_jpg.is_file()
    with contextlib.suppress(OSError):
        tmp_jpg.unlink()
    return False


async def _get_v1_span_map(st: StateDep) -> dict[str, Any]:
    """Stateless 5-second read-through memo of V1 clip spans so 16 concurrent lazy-loaded `<img>`
    thumbnail requests trigger ONLY ONE Resolve RPC instead of queueing 32 COM calls!
    """
    import time

    now = time.monotonic()
    memo = st.tl_span_memo
    if memo and (now - float(memo.get("ts", 0.0))) < 5.0:
        return memo

    async with _SPAN_MEMO_LOCK:
        now = time.monotonic()
        if st.tl_span_memo and (now - float(st.tl_span_memo.get("ts", 0.0))) < 5.0:
            return st.tl_span_memo

        session = st.session
        spans: list[dict[str, Any]] = []
        fps = 30.0
        tl_start = 0
        if session.transport is not None:
            with contextlib.suppress(Exception):
                tl_st = await session.transport.call(
                    "timeline.get_state", {"track_types": ["video"], "include_properties": False}, timeout=4.0
                )
                fps = float(tl_st.get("fps") or 30.0)
                tl_start = int(tl_st.get("start_frame", 0))
                v_tracks = tl_st.get("tracks", {}).get("video", [])
                mp_res = await session.transport.call("mediapool.clips", {"recursive": True}, timeout=4.0)
                by_id = {c["clip_id"]: c for c in (mp_res.get("clips") or []) if c.get("clip_id")}
                if v_tracks:
                    for it in v_tracks[0].get("items") or []:
                        c_info = by_id.get(it.get("media_pool_id"))
                        if c_info and c_info.get("file_path"):
                            cand_p = Path(str(c_info["file_path"]))
                            proxy_p = Path(str(c_info["proxy_path"])) if c_info.get("proxy_path") else None
                            eff_p = proxy_p if (proxy_p and proxy_p.is_file()) else cand_p
                            if eff_p.is_file():
                                spans.append(
                                    {
                                        "record_start": int(it["record_start"]),
                                        "record_end": int(it["record_end"]),
                                        "source_start": int(it.get("source_start", 0)),
                                        "clip_fps": float(c_info.get("fps") or fps),
                                        "path": eff_p,
                                    }
                                )

        st.tl_span_memo = {"ts": time.monotonic(), "fps": fps, "tl_start": tl_start, "spans": spans}
        return st.tl_span_memo


@router.get("/api/overlays/thumb")
async def api_overlays_thumb(
    st: StateDep,
    frame: int,
    w: int = 320,
    live: int = 0,
) -> Response:
    """Lazy-loaded disk-cached timeline thumbnail (`live=0` fast FFmpeg source-seek; `live=1` Resolve composite)."""
    from davag.resolve_ops.helpers import frames_to_tc

    session = st.session
    width = max(120, min(1920, int(w)))
    cache_dir = session.cfg.home / "cache" / "tl_thumbs"
    cache_dir.mkdir(parents=True, exist_ok=True)

    if live == 1 and session.transport is not None:
        async with _LIVE_GRAB_LOCK:
            with contextlib.suppress(Exception):
                memo = await _get_v1_span_map(st)
                tc_str = frames_to_tc(frame, float(memo.get("fps") or 30.0))
                still = await session.transport.call(
                    "timeline.grab_still", {"tc": tc_str, "attach_to_context": False}, timeout=6.0
                )
                raw_png = Path(str(still["path"]))
                out_live = cache_dir / f"live_{frame}_{width}.jpg"
                ok = await asyncio.to_thread(_extract_jpeg_frame, raw_png, 0.0, out_live, width)
                with contextlib.suppress(OSError):
                    raw_png.unlink()
                if ok:
                    return FileResponse(out_live, media_type="image/jpeg", headers={"Cache-Control": "no-cache"})

    memo = await _get_v1_span_map(st)
    spans = memo.get("spans") or []
    fps = float(memo.get("fps") or 30.0)
    tl_start = int(memo.get("tl_start", 0))

    src_file: Path | None = None
    src_sec = 0.0
    if spans:
        chosen = next(
            (sp for sp in spans if sp["record_start"] <= frame < sp["record_end"]),
            spans[-1] if frame >= spans[-1]["record_end"] else spans[0],
        )
        src_file = chosen["path"]
        offset_f = max(0, frame - chosen["record_start"])
        src_sec = max(0.0, (chosen["source_start"] + offset_f) / max(1.0, chosen["clip_fps"]))
    else:
        masters = sorted(
            (session.cfg.cwd / "youtube_staging").glob("*/*_master.mp4"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if masters:
            src_file = masters[0]
            src_sec = max(0.0, (frame - tl_start) / max(1.0, fps))

    if src_file is None or not src_file.is_file():
        return JSONResponse({"error": "no timeline source media on disk for thumbnail"}, status_code=404)

    key_raw = f"{src_file.resolve()}:{int(src_file.stat().st_mtime)}:{src_sec:.2f}:{width}"
    digest = hashlib.sha1(key_raw.encode("utf-8")).hexdigest()[:18]
    cached_jpg = cache_dir / f"th_{digest}.jpg"

    if not cached_jpg.is_file():
        async with st.thumb_sem:
            if not cached_jpg.is_file():
                ok = await asyncio.to_thread(_extract_jpeg_frame, src_file, src_sec, cached_jpg, width)
                if not ok:
                    return JSONResponse({"error": "thumbnail extraction failed"}, status_code=500)

    return FileResponse(cached_jpg, media_type="image/jpeg", headers={"Cache-Control": "max-age=3600"})


@router.post("/api/overlays/adjust")
async def api_overlays_adjust(body: AdjustTimelineBody, st: StateDep) -> JSONResponse:
    """Interactive GUI drag/edit handler for markers (`Cyan` chapters / `Purple` QR links) & overlay clips.
    Records every operator action into `session.journal` so markers, drags, labels & clip deletes
    support instant Undo/Redo, Rewind/Fast-Forward, and `--resume` persistence!
    """
    from davag.resolve_ops.helpers import frames_to_tc, tc_to_frames

    session = st.session
    if session.transport is None:
        return JSONResponse({"error": "DaVinci Resolve is not connected"}, status_code=400)

    st.tl_span_memo.clear()
    try:
        tl_st = await session.transport.call(
            "timeline.get_state", {"track_types": ["video"], "include_properties": False}, timeout=2.5
        )
        fps = float(tl_st.get("fps") or 30.0)
        tl_start = int(tl_st.get("start_frame", 0))
        v_tracks = tl_st.get("tracks", {}).get("video", [])

        def _find_tl_item(iid: str) -> tuple[int, dict[str, Any]] | tuple[None, None]:
            for tr in v_tracks:
                tidx = int(tr.get("track_index", 1))
                for it in tr.get("items") or []:
                    if str(it.get("item_id")) == iid:
                        return tidx, it
            return None, None

        if body.action in ("move_marker", "add_marker"):
            mk_res = await session.transport.call("marker.list", timeout=2.0)
            all_markers = list(mk_res.get("markers") or [])

            old_m: dict[str, Any] | None = None
            rel_old: int | None = None
            if body.action == "move_marker" and body.old_frame is not None:
                rel_old = max(0, int(body.old_frame) - tl_start)
                old_m = next((m for m in all_markers if int(m.get("frame_offset", -1)) == rel_old), None)
                with contextlib.suppress(Exception):
                    await session.transport.call("marker.delete", {"frame_offset": rel_old})

            target_tc = body.new_tc or frames_to_tc(int(body.new_frame or tl_start), fps)
            new_abs = tc_to_frames(target_tc, fps)
            rel_new = max(0, new_abs - tl_start)
            overwritten_m = next(
                (m for m in all_markers if int(m.get("frame_offset", -1)) == rel_new and rel_new != rel_old),
                None,
            )
            if overwritten_m is not None:
                with contextlib.suppress(Exception):
                    await session.transport.call("marker.delete", {"frame_offset": rel_new})

            m_color = body.color or (old_m.get("color") if old_m else None) or "Cyan"
            m_name = body.name or (old_m.get("name") if old_m else None) or "Marker"
            m_note = body.note if body.note is not None else (str(old_m.get("note", "")) if old_m else "")
            m_dur = max(1, int(body.duration or (old_m.get("duration", 1) if old_m else 1)))
            m_cd = body.custom_data or (
                str(old_m.get("custom_data", "")) if old_m else ("davag:link" if m_color.lower() == "purple" else "")
            )

            res = await session.transport.call(
                "marker.add",
                {
                    "frame_offset": rel_new,
                    "color": m_color,
                    "name": m_name,
                    "note": m_note,
                    "duration": m_dur,
                    "custom_data": m_cd,
                },
            )
            actual_off = int(res.get("frame_offset", rel_new))
            actual_tc = str(res.get("tc", target_tc))

            undo_ops: list[dict[str, Any]] = [
                {"op": "marker.delete", "params": {"frame_offset": actual_off}, "ignore_error": True}
            ]
            if overwritten_m is not None:
                undo_ops.append(
                    {
                        "op": "marker.add",
                        "params": {
                            "frame_offset": int(overwritten_m["frame_offset"]),
                            "color": overwritten_m.get("color", "Cyan"),
                            "name": overwritten_m.get("name", "Marker"),
                            "note": overwritten_m.get("note", ""),
                            "duration": int(overwritten_m.get("duration", 1)),
                            "custom_data": overwritten_m.get("custom_data", ""),
                        },
                    }
                )
            if old_m is not None and rel_old is not None:
                undo_ops.extend(
                    [
                        {"op": "marker.delete", "params": {"frame_offset": rel_old}, "ignore_error": True},
                        {
                            "op": "marker.add",
                            "params": {
                                "frame_offset": rel_old,
                                "color": old_m.get("color", "Cyan"),
                                "name": old_m.get("name", "Marker"),
                                "note": old_m.get("note", ""),
                                "duration": int(old_m.get("duration", 1)),
                                "custom_data": old_m.get("custom_data", ""),
                            },
                        },
                    ]
                )

            redo_ops: list[dict[str, Any]] = []
            if rel_old is not None:
                redo_ops.append({"op": "marker.delete", "params": {"frame_offset": rel_old}, "ignore_error": True})
            redo_ops.extend(
                [
                    {"op": "marker.delete", "params": {"frame_offset": actual_off}, "ignore_error": True},
                    {
                        "op": "marker.add",
                        "params": {
                            "frame_offset": actual_off,
                            "color": m_color,
                            "name": m_name,
                            "note": m_note,
                            "duration": m_dur,
                            "custom_data": m_cd,
                        },
                    },
                ]
            )

            if body.action == "add_marker" or old_m is None:
                summary = f'Added {m_color} marker "{m_name}" @ {actual_tc}'
                act_kind = "add_marker"
            elif old_m.get("tc") != actual_tc:
                summary = f'Moved {m_color} marker "{m_name}" {old_m.get("tc")} -> {actual_tc}'
                act_kind = "move_marker"
            else:
                summary = f'Edited {m_color} marker "{m_name}" @ {actual_tc}'
                act_kind = "edit_marker"

            rec = session.journal.record_inverse_action(
                actor="operator",
                action_type=act_kind,
                summary=summary,
                undo_ops=undo_ops,
                redo_ops=redo_ops,
                tc=actual_tc,
                color=m_color,
                details={"before": old_m, "after": res},
            )
            return JSONResponse({"ok": True, "marker": res, "action_id": rec.id})

        if body.action == "delete_marker" and body.old_frame is not None:
            mk_res = await session.transport.call("marker.list", timeout=2.0)
            all_markers = list(mk_res.get("markers") or [])
            rel_old = max(0, int(body.old_frame) - tl_start)
            old_m = next((m for m in all_markers if int(m.get("frame_offset", -1)) == rel_old), None)
            res = await session.transport.call("marker.delete", {"frame_offset": rel_old})

            rec_id: str | None = None
            if old_m is not None:
                m_color = str(old_m.get("color") or "Cyan")
                m_name = str(old_m.get("name") or "Marker")
                m_tc = str(old_m.get("tc") or frames_to_tc(tl_start + rel_old, fps))
                rec = session.journal.record_inverse_action(
                    actor="operator",
                    action_type="delete_marker",
                    summary=f'Deleted {m_color} marker "{m_name}" @ {m_tc}',
                    undo_ops=[
                        {"op": "marker.delete", "params": {"frame_offset": rel_old}, "ignore_error": True},
                        {
                            "op": "marker.add",
                            "params": {
                                "frame_offset": rel_old,
                                "color": m_color,
                                "name": m_name,
                                "note": str(old_m.get("note") or ""),
                                "duration": int(old_m.get("duration") or 1),
                                "custom_data": str(old_m.get("custom_data") or ""),
                            },
                        },
                    ],
                    redo_ops=[{"op": "marker.delete", "params": {"frame_offset": rel_old}, "ignore_error": True}],
                    tc=m_tc,
                    color=m_color,
                    details={"deleted_marker": old_m},
                )
                rec_id = rec.id
            return JSONResponse({"ok": True, **res, "action_id": rec_id})

        if body.action == "move_overlay" and body.item_id:
            old_tidx, old_it = _find_tl_item(body.item_id)
            params: dict[str, Any] = {"item_id": body.item_id, "update_linked": False}
            if body.new_frame is not None:
                params["record_frame"] = int(body.new_frame)
            elif body.new_tc:
                params["record_frame"] = tc_to_frames(body.new_tc, fps)
            if body.track_index is not None:
                params["track_index"] = int(body.track_index)
            if body.extend_end_frames is not None:
                params["extend_end_frames"] = int(body.extend_end_frames)
            res = await session.transport.call("timeline.trim_and_move", params)

            rec_id = None
            if old_it is not None:
                old_rf = int(old_it["record_start"])
                old_tc_s = str(old_it.get("record_start_tc") or frames_to_tc(old_rf, fps))
                new_rf = int(params.get("record_frame", old_rf))
                new_tc_s = frames_to_tc(new_rf, fps)
                inv_params: dict[str, Any] = {
                    "item_id": body.item_id,
                    "update_linked": False,
                    "record_frame": old_rf,
                }
                if old_tidx is not None and body.track_index is not None:
                    inv_params["track_index"] = old_tidx
                if body.extend_end_frames:
                    inv_params["extend_end_frames"] = -int(body.extend_end_frames)
                rec = session.journal.record_inverse_action(
                    actor="operator",
                    action_type="move_overlay",
                    summary=f'Moved overlay "{old_it.get("name", body.item_id)}" {old_tc_s} -> {new_tc_s}',
                    undo_ops=[{"op": "timeline.trim_and_move", "params": inv_params}],
                    redo_ops=[{"op": "timeline.trim_and_move", "params": params}],
                    tc=new_tc_s,
                    color="Mint",
                    details={"item_id": body.item_id, "old_frame": old_rf, "new_frame": new_rf},
                )
                rec_id = rec.id
            return JSONResponse({"ok": True, **res, "action_id": rec_id})

        if body.action == "set_clip_color" and body.item_id:
            _old_tidx, old_it = _find_tl_item(body.item_id)
            old_color = str(old_it.get("clip_color", "")) if old_it else ""
            it_name = str(old_it.get("name", body.item_id)) if old_it else body.item_id
            it_tc = str(old_it.get("record_start_tc", "")) if old_it else ""
            res = await session.transport.call(
                "timeline.set_clip_state",
                {"item_ids": [body.item_id], "clip_color": body.color},
            )
            rec = session.journal.record_inverse_action(
                actor="operator",
                action_type="set_clip_color",
                summary=f'Labeled clip "{it_name}" {old_color or "None"} -> {body.color or "None"}',
                undo_ops=[{"op": "timeline.set_clip_state", "params": {"item_ids": [body.item_id], "clip_color": old_color}}],
                redo_ops=[{"op": "timeline.set_clip_state", "params": {"item_ids": [body.item_id], "clip_color": body.color}}],
                tc=it_tc,
                color=body.color,
            )
            return JSONResponse({"ok": True, **res, "action_id": rec.id})

        if body.action == "delete_overlay" and body.item_id:
            _old_tidx, old_it = _find_tl_item(body.item_id)
            it_name = str(old_it.get("name", body.item_id)) if old_it else body.item_id
            it_tc = str(old_it.get("record_start_tc", "")) if old_it else ""
            cp = await session.journal.create_resolve_checkpoint(session.transport)
            res = await session.transport.call(
                "timeline.delete_clips", {"item_ids": [body.item_id], "ripple": False}
            )
            rec_id = None
            if cp is not None:
                rec = session.journal.record_batch_action(
                    actor="operator",
                    action_type="delete_overlay",
                    summary=f'Deleted overlay "{it_name}" @ {it_tc or "timeline"}',
                    checkpoint=cp,
                    tc=it_tc,
                    color="Mint",
                    details={"item_id": body.item_id, "name": it_name},
                )
                rec_id = rec.id
            return JSONResponse({"ok": True, **res, "action_id": rec_id})

        return JSONResponse({"error": f"Unsupported adjust action '{body.action}'"}, status_code=400)
    except Exception as err:  # noqa: BLE001
        return JSONResponse({"error": str(err)}, status_code=400)


@router.get("/api/overlays/qr-preview")
async def api_overlays_qr_preview(
    url: str = "https://github.com/",
    title: str = "Interactive Code & Notebook",
    position: str = "bottom-right",
) -> Response:
    from davag.youtube.qr import build_qr_callout_html, generate_qr_svg

    try:
        svg = generate_qr_svg(url.strip(), size_px=180)
        card_html = build_qr_callout_html(title=title.strip(), url=url.strip(), position=position.strip())
        return JSONResponse({"ok": True, "svg": svg, "html": card_html})
    except Exception as err:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(err)}, status_code=400)


@router.post("/api/overlays/render")
async def api_overlays_render(body: RenderOverlayBody, st: StateDep) -> JSONResponse:
    from davag.agent.executor import OpCall, execute_batch

    session = st.session
    if body.kind == "qr":
        if not body.url.strip():
            return JSONResponse({"error": "URL is required"}, status_code=400)
        op_call = OpCall(
            index=0,
            op="motion.qr_callout",
            params={
                "title": body.title,
                "url": body.url.strip(),
                "duration_sec": body.duration_sec,
                "position": body.position,
                "codec": body.codec,
                "add_marker": body.add_marker,
                **({"record_tc": body.record_tc.strip()} if body.record_tc else {}),
            },
        )
    else:
        if not body.html.strip():
            return JSONResponse({"error": "HTML/SVG content is required"}, status_code=400)
        op_call = OpCall(
            index=0,
            op="motion.render_overlay",
            params={
                "name": body.name,
                "html": body.html,
                "duration_sec": body.duration_sec,
                "static": body.static,
                "codec": body.codec,
                **({"record_tc": body.record_tc.strip()} if body.record_tc else {}),
            },
        )

    outcome = await execute_batch(
        [op_call],
        sandbox=session.sandbox,
        journal=session.journal,
        transport=session.transport,
        cache_dir=session.cfg.home / "cache",
        cfg=session.cfg,
        yolo=True,
        actor="operator",
    )
    if outcome.status != "ok":
        err_msg = "; ".join(e["message"] for e in outcome.errors) or "overlay render failed"
        return JSONResponse({"error": err_msg}, status_code=400)
    return JSONResponse({"ok": True, "result": outcome.results[0]["result"]})


@router.get("/api/overlays/preview/{name}")
async def api_overlays_preview_file(name: str, st: StateDep) -> Response:
    raw_name = Path(name).name
    ov_dir = (st.session.cfg.cwd / "overlays").resolve()
    src = (ov_dir / raw_name).resolve()
    if not src.is_file() or src.parent != ov_dir:
        return JSONResponse({"error": "overlay not found"}, status_code=404)
    if src.suffix.lower() == ".png":
        return FileResponse(src, headers={"Cache-Control": "no-cache"})

    prev_dir = ov_dir / ".previews"
    prev_png = prev_dir / f"{src.stem}_{int(src.stat().st_mtime)}.jpg"
    if not prev_png.is_file():
        ok = await asyncio.to_thread(_extract_jpeg_frame, src, 1.5, prev_png, 1280)
        if not ok:
            return JSONResponse({"error": "could not extract preview frame"}, status_code=500)
    return FileResponse(prev_png, media_type="image/jpeg", headers={"Cache-Control": "max-age=300"})


def _encode_overlay_preview_mp4(ov_mov: Path, bg_src: Path | None, bg_sec: float, out_mp4: Path) -> bool:
    import os
    import uuid

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return False
    out_mp4.parent.mkdir(parents=True, exist_ok=True)
    tmp_mp4 = out_mp4.with_name(f".tmp_{uuid.uuid4().hex[:10]}.mp4")
    if bg_src is not None and bg_src.is_file():
        cmd = [
            ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
            "-ss", f"{max(0.0, bg_sec):.3f}", "-t", "8.0", "-i", str(bg_src),
            "-i", str(ov_mov),
            "-filter_complex", "[0:v]scale=640:360[bg];[1:v]scale=640:360[fg];[bg][fg]overlay=0:0:shortest=1,format=yuv420p",
            "-an", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "24", "-movflags", "+faststart", str(tmp_mp4),
        ]
    else:
        cmd = [
            ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "color=c=0x111726:s=640x360:r=30",
            "-i", str(ov_mov),
            "-filter_complex", "[1:v]scale=640:360[fg];[0:v][fg]overlay=0:0:shortest=1,format=yuv420p",
            "-an", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "24", "-movflags", "+faststart", str(tmp_mp4),
        ]
    subprocess.run(cmd, capture_output=True, timeout=30, check=False)
    if tmp_mp4.is_file() and tmp_mp4.stat().st_size > 0:
        with contextlib.suppress(OSError):
            os.replace(tmp_mp4, out_mp4)
        with contextlib.suppress(OSError):
            if tmp_mp4.is_file():
                tmp_mp4.unlink()
        return out_mp4.is_file()
    with contextlib.suppress(OSError):
        tmp_mp4.unlink()
    return False


@router.get("/api/overlays/render-video/{name}")
async def api_overlays_render_video(st: StateDep, name: str, frame: int = 0) -> Response:
    """Serve a fast cached H.264 MP4 preview of the actual rendered `.mov` overlay composited over V1."""
    raw_name = Path(name).name
    ov_dir = (st.session.cfg.cwd / "overlays").resolve()
    ov_mov = (ov_dir / raw_name).resolve()
    if not ov_mov.is_file() or ov_mov.parent != ov_dir:
        return JSONResponse({"error": "rendered overlay .mov not found"}, status_code=404)

    memo = await _get_v1_span_map(st)
    spans = memo.get("spans") or []
    bg_src: Path | None = None
    bg_sec = 0.0
    if spans and frame > 0:
        chosen = next(
            (sp for sp in spans if sp["record_start"] <= frame < sp["record_end"]),
            spans[-1] if frame >= spans[-1]["record_end"] else spans[0],
        )
        bg_src = chosen["path"]
        bg_sec = max(0.0, (chosen["source_start"] + max(0, frame - chosen["record_start"])) / max(1.0, chosen["clip_fps"]))

    prev_dir = ov_dir / ".previews"
    key_raw = f"{ov_mov.name}:{int(ov_mov.stat().st_mtime)}:{bg_src}:{bg_sec:.1f}"
    digest = hashlib.sha1(key_raw.encode("utf-8")).hexdigest()[:14]
    out_mp4 = prev_dir / f"rv_{ov_mov.stem}_{digest}.mp4"
    if not out_mp4.is_file():
        async with st.thumb_sem:
            if not out_mp4.is_file():
                ok = await asyncio.to_thread(_encode_overlay_preview_mp4, ov_mov, bg_src, bg_sec, out_mp4)
                if not ok:
                    return JSONResponse({"error": "failed to encode overlay render preview"}, status_code=500)
    return FileResponse(out_mp4, media_type="video/mp4", headers={"Cache-Control": "max-age=600"})