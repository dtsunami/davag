"""Voiceover Teleprompter / Fairlight ADR Cue generator + DaVinci Resolve 21.1–21.2 NLE ops (stdlib only).

Implements:
- `timeline.vo_script`: Converts timeline markers (or custom beat lines) into BOTH a Fairlight ADR
  Cue Sheet `.csv` AND a synced `.srt` teleprompter file, optionally importing onto a Subtitle track!
- `timeline.import_srt`: Imports `.srt` captions directly onto the active timeline.
- `timeline.trim_and_move`: Resolve 21.1.2+ in-place `SetLeftOffset`, `SetRightOffset`, `SetPosition`,
  `DeleteGaps`, and `RippleDeleteSilence`.
- `timeline.transitions_and_fades`: Resolve 21.1+ `AddTransition` (Cross Dissolve, etc.) & `SetFades`.
- `timeline.set_speed`: Resolve 21.1+ `SetSpeed` (% speed + ripple), `Stabilize`, and `SmartReframe`.
- `color.grade`: Resolve 21.2 `ApplyAutoColor`, `ApplyGradeFromDRX`, `GetCDL`, and `ResetAllGrades`.
- `audio.enhance`: Resolve 21.1+ `NormalizeAudioLevel` (-14 LKFS), `VoiceIsolation`, `DialogueLeveler`,
  `AnalyzeAudioLevels`, and `GenerateSpeech` (Studio Neural TTS scratch VO).
"""

from __future__ import annotations

import csv
import math
from pathlib import Path
from typing import Any

from .helpers import frames_to_tc, lua_list, parse_fps, safe_call, tc_to_frames
from .schema import OpContext, OpError, resolve_op
from .timeline_ops import find_timeline_items


def _frames_to_srt_ts(abs_frames: int, fps_raw: Any) -> str:
    """Convert absolute frames to SRT timestamp `HH:MM:SS,mmm`."""
    exact_fps, _, _ = parse_fps(fps_raw)
    total_sec = max(0.0, abs_frames / exact_fps)
    hh = int(total_sec // 3600)
    rem = total_sec - hh * 3600
    mm = int(rem // 60)
    sec_rem = rem - mm * 60
    ss = math.floor(sec_rem)
    ms = min(999, round((sec_rem - ss) * 1000))
    return f"{hh:02d}:{mm:02d}:{ss:02d},{ms:03d}"


def build_vo_beats_from_timeline(
    ctx: OpContext, custom_cues: list[dict[str, Any]] | None = None
) -> tuple[list[dict[str, Any]], float, int, int]:
    """Extract chronological voiceover beats `[in_frame, out_frame, in_tc, out_tc, title, script]`."""
    proj = ctx.project
    tl = ctx.timeline
    fps_raw = safe_call(proj, "GetSetting", "timelineFrameRate", default="30")
    exact_fps, _, _ = parse_fps(fps_raw)
    tl_start = int(safe_call(tl, "GetStartFrame", default=0))
    # Compute picture end from Video tracks first so stray audio/subtitle items never inflate final beat!
    vid_ends: list[int] = []
    for v_idx in range(1, int(safe_call(tl, "GetTrackCount", "video", default=0)) + 1):
        for v_it in lua_list(safe_call(tl, "GetItemListInTrack", "video", v_idx, default=[])):
            vid_ends.append(int(safe_call(v_it, "GetEnd", default=tl_start)))
    tl_end = max(vid_ends) if vid_ends else int(safe_call(tl, "GetEndFrame", default=tl_start))

    raw_markers = safe_call(tl, "GetMarkers", default={}) or {}
    sorted_offsets = sorted(int(k) for k in raw_markers)

    override_by_idx: dict[int, str] = {}
    override_by_tc: dict[str, str] = {}
    if custom_cues:
        for idx, c in enumerate(custom_cues, start=1):
            txt = str(c.get("script") or c.get("text") or "").strip()
            if not txt:
                continue
            if c.get("cue") is not None:
                override_by_idx[int(c["cue"])] = txt
            if c.get("in_tc"):
                override_by_tc[str(c["in_tc"]).strip()] = txt
            override_by_idx.setdefault(idx, txt)

    beats: list[dict[str, Any]] = []
    for i, off in enumerate(sorted_offsets):
        info = raw_markers[off] if off in raw_markers else raw_markers.get(str(off), {})
        abs_in = tl_start + off
        abs_out = (tl_start + sorted_offsets[i + 1]) if (i + 1 < len(sorted_offsets)) else tl_end
        if abs_out <= abs_in:
            abs_out = abs_in + round(exact_fps * 3)

        in_tc = frames_to_tc(abs_in, fps_raw)
        out_tc = frames_to_tc(abs_out, fps_raw)
        dur_sec = round((abs_out - abs_in) / exact_fps, 2)
        title = str(info.get("name", f"Beat {i + 1}")).strip()
        note = str(info.get("note", "")).strip()
        cue_num = i + 1
        script_text = (
            override_by_tc.get(in_tc)
            or override_by_idx.get(cue_num)
            or (f"{title}: {note}" if note else title)
        )
        word_budget = max(5, round(dur_sec * 2.35))
        beats.append(
            {
                "cue": cue_num,
                "color": str(info.get("color", "Cyan")),
                "title": title,
                "note": note,
                "script": script_text,
                "in_frame": abs_in,
                "out_frame": abs_out,
                "in_tc": in_tc,
                "out_tc": out_tc,
                "duration_sec": dur_sec,
                "recommended_words": word_budget,
            }
        )
    return beats, exact_fps, tl_start, tl_end


@resolve_op(
    "timeline.vo_script",
    tier="mutate",
    summary=(
        "Build a synced Voiceover Teleprompter from timeline markers: writes Fairlight ADR Cue Sheet `.csv` "
        "(for Fairlight -> ADR on-screen voiceover mode) + `.srt` captions, and optionally imports the `.srt` "
        "onto a Subtitle track so cues display right on the DaVinci Resolve Viewer!"
    ),
    params_doc={
        "output_dir": "Optional directory for generated .csv and .srt files (defaults to project cwd)",
        "prefix": "Optional filename prefix (default 'vo_teleprompter')",
        "cues": "Optional list of {cue?, in_tc?, script} overrides with polished spoken lines",
        "character": "Optional speaker name for Fairlight ADR CSV (default 'Narrator')",
        "import_srt_track": "Optional bool (default true) — import .srt onto active timeline Subtitle track",
    },
)
def op_timeline_vo_script(ctx: OpContext, params: dict[str, Any]) -> dict[str, Any]:
    tl = ctx.timeline
    proj = ctx.project
    mp = ctx.media_pool
    fps_raw = safe_call(proj, "GetSetting", "timelineFrameRate", default="30")

    # If replacing an existing VO Teleprompter subtitle track, remove it BEFORE reading GetEndFrame()
    # so stale subtitle extents never inflate the final beat's out_frame!
    want_import = bool(params.get("import_srt_track", True))
    if want_import and int(safe_call(tl, "GetTrackCount", "subtitle", default=0)) >= 1:
        safe_call(tl, "DeleteTrack", "subtitle", 1)

    beats, _exact_fps, tl_start, _tl_end = build_vo_beats_from_timeline(
        ctx, custom_cues=params.get("cues") if isinstance(params.get("cues"), list) else None
    )
    if not beats:
        raise OpError("Active timeline has no markers to build a VO script from.", code="NO_MARKERS")

    out_dir = Path(str(params.get("output_dir") or Path.cwd())).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    prefix = str(params.get("prefix") or "vo_teleprompter").strip()
    speaker = str(params.get("character") or "Narrator").strip()

    csv_path = out_dir / f"{prefix}_fairlight_adr.csv"
    srt_path = out_dir / f"{prefix}_captions.srt"

    # 1. Write Fairlight ADR Cue Sheet CSV (Fairlight -> ADR -> Setup -> Import Cue List)
    with open(csv_path, "w", encoding="utf-8", newline="") as f_csv:
        writer = csv.writer(f_csv)
        writer.writerow(["Cue #", "Character", "In", "Out", "Dialog"])
        for b in beats:
            writer.writerow([b["cue"], speaker, b["in_tc"], b["out_tc"], b["script"]])

    # 2. Write 00:00:00-relative SRT (aligns to `recordFrame: tl_start` in Resolve AND works on YouTube!)
    srt_blocks: list[str] = []
    for b in beats:
        z_in = _frames_to_srt_ts(b["in_frame"] - tl_start, fps_raw)
        z_out = _frames_to_srt_ts(max(b["in_frame"] - tl_start + 5, b["out_frame"] - tl_start - 2), fps_raw)
        srt_blocks.append(f"{b['cue']}\n{z_in} --> {z_out}\n{b['script']}\n")
    srt_path.write_text("\n".join(srt_blocks), encoding="utf-8")
    yt_srt_path = srt_path

    imported_srt = False
    if want_import:
        if int(safe_call(tl, "GetTrackCount", "subtitle", default=0)) == 0:
            safe_call(tl, "AddTrack", "subtitle")
            safe_call(tl, "SetTrackName", "subtitle", 1, "VO Teleprompter")

        import tempfile

        # Import via a fresh unique temp `.srt` so Resolve never reuses a cached MediaPool SRT!
        with tempfile.NamedTemporaryFile(
            prefix="davag_vo_srt_", suffix=".srt", mode="w", encoding="utf-8", delete=False
        ) as tmp_srt:
            tmp_srt.write("\n".join(srt_blocks))
            tmp_srt_path = Path(tmp_srt.name)

        try:
            imported_items = lua_list(safe_call(mp, "ImportMedia", [str(tmp_srt_path)], default=[]))
            if imported_items:
                appended = lua_list(
                    safe_call(
                        mp,
                        "AppendToTimeline",
                        [{"mediaPoolItem": imported_items[0], "recordFrame": tl_start}],
                        default=[],
                    )
                )
                imported_srt = bool(appended)
                safe_call(mp, "DeleteClips", imported_items)
        finally:
            tmp_srt_path.unlink(missing_ok=True)

    return {
        "timeline": str(safe_call(tl, "GetName", default="")),
        "beat_count": len(beats),
        "fairlight_adr_csv": str(csv_path),
        "resolve_srt": str(srt_path),
        "youtube_srt": str(yt_srt_path),
        "imported_onto_timeline": imported_srt,
        "beats": beats,
    }


@resolve_op(
    "timeline.import_srt",
    tier="mutate",
    checkpoint_timeline=True,
    summary="Import an `.srt` subtitle file directly onto a Subtitle track on the active timeline.",
    params_doc={
        "file_path": "Path to .srt file on disk",
        "track_name": "Optional name for subtitle track (default 'Subtitles')",
    },
)
def op_timeline_import_srt(ctx: OpContext, params: dict[str, Any]) -> dict[str, Any]:
    srt_str = str(params.get("file_path", "")).strip()
    if not srt_str:
        raise OpError("timeline.import_srt requires 'file_path'", code="MISSING_PARAM")
    srt_p = Path(srt_str).resolve()
    if not srt_p.is_file():
        raise OpError(f"SRT file not found: {srt_p}", code="FILE_NOT_FOUND")

    tl = ctx.timeline
    tl_start = int(safe_call(tl, "GetStartFrame", default=0))
    if int(safe_call(tl, "GetTrackCount", "subtitle", default=0)) == 0:
        safe_call(tl, "AddTrack", "subtitle")
        safe_call(tl, "SetTrackName", "subtitle", 1, str(params.get("track_name") or "Subtitles"))

    mp = ctx.media_pool
    cur_folder = safe_call(mp, "GetCurrentFolder") or mp.GetRootFolder()
    stale = [
        c
        for c in lua_list(safe_call(cur_folder, "GetClipList", default=[]))
        if str(safe_call(c, "GetName", default="")) == srt_p.name
    ]
    if stale:
        safe_call(mp, "DeleteClips", stale)
    imported_items = lua_list(safe_call(mp, "ImportMedia", [str(srt_p)], default=[]))
    ok = False
    if imported_items:
        res_app = lua_list(
            safe_call(
                mp,
                "AppendToTimeline",
                [{"mediaPoolItem": imported_items[0], "recordFrame": tl_start}],
                default=[],
            )
        )
        ok = bool(res_app)
    return {"imported": ok, "file_path": str(srt_p), "subtitle_tracks": int(safe_call(tl, "GetTrackCount", "subtitle", default=0))}


@resolve_op(
    "timeline.trim_and_move",
    tier="mutate",
    checkpoint_timeline=True,
    summary=(
        "In-place Resolve 21.1.2+ timeline editing: extend/trim clip tail (`extend_end_frames`), "
        "set source offsets (`left_offset`, `right_offset`), move clips, delete gaps, or ripple-delete silence!"
    ),
    params_doc={
        "item_id": "Optional timeline item_id for single-clip trim/move/silence-strip",
        "extend_end_frames": "Preferred delta frames to lengthen (+N) or shorten (-N) clip tail!",
        "extend_start_frames": "Optional delta frames to lengthen (+N) or shorten (-N) clip head",
        "left_offset": "Raw source start frame (`SetLeftOffset`)",
        "right_offset": "Raw remaining handle from END of source media (`SetRightOffset` — prefer `extend_end_frames`!)",
        "record_tc": "Optional new timeline record TC for moving `item_id`",
        "record_frame": "Optional new timeline record frame for moving `item_id`",
        "track_index": "Optional destination 1-based track index when moving",
        "update_linked": "Optional bool (default true)",
        "delete_gaps": "Optional bool — close all gaps across the active timeline (`Timeline.DeleteGaps`)",
        "ripple_delete_silence": "Optional bool — run `RippleDeleteSilence` on `item_id`",
        "silence_threshold_db": "Optional float dB for `ripple_delete_silence` (default -30.0)",
    },
)
def op_timeline_trim_and_move(ctx: OpContext, params: dict[str, Any]) -> dict[str, Any]:
    tl = ctx.timeline
    fps = safe_call(ctx.project, "GetSetting", "timelineFrameRate", default="30")
    linked = bool(params.get("update_linked", True))
    actions: dict[str, Any] = {}

    if params.get("item_id"):
        item = find_timeline_items(tl, [str(params["item_id"])])[0][2]
        if params.get("extend_start_frames") is not None:
            fn = getattr(item, "SetLeftOffset", None)
            if not callable(fn):
                raise OpError("SetLeftOffset requires DaVinci Resolve 21.1.2+", code="UNSUPPORTED")
            cur_l = float(safe_call(item, "GetLeftOffset", default=0.0))
            new_l = max(0.0, cur_l - float(params["extend_start_frames"]))
            actions["extend_start_frames"] = bool(fn(new_l, False, linked))
            actions["new_left_offset"] = new_l

        if params.get("extend_end_frames") is not None:
            fn = getattr(item, "SetRightOffset", None)
            if not callable(fn):
                raise OpError("SetRightOffset requires DaVinci Resolve 21.1.2+", code="UNSUPPORTED")
            cur_r = float(safe_call(item, "GetRightOffset", default=0.0))
            new_r = max(0.0, cur_r - float(params["extend_end_frames"]))
            actions["extend_end_frames"] = bool(fn(new_r, False, linked))
            actions["new_right_offset"] = new_r

        if params.get("left_offset") is not None:
            fn = getattr(item, "SetLeftOffset", None)
            if not callable(fn):
                raise OpError("SetLeftOffset requires DaVinci Resolve 21.1.2+", code="UNSUPPORTED")
            actions["set_left_offset"] = bool(fn(float(params["left_offset"]), False, linked))

        if params.get("right_offset") is not None:
            fn = getattr(item, "SetRightOffset", None)
            if not callable(fn):
                raise OpError("SetRightOffset requires DaVinci Resolve 21.1.2+", code="UNSUPPORTED")
            actions["set_right_offset"] = bool(fn(float(params["right_offset"]), False, linked))

        if params.get("record_frame") is not None or params.get("record_tc") or params.get("track_index") is not None:
            fn = getattr(item, "SetPosition", None)
            if not callable(fn):
                raise OpError("SetPosition requires DaVinci Resolve 21.1.2+", code="UNSUPPORTED")
            pos: dict[str, Any] = {}
            if params.get("track_index") is not None:
                pos["trackIndex"] = int(params["track_index"])
            if params.get("record_frame") is not None:
                pos["startFrame"] = float(params["record_frame"])
            elif params.get("record_tc"):
                pos["startFrame"] = float(tc_to_frames(str(params["record_tc"]), fps))
            actions["moved"] = bool(fn(pos, linked, False))

        if bool(params.get("ripple_delete_silence", False)):
            fn = getattr(item, "RippleDeleteSilence", None)
            if not callable(fn):
                raise OpError("RippleDeleteSilence requires DaVinci Resolve Studio 21.1.2+", code="UNSUPPORTED")
            opts = {"threshold": float(params.get("silence_threshold_db", -30.0)), "crossFadeAudio": True}
            actions["ripple_delete_silence"] = bool(fn(opts))

    if bool(params.get("delete_gaps", False)):
        fn = getattr(tl, "DeleteGaps", None)
        if not callable(fn):
            raise OpError("DeleteGaps requires DaVinci Resolve 21.1.2+", code="UNSUPPORTED")
        actions["deleted_gaps"] = bool(fn())

    return actions


@resolve_op(
    "timeline.transitions_and_fades",
    tier="mutate",
    checkpoint_timeline=True,
    summary="Add transitions (e.g. Cross Dissolve) and/or FadeIn/FadeOut frames to clips (Resolve 21.1+).",
    params_doc={
        "item_ids": "Optional list of timeline item_ids (or pass all_v1=true for every clip on Video 1)",
        "all_v1": "Optional bool — target all clips on Video Track 1",
        "transition_type": "Optional transition name, e.g. 'Cross Dissolve'",
        "category": "Optional 'simple' | 'fusion' | 'ofx' | 'audio' (default 'simple')",
        "position": "'start' | 'end' (default 'end')",
        "duration_frames": "Optional transition duration in frames (e.g. 12)",
        "fade_in": "Optional FadeIn duration in frames",
        "fade_out": "Optional FadeOut duration in frames",
    },
)
def op_timeline_transitions_and_fades(ctx: OpContext, params: dict[str, Any]) -> dict[str, Any]:
    tl = ctx.timeline
    if bool(params.get("all_v1", False)):
        items = lua_list(safe_call(tl, "GetItemListInTrack", "video", 1, default=[]))
    else:
        ids = params.get("item_ids")
        if not isinstance(ids, list) or not ids:
            raise OpError("Provide 'item_ids' or set 'all_v1': true", code="MISSING_PARAM")
        items = [it for _tt, _ti, it in find_timeline_items(tl, [str(x) for x in ids])]

    tr_added = 0
    fades_set = 0
    tr_type = str(params.get("transition_type") or "").strip()

    for idx, it in enumerate(items):
        if tr_type:
            # Skip 'end' transition on final clip if all_v1=true unless requested explicitly
            if bool(params.get("all_v1", False)) and params.get("position", "end") == "end" and idx == len(items) - 1:
                pass
            else:
                add_fn = getattr(it, "AddTransition", None)
                if callable(add_fn):
                    t_opts: dict[str, Any] = {
                        "type": tr_type,
                        "category": str(params.get("category", "simple")),
                        "position": str(params.get("position", "end")),
                        "alignment": str(params.get("alignment", "center")),
                    }
                    if params.get("duration_frames") is not None:
                        t_opts["duration"] = int(params["duration_frames"])
                    if add_fn(t_opts) is not None:
                        tr_added += 1

        if params.get("fade_in") is not None or params.get("fade_out") is not None:
            fade_fn = getattr(it, "SetFades", None)
            if callable(fade_fn):
                cur = safe_call(it, "GetFades", default={}) or {}
                f_info = {
                    "FadeIn": int(params["fade_in"]) if params.get("fade_in") is not None else int(cur.get("FadeIn", 0)),
                    "FadeOut": int(params["fade_out"]) if params.get("fade_out") is not None else int(cur.get("FadeOut", 0)),
                }
                if fade_fn(f_info):
                    fades_set += 1

    return {"target_clips": len(items), "transitions_added": tr_added, "fades_updated": fades_set}


@resolve_op(
    "timeline.set_speed",
    tier="mutate",
    checkpoint_timeline=True,
    summary="Set constant speed %, pitch correction, ripple retime, stabilization, or SmartReframe on clips (Resolve 21.1+).",
    params_doc={
        "item_ids": "List of timeline item_ids",
        "percentage": "Optional speed percentage (e.g. 200.0 for 2x fast-forward, 50.0 for half-speed, 0.0 for freeze)",
        "ripple": "Optional bool (default true) — ripple subsequent clips on the timeline",
        "pitch_correction": "Optional bool (default true)",
        "stabilize": "Optional bool — run Resolve camera stabilization",
        "smart_reframe": "Optional bool — run Studio AI Smart Reframe (e.g. for 9:16 vertical)",
    },
)
def op_timeline_set_speed(ctx: OpContext, params: dict[str, Any]) -> dict[str, Any]:
    ids = params.get("item_ids")
    if not isinstance(ids, list) or not ids:
        raise OpError("timeline.set_speed requires non-empty 'item_ids'", code="MISSING_PARAM")
    matches = find_timeline_items(ctx.timeline, [str(x) for x in ids])
    updated = 0
    for _tt, _ti, it in matches:
        if params.get("percentage") is not None:
            sp_fn = getattr(it, "SetSpeed", None)
            if not callable(sp_fn):
                raise OpError("SetSpeed requires DaVinci Resolve 21.1+", code="UNSUPPORTED")
            s_opts = {
                "Percentage": float(params["percentage"]),
                "PitchCorrection": bool(params.get("pitch_correction", True)),
                "RippleTimeline": bool(params.get("ripple", True)),
            }
            if sp_fn(s_opts):
                updated += 1
        if bool(params.get("stabilize", False)):
            safe_call(it, "Stabilize")
        if bool(params.get("smart_reframe", False)):
            safe_call(it, "SmartReframe")
    return {"updated_count": updated, "item_count": len(matches)}


@resolve_op(
    "color.grade",
    tier="mutate",
    checkpoint_timeline=True,
    summary="Apply Auto-Color (21.2), DRX grade still, or reset grades, and read back CDL/LUT node state.",
    params_doc={
        "item_ids": "List of timeline item_ids",
        "auto_color": "Optional bool — run Resolve 21.2 AI Auto Color",
        "drx_path": "Optional path to .drx grade still to apply",
        "reset": "Optional bool — reset all color nodes on target clips",
        "inspect": "Optional bool — read back node count, LUTs, and CDL values",
    },
)
def op_color_grade(ctx: OpContext, params: dict[str, Any]) -> dict[str, Any]:
    ids = params.get("item_ids")
    if not isinstance(ids, list) or not ids:
        raise OpError("color.grade requires non-empty 'item_ids'", code="MISSING_PARAM")
    matches = find_timeline_items(ctx.timeline, [str(x) for x in ids])
    inspected: list[dict[str, Any]] = []

    for _tt, _ti, it in matches:
        graph = safe_call(it, "GetNodeGraph")
        if bool(params.get("reset", False)) and graph is not None:
            safe_call(graph, "ResetAllGrades")
        if bool(params.get("auto_color", False)):
            safe_call(it, "ApplyAutoColor")
        if params.get("drx_path") and graph is not None:
            safe_call(graph, "ApplyGradeFromDRX", str(Path(str(params["drx_path"])).resolve()), 0)
        if bool(params.get("inspect", False)):
            n_nodes = int(safe_call(graph, "GetNumNodes", default=1)) if graph else 1
            nodes_info = []
            for ni in range(1, n_nodes + 1):
                nodes_info.append(
                    {
                        "node": ni,
                        "label": safe_call(graph, "GetNodeLabel", ni, default="") if graph else "",
                        "lut": safe_call(graph, "GetLUT", ni, default="") if graph else "",
                        "cdl": safe_call(it, "GetCDL", ni, default=None),
                    }
                )
            inspected.append({"item_id": str(safe_call(it, "GetUniqueId", default="")), "nodes": nodes_info})

    return {"count": len(matches), **({"inspected": inspected} if inspected else {})}


@resolve_op(
    "audio.enhance",
    tier="mutate",
    checkpoint_timeline=True,
    summary=(
        "Fairlight audio loudness & AI speech suite: measure LUFS (`analyze`), normalize timeline audio "
        "(e.g. YouTube `-14.0` LKFS), enable Studio Voice Isolation / Dialogue Leveler, or synthesize scratch TTS VO!"
    ),
    params_doc={
        "track_index": "Optional 1-based audio track index (default 2 for Narration VO, or 1)",
        "item_ids": "Optional explicit audio timeline item_ids (defaults to all items on `track_index`)",
        "analyze_lufs": "Optional bool — measure TruePeak dBFS & Integrated LUFS per clip",
        "normalize_lkfs": "Optional float target loudness (e.g. -14.0 for YouTube)",
        "voice_isolation": "Optional int 0..100 — enable Studio Neural Voice Isolation on `track_index` (0 disables)",
        "dialogue_leveler": "Optional bool — enable Resolve Dialogue Leveler on target audio clips",
        "tts_text": "Optional string (<=350 chars) — synthesize scratch voiceover via Resolve 21 `GenerateSpeech` at `tts_tc`",
        "tts_tc": "Optional record timecode for `tts_text`",
    },
)
def op_audio_enhance(ctx: OpContext, params: dict[str, Any]) -> dict[str, Any]:
    tl = ctx.timeline
    proj = ctx.project
    tidx = int(params.get("track_index", 1))
    out: dict[str, Any] = {}

    if params.get("tts_text"):
        gen_fn = getattr(proj, "GenerateSpeech", None)
        if not callable(gen_fn):
            raise OpError("GenerateSpeech requires DaVinci Resolve Studio 21+ with Speech Extras pack", code="UNSUPPORTED")
        s_cfg = {
            "TextInput": str(params["tts_text"])[:350],
            "VoiceModel": str(params.get("tts_voice", "Male 1")),
            "AddToTimeline": True,
            "AudioTrack": tidx,
        }
        mpi = gen_fn(s_cfg, str(params["tts_tc"]) if params.get("tts_tc") else None)
        out["generated_speech_clip"] = str(safe_call(mpi, "GetName", default="")) if mpi else None

    if params.get("voice_isolation") is not None:
        amt = max(0, min(100, int(params["voice_isolation"])))
        vi_fn = getattr(tl, "SetVoiceIsolationState", None)
        if callable(vi_fn):
            out["voice_isolation_track"] = bool(vi_fn(tidx, {"isEnabled": amt > 0, "amount": amt}))

    if params.get("item_ids"):
        items = [it for _tt, _ti, it in find_timeline_items(tl, [str(x) for x in params["item_ids"]])]
    else:
        items = lua_list(safe_call(tl, "GetItemListInTrack", "audio", tidx, default=[]))

    if items and params.get("dialogue_leveler") is not None:
        en = bool(params["dialogue_leveler"])
        for it in items:
            safe_call(it, "SetProperties", {"AudioDialogueLevelerEnabled": en})
        out["dialogue_leveler_clips"] = len(items)

    if items and params.get("normalize_lkfs") is not None:
        norm_fn = getattr(tl, "NormalizeAudioLevel", None)
        if callable(norm_fn):
            modes = lua_list(safe_call(tl, "GetNormalizeAudioModes", default=[]))
            yt_mode = next((m for m in modes if "YouTube" in m or "ITU" in m or "EBU" in m), modes[0] if modes else "ITU-R BS.1770-4")
            n_opts = {
                "normalizationMode": yt_mode,
                "targetLoudness": float(params["normalize_lkfs"]),
                "targetLevel": -1.5,
            }
            out["normalized"] = bool(norm_fn(items, n_opts))

    if items and bool(params.get("analyze_lufs", False)):
        levels = []
        for it in items:
            lv = safe_call(it, "AnalyzeAudioLevels", "ITU-R BS.1770-4", default={})
            levels.append({"name": str(safe_call(it, "GetName", default="")), "levels": lv})
        out["measured_lufs"] = levels

    return out