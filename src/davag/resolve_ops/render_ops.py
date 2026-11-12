"""Deliver page render presets, render job queueing, and timeline range exports (stdlib only)."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from .helpers import lua_dict, lua_list, safe_call
from .schema import OpContext, OpError, resolve_op


@resolve_op(
    "render.presets",
    tier="read",
    summary="List available Deliver render presets in the current project.",
)
def op_render_presets(ctx: OpContext, _params: dict[str, Any]) -> dict[str, Any]:
    proj = ctx.project
    presets = [str(x) for x in lua_list(safe_call(proj, "GetRenderPresetList", default=[]))]
    return {"presets": presets}


@resolve_op(
    "render.queue_add",
    tier="mutate",
    summary="Configure Deliver render settings and add a render job to Resolve's queue.",
    params_doc={
        "target_dir": "Output folder path on disk",
        "custom_name": "Output filename (without extension)",
        "preset_name": "Optional Deliver preset name to load first",
        "mark_in": "Optional start frame",
        "mark_out": "Optional end frame",
        "width": "Optional output width (int)",
        "height": "Optional output height (int)",
        "select_all_frames": "Optional bool",
    },
)
def op_render_queue_add(ctx: OpContext, params: dict[str, Any]) -> dict[str, Any]:
    target_dir = str(params.get("target_dir", "")).strip()
    custom_name = str(params.get("custom_name", "")).strip()
    if not target_dir or not custom_name:
        raise OpError("render.queue_add requires 'target_dir' and 'custom_name'", code="MISSING_PARAM")

    proj = ctx.project
    Path(target_dir).mkdir(parents=True, exist_ok=True)

    preset = str(params.get("preset_name", "")).strip()
    if preset and not proj.LoadRenderPreset(preset):
        raise OpError(f'Render preset "{preset}" not found', code="PRESET_NOT_FOUND")

    settings: dict[str, Any] = {
        "TargetDir": str(Path(target_dir).resolve()),
        "CustomName": custom_name,
    }
    if params.get("mark_in") is not None:
        settings["MarkIn"] = int(params["mark_in"])
    if params.get("mark_out") is not None:
        settings["MarkOut"] = int(params["mark_out"])
    if params.get("width") is not None:
        settings["FormatWidth"] = int(params["width"])
    if params.get("height") is not None:
        settings["FormatHeight"] = int(params["height"])
    if params.get("select_all_frames") is not None:
        settings["SelectAllFrames"] = bool(params["select_all_frames"])

    if not proj.SetRenderSettings(settings):
        raise OpError("Resolve rejected SetRenderSettings", code="RENDER_SETTINGS_FAILED")

    job_id = proj.AddRenderJob()
    if not job_id:
        raise OpError("AddRenderJob failed", code="ADD_RENDER_JOB_FAILED")

    return {"job_id": str(job_id), "settings": settings}


@resolve_op(
    "render.start",
    tier="mutate",
    summary="Start rendering queued Deliver jobs (optionally waiting for completion).",
    params_doc={
        "job_ids": "Optional list of job_ids (renders all queued jobs if omitted)",
        "wait": "Optional bool (default false)",
        "timeout_sec": "Max seconds to wait if wait=true (default 120)",
    },
)
def op_render_start(ctx: OpContext, params: dict[str, Any]) -> dict[str, Any]:
    proj = ctx.project
    job_ids = params.get("job_ids")
    if isinstance(job_ids, list) and job_ids:
        ok = bool(proj.StartRendering([str(j) for j in job_ids]))
    else:
        ok = bool(proj.StartRendering())
    if not ok:
        raise OpError("StartRendering returned False", code="START_RENDER_FAILED")

    if bool(params.get("wait", False)):
        deadline = time.time() + float(params.get("timeout_sec", 120.0))
        while time.time() < deadline:
            if not bool(safe_call(proj, "IsRenderingInProgress", default=False)):
                break
            time.sleep(0.25)

    jobs = [lua_dict(j) for j in lua_list(safe_call(proj, "GetRenderJobList", default=[]))]
    return {"started": True, "jobs": jobs}


@resolve_op(
    "render.status",
    tier="read",
    summary="Inspect Deliver render queue jobs and completion percentages.",
)
def op_render_status(ctx: OpContext, _params: dict[str, Any]) -> dict[str, Any]:
    proj = ctx.project
    in_progress = bool(safe_call(proj, "IsRenderingInProgress", default=False))
    jobs = [lua_dict(j) for j in lua_list(safe_call(proj, "GetRenderJobList", default=[]))]
    for j in jobs:
        jid = j.get("JobId")
        if jid:
            j["status"] = lua_dict(safe_call(proj, "GetRenderJobStatus", jid, default={}))
    return {"rendering_in_progress": in_progress, "jobs": jobs}