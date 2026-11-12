"""Session, page navigation, and project-level Resolve operations (stdlib only)."""

from __future__ import annotations

import time
from typing import Any

from .helpers import lua_list, safe_call
from .schema import OpContext, OpError, list_ops, resolve_op

VALID_PAGES = ("media", "cut", "edit", "fusion", "color", "fairlight", "deliver")


@resolve_op(
    "system.ping",
    tier="read",
    summary="Health check for Resolve transport latency and registered ops.",
)
def op_system_ping(ctx: OpContext, _params: dict[str, Any]) -> dict[str, Any]:
    product = safe_call(ctx.resolve, "GetProductName", default="DaVinci Resolve")
    version = safe_call(ctx.resolve, "GetVersionString", default="unknown")
    return {
        "pong": True,
        "mode": ctx.mode,
        "product": str(product),
        "version": str(version),
        "ts": time.time(),
        "ops_count": len(list_ops()),
    }


@resolve_op(
    "resolve.status",
    tier="read",
    summary="Inspect Resolve product/edition, current UI page, open project, and active timeline.",
)
def op_resolve_status(ctx: OpContext, _params: dict[str, Any]) -> dict[str, Any]:
    product = str(safe_call(ctx.resolve, "GetProductName", default="DaVinci Resolve"))
    version_str = str(safe_call(ctx.resolve, "GetVersionString", default="unknown"))
    version_parts = lua_list(safe_call(ctx.resolve, "GetVersion", default=[]))
    page = safe_call(ctx.resolve, "GetCurrentPage", default=None)

    pm = safe_call(ctx.resolve, "GetProjectManager")
    proj = safe_call(pm, "GetCurrentProject") if pm else None
    tl = safe_call(proj, "GetCurrentTimeline") if proj else None

    project_info: dict[str, Any] | None = None
    if proj is not None:
        project_info = {
            "name": str(safe_call(proj, "GetName", default="")),
            "timeline_count": int(safe_call(proj, "GetTimelineCount", default=0)),
            "fps": safe_call(proj, "GetSetting", "timelineFrameRate", default=None),
            "width": safe_call(proj, "GetSetting", "timelineResolutionWidth", default=None),
            "height": safe_call(proj, "GetSetting", "timelineResolutionHeight", default=None),
        }

    timeline_info: dict[str, Any] | None = None
    if tl is not None:
        start_frame = int(safe_call(tl, "GetStartFrame", default=0))
        end_frame = int(safe_call(tl, "GetEndFrame", default=0))
        timeline_info = {
            "name": str(safe_call(tl, "GetName", default="")),
            "playhead_tc": safe_call(tl, "GetCurrentTimecode", default=None),
            "start_tc": safe_call(tl, "GetStartTimecode", default=None),
            "start_frame": start_frame,
            "end_frame": end_frame,
            "duration_frames": max(0, end_frame - start_frame),
            "video_tracks": int(safe_call(tl, "GetTrackCount", "video", default=0)),
            "audio_tracks": int(safe_call(tl, "GetTrackCount", "audio", default=0)),
            "subtitle_tracks": int(safe_call(tl, "GetTrackCount", "subtitle", default=0)),
        }

    return {
        "mode": ctx.mode,
        "product": product,
        "studio": "studio" in product.lower(),
        "version": version_str,
        "version_parts": version_parts,
        "page": page,
        "project": project_info,
        "timeline": timeline_info,
    }


@resolve_op(
    "resolve.open_page",
    tier="read",
    summary="Switch DaVinci Resolve's active UI page.",
    params_doc={"page": "One of: media, cut, edit, fusion, color, fairlight, deliver"},
)
def op_open_page(ctx: OpContext, params: dict[str, Any]) -> dict[str, Any]:
    page = str(params.get("page", "")).strip().lower()
    if page not in VALID_PAGES:
        raise OpError(
            f'Invalid page "{page}" (expected one of: {", ".join(VALID_PAGES)})',
            code="INVALID_PAGE",
        )
    ok = bool(ctx.resolve.OpenPage(page))
    if not ok:
        raise OpError(f'Resolve refused OpenPage("{page}")', code="OPEN_PAGE_FAILED")
    return {"page": page, "current_page": safe_call(ctx.resolve, "GetCurrentPage", default=page)}


@resolve_op(
    "project.list",
    tier="read",
    summary="List projects and folders in the current Resolve database folder.",
)
def op_project_list(ctx: OpContext, _params: dict[str, Any]) -> dict[str, Any]:
    pm = ctx.project_manager
    projects = [str(x) for x in lua_list(safe_call(pm, "GetProjectListInCurrentFolder", default=[]))]
    folders = [str(x) for x in lua_list(safe_call(pm, "GetFolderListInCurrentFolder", default=[]))]
    current_folder = safe_call(pm, "GetCurrentFolder", default="")
    current_proj = safe_call(pm, "GetCurrentProject")
    return {
        "database_folder": str(current_folder),
        "folders": folders,
        "projects": projects,
        "current_project": str(safe_call(current_proj, "GetName", default="")) if current_proj else None,
    }


@resolve_op(
    "project.save",
    tier="mutate",
    summary="Save the currently open DaVinci Resolve project.",
)
def op_project_save(ctx: OpContext, _params: dict[str, Any]) -> dict[str, Any]:
    pm = ctx.project_manager
    proj = ctx.project
    ok = bool(pm.SaveProject())
    return {"saved": ok, "project": str(safe_call(proj, "GetName", default=""))}


@resolve_op(
    "project.open",
    tier="mutate",
    summary="Open an existing Resolve project by name (or create it if create_if_missing=true).",
    params_doc={
        "name": "Project name in the current database folder",
        "create_if_missing": "Optional bool (default false)",
        "save_current": "Optional bool (default true) — saves active project before switching",
    },
)
def op_project_open(ctx: OpContext, params: dict[str, Any]) -> dict[str, Any]:
    name = str(params.get("name", "")).strip()
    if not name:
        raise OpError("project.open requires a non-empty 'name'", code="MISSING_PARAM")

    pm = ctx.project_manager
    if params.get("save_current", True) and safe_call(pm, "GetCurrentProject") is not None:
        safe_call(pm, "SaveProject")

    proj = pm.LoadProject(name)
    created = False
    if proj is None and bool(params.get("create_if_missing", False)):
        proj = pm.CreateProject(name)
        created = proj is not None

    if proj is None:
        raise OpError(f'Project "{name}" could not be opened', code="PROJECT_NOT_FOUND")

    return {
        "project": str(safe_call(proj, "GetName", default=name)),
        "created": created,
    }