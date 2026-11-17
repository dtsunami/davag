"""DaVinci Resolve Inspector, Clip Studio, Teleprompter, Playhead & Media serving routes (FastAPI)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request, Response
from fastapi.responses import FileResponse, JSONResponse

from davag.context.media import load_image_part

from .state import StateDep

router = APIRouter()


@router.get("/api/resolve")
async def api_resolve_state(req: Request, st: StateDep) -> dict[str, Any]:
    from davag.media_intel.digest import _digest_key, load_clip_digest
    from davag.media_intel.transcribe import load_cached_transcript

    full = str(req.query_params.get("full", "1")).lower() not in ("0", "false", "no")
    session = st.session
    if session.transport is None:
        await session.maybe_auto_connect_resolve()
    if session.transport is None:
        return {"connected": False, "note": session.resolve_connect_note}
    try:
        status = await session.transport.call("resolve.status", timeout=8.0)
        if not full:
            return {
                "connected": True,
                "status": status,
                "checkpoints": [
                    {
                        "seq": c.seq,
                        "original_name": c.original_name,
                        "snapshot_name": c.snapshot_name,
                        "ts": c.ts,
                    }
                    for c in session.journal.resolve_checkpoints
                ],
            }

        tree = None
        clips_list = []
        tl_state = None
        if status.get("project"):
            tree = await session.transport.call("mediapool.tree", timeout=10.0)
            cache_root = session.cfg.home / "cache"
            proxy_cache_dir = cache_root / "resolve_proxies"
            sheet_cache_dir = cache_root / "contact_sheets"

            c_res = await session.transport.call("mediapool.clips", {"recursive": True}, timeout=12.0)
            for c in c_res.get("clips") or []:
                fp_str = str(c.get("file_path") or "")
                dig = load_clip_digest(cache_root, fp_str) if fp_str else None
                tr_cached = load_cached_transcript(cache_root / "transcripts", fp_str) if fp_str else None

                proxy_file: Path | None = None
                if c.get("proxy_path") and Path(str(c["proxy_path"])).is_file():
                    proxy_file = Path(str(c["proxy_path"]))
                elif fp_str and proxy_cache_dir.is_dir():
                    stem = Path(fp_str).stem
                    matches = sorted(
                        proxy_cache_dir.glob(f"{stem}_*proxy.mp4"),
                        key=lambda p: p.stat().st_mtime,
                        reverse=True,
                    )
                    if matches:
                        proxy_file = matches[0]

                proxy_url = st.register_media_file(proxy_file) if proxy_file else None

                sheet_file: Path | None = None
                if dig and dig.get("contact_sheet_path") and Path(str(dig["contact_sheet_path"])).is_file():
                    sheet_file = Path(str(dig["contact_sheet_path"]))
                elif fp_str and sheet_cache_dir.is_dir():
                    dk = _digest_key(fp_str)
                    s_matches = sorted(
                        sheet_cache_dir.glob(f"{dk}*.png"),
                        key=lambda p: p.stat().st_mtime,
                        reverse=True,
                    )
                    if s_matches:
                        sheet_file = s_matches[0]

                sheet_url = st.register_media_file(sheet_file) if sheet_file else None
                clips_list.append(
                    {
                        **c,
                        "digest": dig.get("summary") if dig else None,
                        "scout_model": dig.get("scout_model") if dig else None,
                        "proxy_url": proxy_url,
                        "proxy_file_name": proxy_file.name if proxy_file else None,
                        "sheet_url": sheet_url,
                        "transcript": (
                            {
                                "backend": tr_cached.get("backend"),
                                "segment_count": len(tr_cached.get("segments") or []),
                                "segments": (tr_cached.get("segments") or [])[:200],
                            }
                            if tr_cached
                            else None
                        ),
                    }
                )
        if status.get("timeline"):
            tl_state = await session.transport.call("timeline.get_state", timeout=12.0)
        return {
            "connected": True,
            "status": status,
            "mediapool": tree,
            "clips": clips_list,
            "timeline": tl_state,
            "checkpoints": [
                {
                    "seq": c.seq,
                    "original_name": c.original_name,
                    "snapshot_name": c.snapshot_name,
                    "ts": c.ts,
                }
                for c in session.journal.resolve_checkpoints
            ],
        }
    except TimeoutError as err:
        return {
            "connected": True,
            "busy": True,
            "mode": session.transport.mode,
            "error": str(err),
        }
    except Exception as err:  # noqa: BLE001
        return {"connected": False, "error": str(err)}


@router.post("/api/resolve/reconnect")
async def api_resolve_reconnect(req: Request, st: StateDep) -> JSONResponse:
    body = await req.json()
    try:
        res = await st.session.reconnect_resolve(body.get("mode"))
        return JSONResponse({"ok": True, "status": res})
    except Exception as err:  # noqa: BLE001
        st.session.bus.publish(
            "resolve-changed", {"connected": False, "mode": None, "error": str(err)}
        )
        return JSONResponse({"ok": False, "error": str(err)}, status_code=400)


@router.post("/api/resolve/grab")
async def api_resolve_grab(req: Request, st: StateDep) -> JSONResponse:
    session = st.session
    if session.transport is None:
        return JSONResponse({"error": "DaVinci Resolve is not connected"}, status_code=400)
    body = await req.json()
    attach = bool(body.get("attach", True))
    params: dict[str, Any] = {}
    if body.get("tc"):
        params["tc"] = str(body["tc"])
    try:
        still = await session.transport.call("timeline.grab_still", params)
        p = Path(still["path"]).resolve()
        still_id = f"s{len(st.grab_files) + 1}"
        st.grab_files[still_id] = p
        if attach and session.cfg.model.vision:
            img = load_image_part(p)
            session.ledger.add(
                seg_type="media",
                label=f"viewer frame @ {still.get('playhead_tc')}",
                role="user",
                image=img,
            )
        url = f"/api/media/still/{still_id}"
        session.bus.publish(
            "viewer-still",
            {"url": url, "playhead_tc": still.get("playhead_tc"), "attached": attach},
        )
        return JSONResponse({"ok": True, "url": url, "playhead_tc": still.get("playhead_tc")})
    except Exception as err:  # noqa: BLE001
        return JSONResponse({"error": str(err)}, status_code=400)


@router.get("/api/media/still/{sid}")
async def api_serve_still(sid: str, st: StateDep) -> Response:
    p = st.grab_files.get(sid)
    if p is None or not p.is_file():
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(p)


@router.get("/api/journal")
async def api_journal_get(st: StateDep) -> dict[str, Any]:
    return st.session.journal.list_state()


@router.post("/api/journal/undo")
async def api_journal_undo(req: Request, st: StateDep) -> JSONResponse:
    session = st.session
    body = await req.json() if req.headers.get("content-length") not in (None, "0") else {}
    st.tl_span_memo.clear()
    try:
        if body.get("id"):
            res = await session.journal.undo_entry(str(body["id"]), session.transport)
        else:
            res = await session.journal.undo_last(session.transport, steps=int(body.get("steps", 1)))
        undone_info = res.get("undone") or {}
        msg = f"Undid [{undone_info.get('actor', 'session')}] {undone_info.get('summary') or res.get('restored_timeline', 'action')}"
        session.bus.publish("transcript", {"type": "notice", "message": msg, "origin": "web"})
        return JSONResponse(res)
    except Exception as err:  # noqa: BLE001
        return JSONResponse({"error": str(err)}, status_code=400)


@router.post("/api/journal/redo")
async def api_journal_redo(req: Request, st: StateDep) -> JSONResponse:
    session = st.session
    body = await req.json() if req.headers.get("content-length") not in (None, "0") else {}
    st.tl_span_memo.clear()
    try:
        if body.get("id"):
            res = await session.journal.redo_entry(str(body["id"]), session.transport)
        else:
            res = await session.journal.redo_next(session.transport, steps=int(body.get("steps", 1)))
        redone_info = res.get("redone") or {}
        msg = f"Redid [{redone_info.get('actor', 'session')}] {redone_info.get('summary', 'action')}"
        session.bus.publish("transcript", {"type": "notice", "message": msg, "origin": "web"})
        return JSONResponse(res)
    except Exception as err:  # noqa: BLE001
        return JSONResponse({"error": str(err)}, status_code=400)


@router.post("/api/journal/goto")
async def api_journal_goto(req: Request, st: StateDep) -> JSONResponse:
    session = st.session
    body = await req.json()
    if body.get("seq") is None:
        return JSONResponse({"error": "'seq' is required"}, status_code=400)
    st.tl_span_memo.clear()
    try:
        res = await session.journal.goto_cursor(int(body["seq"]), session.transport)
        session.bus.publish(
            "transcript",
            {
                "type": "notice",
                "message": (
                    f"Action timeline moved to step #{res['cursor']} "
                    f"(undid {len(res['undone_ids'])}, redid {len(res['redone_ids'])})"
                ),
                "origin": "web",
            },
        )
        return JSONResponse(res)
    except Exception as err:  # noqa: BLE001
        return JSONResponse({"error": str(err)}, status_code=400)


@router.post("/api/resolve/undo")
async def api_resolve_undo(st: StateDep) -> JSONResponse:
    session = st.session
    st.tl_span_memo.clear()
    try:
        res = await session.journal.undo_last(session.transport)
        undone_info = res.get("undone") or {}
        label = undone_info.get("summary") or f"Restored timeline '{res.get('restored_timeline', '')}'"
        session.bus.publish("transcript", {"type": "notice", "message": f"Undid: {label}", "origin": "web"})
        return JSONResponse({"ok": True, **res})
    except Exception as err:  # noqa: BLE001
        return JSONResponse({"error": str(err)}, status_code=400)


@router.post("/api/clip/action")
async def api_clip_action(req: Request, st: StateDep) -> JSONResponse:
    session = st.session
    if session.transport is None:
        return JSONResponse({"error": "DaVinci Resolve is not connected"}, status_code=400)

    body = await req.json()
    action = str(body.get("action", "")).strip()
    clip_id = str(body.get("clip_id", "")).strip()
    if not clip_id:
        return JSONResponse({"error": "clip_id is required"}, status_code=400)

    c_res = await session.transport.call("mediapool.clips", {"recursive": True}, timeout=4.0)
    clip = next((c for c in (c_res.get("clips") or []) if c.get("clip_id") == clip_id), None)
    if not clip or not clip.get("file_path"):
        return JSONResponse({"error": f"Clip {clip_id} has no file_path on disk"}, status_code=404)

    src = Path(str(clip["file_path"])).resolve()
    cache_root = session.cfg.home / "cache"

    try:
        if action == "proxy":
            if body.get("unlink"):
                await session.transport.call("mediapool.link_proxy", {"clip_id": clip_id, "unlink": True})
                return JSONResponse({"ok": True, "unlinked": True})

            from davag.media_intel.ffmpeg import generate_editing_proxy

            height = int(body.get("height", 720))
            default_kbps = {360: 700, 480: 1200, 540: 1800, 720: 3500, 1080: 6000}.get(height, 3500)
            kbps = int(body.get("video_kbps") or default_kbps)
            proxy_dir = cache_root / "resolve_proxies"
            proxy_dir.mkdir(parents=True, exist_ok=True)
            out_file = proxy_dir / f"{src.stem}_{height}p_proxy.mp4"

            res = await asyncio.to_thread(
                generate_editing_proxy, src, output_path=out_file, height=height, video_kbps=kbps
            )
            linked = False
            if body.get("link_in_resolve", True):
                l_res = await session.transport.call(
                    "mediapool.link_proxy", {"clip_id": clip_id, "proxy_path": res["path"]}
                )
                linked = bool(l_res.get("linked"))
            p_url = st.register_media_file(Path(res["path"]))
            return JSONResponse({"ok": True, **res, "linked": linked, "proxy_url": p_url})

        if action == "sheet":
            from davag.media_intel.digest import _digest_key
            from davag.media_intel.ffmpeg import generate_contact_sheet

            cols = int(body.get("cols", 4))
            rows = int(body.get("rows", 4))
            s_sec = float(body["start_sec"]) if body.get("start_sec") not in (None, "") else None
            d_sec = float(body["duration_sec"]) if body.get("duration_sec") not in (None, "") else None
            sheets_dir = cache_root / "contact_sheets"
            sheets_dir.mkdir(parents=True, exist_ok=True)
            out_png = sheets_dir / f"{_digest_key(src)}_{int(s_sec or 0)}_{int(d_sec or 0)}_{cols}x{rows}.png"

            sheet = await asyncio.to_thread(
                generate_contact_sheet, src, output_path=out_png, start_sec=s_sec, duration_sec=d_sec, cols=cols, rows=rows
            )
            if body.get("attach") and session.cfg.model.vision:
                img = load_image_part(sheet["path"])
                session.ledger.add(
                    seg_type="media",
                    label=f"mosaic {src.name} ({cols}x{rows})",
                    role="user",
                    image=img,
                )
            s_url = st.register_media_file(Path(sheet["path"]))
            return JSONResponse({"ok": True, **sheet, "sheet_url": s_url})

        if action == "transcribe":
            from davag.media_intel.transcribe import transcribe_media

            s_sec = float(body["start_sec"]) if body.get("start_sec") not in (None, "") else None
            d_sec = float(body["duration_sec"]) if body.get("duration_sec") not in (None, "") else None
            tr_res = await transcribe_media(
                src,
                cache_dir=cache_root / "transcripts",
                gemini_api_key=session.cfg.keys.get("gemini"),
                openai_api_key=session.cfg.keys.get("openai"),
                language=body.get("language") or None,
                start_sec=s_sec,
                duration_sec=d_sec,
            )
            if body.get("attach", True):
                lines_txt = "\n".join(
                    f"[{s['start_tc']} f{s['start_frame']}..{s['end_frame']}] {s['text']}"
                    for s in (tr_res.get("segments") or [])[:150]
                )
                session.ledger.add(
                    seg_type="op-result",
                    label=f"Transcript: {src.name} ({tr_res['segment_count']} segs)",
                    role="user",
                    text=f"[Transcript for {src.name} (clip_id={clip_id})]\n{lines_txt}",
                )
            return JSONResponse({"ok": True, **tr_res})

        if action == "index":
            from davag.media_intel.digest import analyze_clip_with_scout

            cols = int(body.get("cols", 4))
            rows = int(body.get("rows", 4))
            dig = await analyze_clip_with_scout(
                src,
                cfg=session.cfg,
                cache_dir=cache_root,
                clip_id=clip_id,
                cols=cols,
                rows=rows,
                force_refresh=bool(body.get("force_refresh", True)),
            )
            if body.get("attach", True):
                session.ledger.add(
                    seg_type="op-result",
                    label=f"Clip Digest: {src.name}",
                    role="user",
                    text=f"[Clip Digest for {src.name} (clip_id={clip_id})]\n{dig['summary']}",
                )
            return JSONResponse({"ok": True, **dig})

        return JSONResponse({"error": f"Unknown clip action '{action}'"}, status_code=400)
    except Exception as err:  # noqa: BLE001
        return JSONResponse({"error": str(err)}, status_code=400)


@router.get("/api/playhead")
async def api_playhead_get(st: StateDep) -> dict[str, Any]:
    session = st.session
    if session.transport is None:
        return {"connected": False}
    try:
        from davag.resolve_ops.helpers import tc_to_frames

        status = await session.transport.call("resolve.status", timeout=1.5)
        tl = status.get("timeline") or {}
        proj = status.get("project") or {}
        fps = proj.get("fps") or 30
        ptc = tl.get("playhead_tc") or "01:00:00:00"
        return {
            "connected": True,
            "timeline": tl.get("name"),
            "playhead_tc": ptc,
            "playhead_frame": tc_to_frames(ptc, fps),
            "fps": fps,
        }
    except Exception:  # noqa: BLE001
        return {"connected": False}


@router.get("/api/prompter")
async def api_prompter_get(st: StateDep) -> dict[str, Any]:
    session = st.session
    if session.transport is None:
        return {"connected": False, "error": "DaVinci Resolve not connected"}
    try:
        from davag.resolve_ops.helpers import tc_to_frames

        out_dir = str(session.cfg.cwd / "vo_scripts")
        res = await session.transport.call(
            "timeline.vo_script",
            {"output_dir": out_dir, "prefix": "vo_teleprompter", "import_srt_track": False},
        )
        status = await session.transport.call("resolve.status", timeout=2.0)
        tl = status.get("timeline") or {}
        proj = status.get("project") or {}
        fps = proj.get("fps") or 30
        ptc = tl.get("playhead_tc") or "01:00:00:00"
        return {
            "connected": True,
            **res,
            "playhead_tc": ptc,
            "playhead_frame": tc_to_frames(ptc, fps),
            "fps": fps,
        }
    except Exception as err:  # noqa: BLE001
        return {"connected": False, "error": str(err)}


@router.post("/api/prompter/push")
async def api_prompter_push(req: Request, st: StateDep) -> JSONResponse:
    session = st.session
    if session.transport is None:
        return JSONResponse({"error": "DaVinci Resolve not connected"}, status_code=400)
    body = await req.json()
    try:
        out_dir = str(session.cfg.cwd / "vo_scripts")
        res = await session.transport.call(
            "timeline.vo_script",
            {
                "output_dir": out_dir,
                "prefix": str(body.get("prefix") or "vo_teleprompter"),
                "cues": body.get("cues"),
                "import_srt_track": bool(body.get("import_srt_track", True)),
            },
        )
        return JSONResponse({"ok": True, **res})
    except Exception as err:  # noqa: BLE001
        return JSONResponse({"error": str(err)}, status_code=400)


@router.post("/api/prompter/seek")
async def api_prompter_seek(req: Request, st: StateDep) -> JSONResponse:
    session = st.session
    if session.transport is None:
        return JSONResponse({"error": "DaVinci Resolve not connected"}, status_code=400)
    body = await req.json()
    try:
        params: dict[str, Any] = {}
        if body.get("tc") is not None:
            params["tc"] = str(body["tc"])
        elif body.get("frame") is not None:
            params["frame"] = int(body["frame"])
        res = await session.transport.call("timeline.playhead", params)
        return JSONResponse({"ok": True, **res})
    except Exception as err:  # noqa: BLE001
        return JSONResponse({"error": str(err)}, status_code=400)