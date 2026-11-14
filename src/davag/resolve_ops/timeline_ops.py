"""Timeline inspection, playhead control, clip assembly, and property ops (stdlib only)."""

from __future__ import annotations

from typing import Any

from .helpers import frames_to_tc, lua_dict, lua_list, safe_call, tc_to_frames
from .mediapool_ops import find_media_pool_clip
from .schema import OpContext, OpError, resolve_op

TRACK_TYPES = ("video", "audio", "subtitle")


def find_timeline(proj: Any, *, name: str | None = None, timeline_id: str | None = None, index: int | None = None) -> Any:
    count = int(safe_call(proj, "GetTimelineCount", default=0))
    if index is not None:
        tl = proj.GetTimelineByIndex(int(index))
        if tl is None:
            raise OpError(f"Timeline index {index} out of range (1..{count})", code="TIMELINE_NOT_FOUND")
        return tl

    for idx in range(1, count + 1):
        tl = proj.GetTimelineByIndex(idx)
        if tl is None:
            continue
        if timeline_id and str(safe_call(tl, "GetUniqueId", default="")) == timeline_id:
            return tl
        if name and str(safe_call(tl, "GetName", default="")) == name:
            return tl

    target = name or timeline_id or "<unspecified>"
    raise OpError(f'Timeline "{target}" not found', code="TIMELINE_NOT_FOUND")


def find_timeline_items(tl: Any, item_ids: list[str] | None = None) -> list[tuple[str, int, Any]]:
    """Return `[(track_type, track_index, item), ...]` matching `item_ids` (or all items if None)."""
    want = set(item_ids) if item_ids is not None else None
    found: list[tuple[str, int, Any]] = []
    seen_ids: set[str] = set()

    for ttype in TRACK_TYPES:
        tcount = int(safe_call(tl, "GetTrackCount", ttype, default=0))
        for tidx in range(1, tcount + 1):
            for item in lua_list(safe_call(tl, "GetItemListInTrack", ttype, tidx, default=[])):
                uid = str(safe_call(item, "GetUniqueId", default=""))
                if want is not None and uid not in want:
                    continue
                if uid and uid in seen_ids and want is not None:
                    continue
                if uid:
                    seen_ids.add(uid)
                found.append((ttype, tidx, item))

    if want is not None:
        missing = want - seen_ids
        if missing:
            raise OpError(
                f"Timeline item(s) not found: {', '.join(sorted(missing))}",
                code="ITEM_NOT_FOUND",
            )
    return found


def _serialize_timeline_item(item: Any, fps: Any, *, include_props: bool = False) -> dict[str, Any]:
    rec_start = int(safe_call(item, "GetStart", default=0))
    rec_end = int(safe_call(item, "GetEnd", default=0))
    mpi = safe_call(item, "GetMediaPoolItem")
    markers = safe_call(item, "GetMarkers", default={})
    out: dict[str, Any] = {
        "item_id": str(safe_call(item, "GetUniqueId", default="")),
        "name": str(safe_call(item, "GetName", default="")),
        "media_pool_id": str(safe_call(mpi, "GetUniqueId", default="")) if mpi else None,
        "record_start": rec_start,
        "record_end": rec_end,
        "record_start_tc": frames_to_tc(rec_start, fps),
        "record_end_tc": frames_to_tc(rec_end, fps),
        "duration": int(safe_call(item, "GetDuration", default=max(0, rec_end - rec_start))),
        "source_start": int(safe_call(item, "GetLeftOffset", default=0)),
        "source_end": int(safe_call(item, "GetLeftOffset", default=0))
        + int(safe_call(item, "GetDuration", default=max(0, rec_end - rec_start))),
        "enabled": bool(safe_call(item, "GetClipEnabled", default=True)),
        "clip_color": str(safe_call(item, "GetClipColor", default="")),
        "fusion_comps": int(safe_call(item, "GetFusionCompCount", default=0)),
        "markers_count": len(markers) if isinstance(markers, dict) else 0,
    }
    if include_props:
        raw_props = lua_dict(safe_call(item, "GetProperty", default={}))
        _defaults = {
            "Pan": 0.0,
            "Tilt": 0.0,
            "ZoomX": 1.0,
            "ZoomY": 1.0,
            "ZoomGang": True,
            "RotationAngle": 0.0,
            "AnchorPointX": 0.0,
            "AnchorPointY": 0.0,
            "Pitch": 0.0,
            "Yaw": 0.0,
            "FlipX": False,
            "FlipY": False,
            "CropLeft": 0.0,
            "CropRight": 0.0,
            "CropTop": 0.0,
            "CropBottom": 0.0,
            "CropSoftness": 0.0,
            "CropRetain": False,
            "DynamicZoomEase": 0,
            "CompositeMode": 0,
            "Opacity": 100.0,
            "Distortion": 0.0,
            "RetimeProcess": 0,
            "MotionEstimation": 0,
            "Scaling": 0,
            "ResizeFilter": 0,
            "TransformEnabled": True,
            "CroppingEnabled": True,
            "DynamicZoomEnabled": False,
            "CompositeEnabled": True,
            "LensCorrectionEnabled": True,
            "RetimeAndScalingEnabled": True,
        }
        out["properties"] = {k: v for k, v in raw_props.items() if _defaults.get(k, object()) != v}
    return out


@resolve_op(
    "timeline.list",
    tier="read",
    summary="List all timelines in the open DaVinci Resolve project.",
)
def op_timeline_list(ctx: OpContext, _params: dict[str, Any]) -> dict[str, Any]:
    proj = ctx.project
    cur = safe_call(proj, "GetCurrentTimeline")
    cur_id = str(safe_call(cur, "GetUniqueId", default="")) if cur else None
    count = int(safe_call(proj, "GetTimelineCount", default=0))
    timelines: list[dict[str, Any]] = []
    for idx in range(1, count + 1):
        tl = proj.GetTimelineByIndex(idx)
        if tl is None:
            continue
        uid = str(safe_call(tl, "GetUniqueId", default=""))
        s_frame = int(safe_call(tl, "GetStartFrame", default=0))
        e_frame = int(safe_call(tl, "GetEndFrame", default=0))
        timelines.append(
            {
                "index": idx,
                "timeline_id": uid,
                "name": str(safe_call(tl, "GetName", default="")),
                "active": bool(cur_id and uid == cur_id),
                "start_tc": safe_call(tl, "GetStartTimecode", default="01:00:00:00"),
                "duration_frames": max(0, e_frame - s_frame),
            }
        )
    return {"count": len(timelines), "timelines": timelines}


@resolve_op(
    "timeline.select",
    tier="mutate",
    summary="Switch the active timeline by name, timeline_id, or 1-based index.",
    params_doc={"name": "Timeline name", "timeline_id": "Unique ID", "index": "1-based index"},
)
def op_timeline_select(ctx: OpContext, params: dict[str, Any]) -> dict[str, Any]:
    proj = ctx.project
    tl = find_timeline(
        proj,
        name=params.get("name"),
        timeline_id=params.get("timeline_id"),
        index=int(params["index"]) if params.get("index") is not None else None,
    )
    if not proj.SetCurrentTimeline(tl):
        raise OpError("Resolve refused SetCurrentTimeline", code="SELECT_TIMELINE_FAILED")
    return {
        "timeline_id": str(safe_call(tl, "GetUniqueId", default="")),
        "name": str(safe_call(tl, "GetName", default="")),
        "playhead_tc": safe_call(tl, "GetCurrentTimecode", default=None),
    }


@resolve_op(
    "timeline.get_state",
    tier="read",
    summary="Inspect tracks, clips, frame ranges, and playhead (note: V/A state is already attached ephemerally on every turn; call only for subtitle items or specific filters).",
    params_doc={
        "include_properties": "Optional bool (default false) — include non-default transform/crop/audio properties",
        "track_types": "Optional list of track types (default ['video', 'audio', 'subtitle'])",
        "track_index": "Optional 1-based track index filter",
    },
)
def op_timeline_get_state(ctx: OpContext, params: dict[str, Any]) -> dict[str, Any]:
    proj = ctx.project
    tl = ctx.timeline
    fps = safe_call(proj, "GetSetting", "timelineFrameRate", default="24")
    include_props = bool(params.get("include_properties", False))
    want_types = params.get("track_types") or list(TRACK_TYPES)
    only_tidx = int(params["track_index"]) if params.get("track_index") is not None else None

    tracks_out: dict[str, list[dict[str, Any]]] = {}
    total_items = 0
    for ttype in want_types:
        if ttype not in TRACK_TYPES:
            continue
        tcount = int(safe_call(tl, "GetTrackCount", ttype, default=0))
        tlist: list[dict[str, Any]] = []
        for tidx in range(1, tcount + 1):
            if only_tidx is not None and tidx != only_tidx:
                continue
            raw_items = lua_list(safe_call(tl, "GetItemListInTrack", ttype, tidx, default=[]))
            serialized = [_serialize_timeline_item(it, fps, include_props=include_props) for it in raw_items]
            total_items += len(serialized)
            tlist.append(
                {
                    "track_index": tidx,
                    "name": str(safe_call(tl, "GetTrackName", ttype, tidx, default=f"{ttype[0].upper()}{tidx}")),
                    "enabled": bool(safe_call(tl, "GetIsTrackEnabled", ttype, tidx, default=True)),
                    "locked": bool(safe_call(tl, "GetIsTrackLocked", ttype, tidx, default=False)),
                    "item_count": len(serialized),
                    "items": serialized,
                }
            )
        tracks_out[ttype] = tlist

    s_frame = int(safe_call(tl, "GetStartFrame", default=0))
    e_frame = int(safe_call(tl, "GetEndFrame", default=0))
    playhead_tc = str(safe_call(tl, "GetCurrentTimecode", default="01:00:00:00"))
    markers = safe_call(tl, "GetMarkers", default={})

    return {
        "timeline_id": str(safe_call(tl, "GetUniqueId", default="")),
        "name": str(safe_call(tl, "GetName", default="")),
        "fps": fps,
        "playhead_tc": playhead_tc,
        "playhead_frame": tc_to_frames(playhead_tc, fps),
        "start_frame": s_frame,
        "end_frame": e_frame,
        "duration_frames": max(0, e_frame - s_frame),
        "total_items": total_items,
        "markers_count": len(markers) if isinstance(markers, dict) else 0,
        "tracks": tracks_out,
    }


@resolve_op(
    "timeline.playhead",
    tier="read",
    summary="Get or set the current timeline playhead position (by timecode or frame number).",
    params_doc={
        "tc": "Optional target SMPTE timecode (e.g. '01:00:05:12')",
        "frame": "Optional target absolute timeline frame (e.g. 86532)",
    },
)
def op_timeline_playhead(ctx: OpContext, params: dict[str, Any]) -> dict[str, Any]:
    proj = ctx.project
    tl = ctx.timeline
    fps = safe_call(proj, "GetSetting", "timelineFrameRate", default="24")

    target_tc: str | None = None
    if params.get("tc") is not None:
        target_tc = str(params["tc"]).strip()
    elif params.get("frame") is not None:
        target_tc = frames_to_tc(int(params["frame"]), fps)

    if target_tc is not None and not tl.SetCurrentTimecode(target_tc):
        raise OpError(f'Resolve rejected playhead timecode "{target_tc}"', code="PLAYHEAD_FAILED")

    now_tc = str(safe_call(tl, "GetCurrentTimecode", default="01:00:00:00"))
    return {
        "playhead_tc": now_tc,
        "playhead_frame": tc_to_frames(now_tc, fps),
        "fps": fps,
    }


@resolve_op(
    "timeline.create",
    tier="mutate",
    summary="Create a new empty timeline and make it active.",
    params_doc={"name": "New timeline name"},
)
def op_timeline_create(ctx: OpContext, params: dict[str, Any]) -> dict[str, Any]:
    name = str(params.get("name", "")).strip()
    if not name:
        raise OpError("timeline.create requires 'name'", code="MISSING_PARAM")
    tl = ctx.media_pool.CreateEmptyTimeline(name)
    if tl is None:
        raise OpError(f'Could not create timeline "{name}" (name may already exist)', code="CREATE_FAILED")
    return {
        "timeline_id": str(safe_call(tl, "GetUniqueId", default="")),
        "name": str(safe_call(tl, "GetName", default=name)),
    }


@resolve_op(
    "timeline.duplicate",
    tier="mutate",
    summary="Duplicate the active timeline (used for safe checkpoints or alternate cuts).",
    params_doc={
        "name": "Optional name for the duplicated timeline",
        "switch_to_duplicate": "Optional bool (default false) — keep original active when checkpointing",
    },
)
def op_timeline_duplicate(ctx: OpContext, params: dict[str, Any]) -> dict[str, Any]:
    proj = ctx.project
    orig = ctx.timeline
    dup_name = str(params["name"]).strip() if params.get("name") else None
    dup = orig.DuplicateTimeline(dup_name) if dup_name else orig.DuplicateTimeline()
    if dup is None:
        raise OpError("DuplicateTimeline returned None", code="DUPLICATE_FAILED")

    # Resolve sometimes switches active timeline to the duplicate; honor `switch_to_duplicate` explicitly.
    if bool(params.get("switch_to_duplicate", False)):
        safe_call(proj, "SetCurrentTimeline", dup)
    else:
        safe_call(proj, "SetCurrentTimeline", orig)

    return {
        "original_id": str(safe_call(orig, "GetUniqueId", default="")),
        "original_name": str(safe_call(orig, "GetName", default="")),
        "duplicate_id": str(safe_call(dup, "GetUniqueId", default="")),
        "duplicate_name": str(safe_call(dup, "GetName", default=dup_name or "")),
    }


@resolve_op(
    "timeline.delete_timeline",
    tier="destructive",
    summary="Delete a timeline from the project.",
    params_doc={"name": "Timeline name", "timeline_id": "Timeline unique ID"},
)
def op_timeline_delete_timeline(ctx: OpContext, params: dict[str, Any]) -> dict[str, Any]:
    proj = ctx.project
    mp = ctx.media_pool
    tl = find_timeline(proj, name=params.get("name"), timeline_id=params.get("timeline_id"))
    tname = str(safe_call(tl, "GetName", default=""))
    ok = bool(mp.DeleteTimelines([tl]))
    if not ok:
        raise OpError(f'Failed to delete timeline "{tname}"', code="DELETE_TIMELINE_FAILED")
    return {"deleted": tname}


@resolve_op(
    "timeline.append_clips",
    tier="mutate",
    checkpoint_timeline=True,
    summary="Append clips or frame-accurate subclips to the active timeline.",
    params_doc={
        "clips": (
            "List of dicts: {clip_id or clip_name, start_frame?, end_frame?, "
            "track_index? (1-based), record_frame? or record_tc?, media_type? (1=video, 2=audio)}"
        )
    },
)
def op_timeline_append_clips(ctx: OpContext, params: dict[str, Any]) -> dict[str, Any]:
    raw_clips = params.get("clips")
    if not isinstance(raw_clips, list) or not raw_clips:
        raise OpError("timeline.append_clips requires non-empty list 'clips'", code="MISSING_PARAM")

    proj = ctx.project
    mp = ctx.media_pool
    _ = ctx.timeline  # Ensure a timeline exists before appending
    fps = safe_call(proj, "GetSetting", "timelineFrameRate", default="24")

    clip_infos: list[dict[str, Any]] = []
    for spec in raw_clips:
        if not isinstance(spec, dict):
            raise OpError("Each entry in 'clips' must be an object", code="INVALID_PARAM")
        mpi = find_media_pool_clip(
            mp,
            clip_id=spec.get("clip_id"),
            name=spec.get("clip_name") or spec.get("name"),
        )
        info: dict[str, Any] = {"mediaPoolItem": mpi}
        if spec.get("start_frame") is not None:
            info["startFrame"] = int(spec["start_frame"])
        if spec.get("end_frame") is not None:
            info["endFrame"] = int(spec["end_frame"])
        if spec.get("track_index") is not None:
            info["trackIndex"] = int(spec["track_index"])
        if spec.get("record_frame") is not None:
            info["recordFrame"] = int(spec["record_frame"])
        elif spec.get("record_tc"):
            info["recordFrame"] = tc_to_frames(str(spec["record_tc"]), fps)
        if spec.get("media_type") is not None:
            info["mediaType"] = int(spec["media_type"])
        clip_infos.append(info)

    added = lua_list(mp.AppendToTimeline(clip_infos))
    return {
        "appended_count": len(added),
        "items": [_serialize_timeline_item(it, fps) for it in added],
    }


@resolve_op(
    "timeline.insert_title_or_generator",
    tier="mutate",
    checkpoint_timeline=True,
    summary="Insert a Fusion Title (e.g. 'Text+') or Generator at the playhead/end of timeline.",
    params_doc={
        "kind": "'title' (default) or 'generator'",
        "name": "Title/Generator name, e.g. 'Text+' or 'Solid Color'",
    },
)
def op_timeline_insert_title(ctx: OpContext, params: dict[str, Any]) -> dict[str, Any]:
    proj = ctx.project
    tl = ctx.timeline
    fps = safe_call(proj, "GetSetting", "timelineFrameRate", default="24")
    kind = str(params.get("kind", "title")).strip().lower()
    name = str(params.get("name", "Text+")).strip()

    if kind == "generator":
        item = tl.InsertGeneratorIntoTimeline(name)
    else:
        item = tl.InsertFusionTitleIntoTimeline(name)

    if item is None:
        raise OpError(f'Failed to insert {kind} "{name}" into timeline', code="INSERT_FAILED")

    return {"item": _serialize_timeline_item(item, fps)}


@resolve_op(
    "timeline.set_clip_props",
    tier="mutate",
    checkpoint_timeline=True,
    summary="Set transform, crop, opacity, or composite properties on one or more timeline items.",
    params_doc={
        "item_ids": "List of timeline item_ids",
        "properties": "Dict of Resolve property keys -> values (e.g. {'ZoomX': 1.2, 'ZoomY': 1.2, 'Pan': -40})",
    },
)
def op_timeline_set_clip_props(ctx: OpContext, params: dict[str, Any]) -> dict[str, Any]:
    item_ids = params.get("item_ids")
    props = params.get("properties")
    if not isinstance(item_ids, list) or not item_ids:
        raise OpError("timeline.set_clip_props requires non-empty list 'item_ids'", code="MISSING_PARAM")
    if not isinstance(props, dict) or not props:
        raise OpError("timeline.set_clip_props requires non-empty dict 'properties'", code="MISSING_PARAM")

    tl = ctx.timeline
    matches = find_timeline_items(tl, [str(x) for x in item_ids])
    updated: list[str] = []
    for _ttype, _tidx, item in matches:
        ok = True
        for k, v in props.items():
            if not item.SetProperty(str(k), v):
                ok = False
        if not ok:
            raise OpError(
                f"Resolve rejected one or more properties on item {safe_call(item, 'GetName')}",
                code="SET_PROPERTY_FAILED",
            )
        updated.append(str(safe_call(item, "GetUniqueId", default="")))
    return {"updated_item_ids": updated, "properties": props}


@resolve_op(
    "timeline.set_clip_state",
    tier="mutate",
    checkpoint_timeline=True,
    summary="Enable/disable timeline clips or set their clip color tag.",
    params_doc={
        "item_ids": "List of timeline item_ids",
        "enabled": "Optional bool",
        "clip_color": "Optional Resolve color name (e.g. 'Orange', 'Teal', 'Blue')",
    },
)
def op_timeline_set_clip_state(ctx: OpContext, params: dict[str, Any]) -> dict[str, Any]:
    item_ids = params.get("item_ids")
    if not isinstance(item_ids, list) or not item_ids:
        raise OpError("timeline.set_clip_state requires non-empty list 'item_ids'", code="MISSING_PARAM")

    tl = ctx.timeline
    matches = find_timeline_items(tl, [str(x) for x in item_ids])
    for _ttype, _tidx, item in matches:
        if params.get("enabled") is not None:
            item.SetClipEnabled(bool(params["enabled"]))
        if params.get("clip_color") is not None:
            cc = str(params["clip_color"]).strip()
            if cc == "" and callable(getattr(item, "ClearClipColor", None)):
                item.ClearClipColor()
            else:
                item.SetClipColor(cc)
    return {"updated_count": len(matches)}


@resolve_op(
    "timeline.delete_clips",
    tier="destructive",
    checkpoint_timeline=True,
    summary="Delete clips from the active timeline (with optional ripple delete).",
    params_doc={
        "item_ids": "List of timeline item_ids to delete",
        "ripple": "Optional bool (default false) — close timeline gaps when true",
    },
)
def op_timeline_delete_clips(ctx: OpContext, params: dict[str, Any]) -> dict[str, Any]:
    item_ids = params.get("item_ids")
    if not isinstance(item_ids, list) or not item_ids:
        raise OpError("timeline.delete_clips requires non-empty list 'item_ids'", code="MISSING_PARAM")
    ripple = bool(params.get("ripple", False))

    tl = ctx.timeline
    matches = find_timeline_items(tl, [str(x) for x in item_ids])
    items = [it for _tt, _ti, it in matches]
    ok = bool(tl.DeleteClips(items, ripple))
    if not ok:
        raise OpError("Timeline.DeleteClips returned False", code="DELETE_CLIPS_FAILED")
    return {"deleted_count": len(items), "ripple": ripple}


@resolve_op(
    "timeline.import_timeline_file",
    tier="mutate",
    summary="Import an OTIO, FCPXML, EDL, or AAF timeline file into the project.",
    params_doc={
        "file_path": "Absolute path to .otio, .fcpxml, .xml, .edl, or .aaf file",
        "timeline_name": "Optional name for the imported timeline",
        "import_source_clips": "Optional bool (default true)",
    },
)
def op_timeline_import_file(ctx: OpContext, params: dict[str, Any]) -> dict[str, Any]:
    file_path = str(params.get("file_path", "")).strip()
    if not file_path:
        raise OpError("timeline.import_timeline_file requires 'file_path'", code="MISSING_PARAM")

    mp = ctx.media_pool
    import_opts: dict[str, Any] = {
        "importSourceClips": bool(params.get("import_source_clips", True)),
    }
    if params.get("timeline_name"):
        import_opts["timelineName"] = str(params["timeline_name"])

    tl = mp.ImportTimelineFromFile(file_path, import_opts)
    if tl is None:
        raise OpError(f'Resolve failed to import timeline from "{file_path}"', code="IMPORT_TIMELINE_FAILED")
    return {
        "timeline_id": str(safe_call(tl, "GetUniqueId", default="")),
        "name": str(safe_call(tl, "GetName", default="")),
    }


@resolve_op(
    "timeline.track_state",
    tier="mutate",
    summary="Add, rename, lock/unlock, or mute/enable a video, audio, or subtitle track.",
    params_doc={
        "track_type": "'video' | 'audio' | 'subtitle'",
        "track_index": "1-based track index (omit if add_track=true)",
        "add_track": "Optional bool (default false) — add a new track of track_type",
        "sub_type": "Optional audio track sub-type ('mono', 'stereo', '5.1', 'adaptive_1'..)",
        "name": "Optional new track name",
        "enabled": "Optional bool — enable/unmute (true) or disable/mute (false)",
        "locked": "Optional bool — lock (true) or unlock (false) track",
    },
)
def op_timeline_track_state(ctx: OpContext, params: dict[str, Any]) -> dict[str, Any]:
    tl = ctx.timeline
    ttype = str(params.get("track_type", "video")).strip().lower()
    if ttype not in TRACK_TYPES:
        raise OpError(f'Invalid track_type "{ttype}"', code="INVALID_PARAM")

    if bool(params.get("add_track", False)):
        sub_type = str(params.get("sub_type", "stereo"))
        ok = bool(tl.AddTrack(ttype, sub_type) if ttype == "audio" else tl.AddTrack(ttype))
        if not ok:
            raise OpError(f"Resolve failed to add {ttype} track", code="ADD_TRACK_FAILED")
        tidx = int(safe_call(tl, "GetTrackCount", ttype, default=1))
    else:
        if params.get("track_index") is None:
            raise OpError("timeline.track_state requires 'track_index' (or add_track=true)", code="MISSING_PARAM")
        tidx = int(params["track_index"])

    if params.get("name") is not None:
        safe_call(tl, "SetTrackName", ttype, tidx, str(params["name"]))
    if params.get("enabled") is not None:
        safe_call(tl, "SetTrackEnable", ttype, tidx, bool(params["enabled"]))
    if params.get("locked") is not None:
        safe_call(tl, "SetTrackLock", ttype, tidx, bool(params["locked"]))

    return {
        "track_type": ttype,
        "track_index": tidx,
        "name": str(safe_call(tl, "GetTrackName", ttype, tidx, default=f"{ttype[0].upper()}{tidx}")),
        "enabled": bool(safe_call(tl, "GetIsTrackEnabled", ttype, tidx, default=True)),
        "locked": bool(safe_call(tl, "GetIsTrackLocked", ttype, tidx, default=False)),
    }


@resolve_op(
    "timeline.create_subtitles",
    tier="mutate",
    checkpoint_timeline=True,
    summary="Generate a subtitle track automatically from timeline audio using Resolve Studio's neural engine.",
    params_doc={
        "max_chars": "Optional max characters per line (default 42)",
        "lines": "Optional 1 or 2 subtitle lines (default 2)",
    },
)
def op_timeline_create_subtitles(ctx: OpContext, params: dict[str, Any]) -> dict[str, Any]:
    tl = ctx.timeline
    fn = getattr(tl, "CreateSubtitlesFromAudio", None)
    if not callable(fn):
        raise OpError(
            "CreateSubtitlesFromAudio is not available (requires DaVinci Resolve Studio 18.5+)",
            code="UNSUPPORTED",
        )
    opts: dict[str, Any] = {}
    if params.get("max_chars") is not None:
        opts["captionPreset"] = "Netflix" if int(params["max_chars"]) <= 42 else "Default"
        opts["charactersPerLine"] = int(params["max_chars"])
    if params.get("lines") is not None:
        opts["lineBreak"] = "Single" if int(params["lines"]) == 1 else "Double"

    ok = bool(fn(opts) if opts else fn())
    if not ok:
        raise OpError("CreateSubtitlesFromAudio returned False", code="SUBTITLES_FAILED")
    sub_tracks = int(safe_call(tl, "GetTrackCount", "subtitle", default=0))
    return {"created": True, "subtitle_tracks": sub_tracks}