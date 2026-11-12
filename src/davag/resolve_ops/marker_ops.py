"""Timeline and clip marker operations (100% Python stdlib only)."""

from __future__ import annotations

from typing import Any

from .helpers import frames_to_tc, safe_call, tc_to_frames
from .mediapool_ops import find_media_pool_clip
from .schema import OpContext, OpError, resolve_op
from .timeline_ops import find_timeline_items


def _resolve_marker_target(ctx: OpContext, params: dict[str, Any]) -> tuple[Any, str, Any]:
    """Return `(target_obj, target_kind, fps)` for timeline, timeline_item, or mediapool_clip."""
    proj = ctx.project
    fps = safe_call(proj, "GetSetting", "timelineFrameRate", default="24")
    if params.get("item_id"):
        matches = find_timeline_items(ctx.timeline, [str(params["item_id"])])
        return matches[0][2], "timeline_item", fps
    if params.get("clip_id"):
        clip = find_media_pool_clip(ctx.media_pool, clip_id=str(params["clip_id"]))
        return clip, "mediapool_clip", fps
    return ctx.timeline, "timeline", fps


def _resolve_marker_frame(target: Any, target_kind: str, fps: Any, params: dict[str, Any]) -> int:
    """Convert `frame` or `tc` into Resolve's marker `frameId` convention.

    Important Resolve quirk: `Timeline.AddMarker(frameId, ...)` uses frame offset relative to
    `Timeline.GetStartFrame()` (e.g., frameId=0 is `01:00:00:00`), NOT absolute frame 86400!
    """
    if params.get("frame_offset") is not None:
        return int(params["frame_offset"])
    if params.get("frame") is not None:
        raw_f = int(params["frame"])
        if target_kind == "timeline":
            start_f = int(safe_call(target, "GetStartFrame", default=0))
            return raw_f - start_f if raw_f >= start_f else raw_f
        return raw_f
    if params.get("tc") is not None:
        abs_f = tc_to_frames(str(params["tc"]), fps)
        if target_kind == "timeline":
            start_f = int(safe_call(target, "GetStartFrame", default=0))
            return max(0, abs_f - start_f)
        return abs_f
    # Default to current playhead on timeline!
    if target_kind == "timeline":
        cur_tc = str(safe_call(target, "GetCurrentTimecode", default="01:00:00:00"))
        start_f = int(safe_call(target, "GetStartFrame", default=0))
        return max(0, tc_to_frames(cur_tc, fps) - start_f)
    raise OpError("Marker op requires 'frame', 'frame_offset', or 'tc'", code="MISSING_PARAM")


@resolve_op(
    "marker.list",
    tier="read",
    summary="List markers on the active timeline, a timeline item, or a Media Pool clip.",
    params_doc={
        "item_id": "Optional timeline item_id",
        "clip_id": "Optional Media Pool clip_id",
        "color": "Optional color filter (e.g. 'Blue')",
    },
)
def op_marker_list(ctx: OpContext, params: dict[str, Any]) -> dict[str, Any]:
    target, kind, fps = _resolve_marker_target(ctx, params)
    raw_markers = safe_call(target, "GetMarkers", default={})
    start_f = int(safe_call(target, "GetStartFrame", default=0)) if kind == "timeline" else 0
    color_filter = str(params["color"]).strip().lower() if params.get("color") else None

    out: list[dict[str, Any]] = []
    if isinstance(raw_markers, dict):
        for f_key in sorted(raw_markers.keys(), key=lambda x: int(x)):
            m = raw_markers[f_key]
            if not isinstance(m, dict):
                continue
            color = str(m.get("color", ""))
            if color_filter and color.lower() != color_filter:
                continue
            offset = int(f_key)
            abs_frame = start_f + offset
            out.append(
                {
                    "frame_offset": offset,
                    "frame": abs_frame,
                    "tc": frames_to_tc(abs_frame, fps),
                    "color": color,
                    "name": str(m.get("name", "")),
                    "note": str(m.get("note", "")),
                    "duration": int(m.get("duration", 1)),
                    "custom_data": str(m.get("customData", "")),
                }
            )
    return {"target": kind, "count": len(out), "markers": out}


@resolve_op(
    "marker.add",
    tier="mutate",
    summary="Add a marker to the active timeline (defaults to playhead), timeline item, or clip.",
    params_doc={
        "tc": "Optional SMPTE timecode (defaults to current playhead on timeline)",
        "frame": "Optional frame number",
        "color": "Marker color (default 'Blue')",
        "name": "Short marker title",
        "note": "Detailed marker note",
        "duration": "Duration in frames (default 1)",
        "custom_data": "Optional custom string tag (default 'davag')",
        "item_id": "Optional timeline item_id",
        "clip_id": "Optional Media Pool clip_id",
    },
)
def op_marker_add(ctx: OpContext, params: dict[str, Any]) -> dict[str, Any]:
    target, kind, fps = _resolve_marker_target(ctx, params)
    offset = _resolve_marker_frame(target, kind, fps, params)
    color = str(params.get("color", "Blue"))
    name = str(params.get("name", "Note"))
    note = str(params.get("note", ""))
    duration = int(params.get("duration", 1))
    custom_data = str(params.get("custom_data", "davag"))

    ok = bool(target.AddMarker(offset, color, name, note, duration, custom_data))
    if not ok:
        raise OpError(
            f"AddMarker failed at offset {offset} (a marker may already exist at that exact frame)",
            code="ADD_MARKER_FAILED",
        )
    start_f = int(safe_call(target, "GetStartFrame", default=0)) if kind == "timeline" else 0
    return {
        "target": kind,
        "frame_offset": offset,
        "frame": start_f + offset,
        "tc": frames_to_tc(start_f + offset, fps),
        "color": color,
        "name": name,
    }


@resolve_op(
    "marker.delete",
    tier="mutate",
    summary="Delete a marker at a specific frame/timecode, or delete all markers of a color.",
    params_doc={
        "tc": "Optional timecode of marker to delete",
        "frame": "Optional frame of marker to delete",
        "color": "Optional color to bulk-delete (or 'All' on timeline)",
        "item_id": "Optional timeline item_id",
        "clip_id": "Optional Media Pool clip_id",
    },
)
def op_marker_delete(ctx: OpContext, params: dict[str, Any]) -> dict[str, Any]:
    target, kind, fps = _resolve_marker_target(ctx, params)
    if params.get("color"):
        color = str(params["color"])
        fn = getattr(target, "DeleteMarkersByColor", None)
        if not callable(fn):
            raise OpError(f"{kind} does not support DeleteMarkersByColor", code="UNSUPPORTED")
        ok = bool(fn(color))
        return {"deleted": ok, "by_color": color}

    offset = _resolve_marker_frame(target, kind, fps, params)
    ok = bool(target.DeleteMarkerAtFrame(offset))
    return {"deleted": ok, "frame_offset": offset}