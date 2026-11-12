"""Viewer frame grab, Color LUT/CDL grading, and Fusion comp/Text+ inspection & editing (stdlib only)."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

from .helpers import lua_list, safe_call
from .schema import OpContext, OpError, resolve_op
from .timeline_ops import find_timeline_items


@resolve_op(
    "timeline.grab_still",
    tier="read",
    summary="Export the current viewer frame as a PNG image (for visual inspection by vision LLMs).",
    params_doc={
        "tc": "Optional SMPTE timecode to move playhead to before grabbing",
        "output_path": "Optional destination .png path (defaults to temp file)",
        "attach_to_context": "Optional bool (default true) — harness attaches image to LLM context",
    },
)
def op_timeline_grab_still(ctx: OpContext, params: dict[str, Any]) -> dict[str, Any]:
    proj = ctx.project
    tl = ctx.timeline

    if params.get("tc") and not tl.SetCurrentTimecode(str(params["tc"]).strip()):
        raise OpError(f"Could not move playhead to {params['tc']}", code="PLAYHEAD_FAILED")

    out_raw = params.get("output_path")
    if out_raw:
        out_path = Path(str(out_raw)).resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
    else:
        fd, tmp_name = tempfile.mkstemp(prefix="davag_still_", suffix=".png")
        os.close(fd)
        out_path = Path(tmp_name)

    ok = bool(proj.ExportCurrentFrameAsStill(str(out_path)))
    if not ok or not out_path.is_file() or out_path.stat().st_size == 0:
        raise OpError(
            "ExportCurrentFrameAsStill failed (ensure timeline has media under the playhead)",
            code="GRAB_STILL_FAILED",
        )

    return {
        "path": str(out_path),
        "bytes": out_path.stat().st_size,
        "playhead_tc": safe_call(tl, "GetCurrentTimecode", default=None),
        "attach_to_context": bool(params.get("attach_to_context", True)),
    }


@resolve_op(
    "color.apply_lut",
    tier="mutate",
    checkpoint_timeline=True,
    summary="Apply a .cube LUT to a 1-based color node on one or more timeline items.",
    params_doc={
        "item_ids": "List of timeline item_ids",
        "lut_path": "Path to .cube LUT file (or relative LUT name after RefreshLUTList)",
        "node_index": "1-based Color node index (default 1)",
    },
)
def op_color_apply_lut(ctx: OpContext, params: dict[str, Any]) -> dict[str, Any]:
    item_ids = params.get("item_ids")
    lut_path = str(params.get("lut_path", "")).strip()
    node_idx = int(params.get("node_index", 1))
    if not isinstance(item_ids, list) or not item_ids or not lut_path:
        raise OpError("color.apply_lut requires 'item_ids' and 'lut_path'", code="MISSING_PARAM")
    if node_idx < 1:
        raise OpError("Resolve Color node_index is 1-based (>= 1)", code="INVALID_PARAM")

    safe_call(ctx.project, "RefreshLUTList")
    matches = find_timeline_items(ctx.timeline, [str(x) for x in item_ids])
    for _tt, _ti, item in matches:
        if not item.SetLUT(node_idx, lut_path):
            raise OpError(
                f'SetLUT({node_idx}, "{lut_path}") failed on {safe_call(item, "GetName")}',
                code="SET_LUT_FAILED",
            )
    return {"updated_count": len(matches), "node_index": node_idx, "lut_path": lut_path}


@resolve_op(
    "color.set_cdl",
    tier="mutate",
    checkpoint_timeline=True,
    summary="Apply ASC-CDL primary color values (Slope, Offset, Power, Saturation) to timeline items.",
    params_doc={
        "item_ids": "List of timeline item_ids",
        "node_index": "1-based Color node index (default 1)",
        "slope": "'R G B' string, e.g. '1.0 1.0 1.0'",
        "offset": "'R G B' string, e.g. '0.0 0.0 0.0'",
        "power": "'R G B' string, e.g. '1.0 1.0 1.0'",
        "saturation": "Float or string, e.g. '1.0'",
    },
)
def op_color_set_cdl(ctx: OpContext, params: dict[str, Any]) -> dict[str, Any]:
    item_ids = params.get("item_ids")
    if not isinstance(item_ids, list) or not item_ids:
        raise OpError("color.set_cdl requires non-empty list 'item_ids'", code="MISSING_PARAM")

    cdl_map = {
        "NodeIndex": str(int(params.get("node_index", 1))),
        "Slope": str(params.get("slope", "1.0 1.0 1.0")),
        "Offset": str(params.get("offset", "0.0 0.0 0.0")),
        "Power": str(params.get("power", "1.0 1.0 1.0")),
        "Saturation": str(params.get("saturation", "1.0")),
    }
    matches = find_timeline_items(ctx.timeline, [str(x) for x in item_ids])
    for _tt, _ti, item in matches:
        if not item.SetCDL(cdl_map):
            raise OpError(f"SetCDL failed on {safe_call(item, 'GetName')}", code="SET_CDL_FAILED")
    return {"updated_count": len(matches), "cdl": cdl_map}


@resolve_op(
    "fusion.inspect_comp",
    tier="read",
    summary="Inspect Fusion tools/nodes inside a TimelineItem (including Text+ StyledText).",
    params_doc={
        "item_id": "Timeline item_id",
        "comp_index": "1-based Fusion composition index (default 1)",
    },
)
def op_fusion_inspect_comp(ctx: OpContext, params: dict[str, Any]) -> dict[str, Any]:
    item_id = str(params.get("item_id", "")).strip()
    if not item_id:
        raise OpError("fusion.inspect_comp requires 'item_id'", code="MISSING_PARAM")

    comp_idx = int(params.get("comp_index", 1))
    item = find_timeline_items(ctx.timeline, [item_id])[0][2]
    comp_count = int(safe_call(item, "GetFusionCompCount", default=0))
    if comp_count == 0:
        return {"item_id": item_id, "comp_count": 0, "tools": []}

    comp = item.GetFusionCompByIndex(comp_idx)
    if comp is None:
        raise OpError(f"Fusion comp index {comp_idx} not found (count={comp_count})", code="COMP_NOT_FOUND")

    tools_out: list[dict[str, Any]] = []
    for tool in lua_list(safe_call(comp, "GetToolList", False, default=[])):
        attrs = safe_call(tool, "GetAttrs", default={}) or {}
        t_name = str(getattr(tool, "Name", None) or attrs.get("TOOLS_Name", ""))
        t_id = str(getattr(tool, "ID", None) or attrs.get("TOOLS_RegID", ""))
        entry: dict[str, Any] = {"name": t_name, "type": t_id}
        if t_id in ("TextPlus", "Text3D"):
            entry["styled_text"] = safe_call(tool, "GetInput", "StyledText", default=None)
            entry["size"] = safe_call(tool, "GetInput", "Size", default=None)
        tools_out.append(entry)

    return {"item_id": item_id, "comp_count": comp_count, "tools": tools_out}


@resolve_op(
    "fusion.set_tool_input",
    tier="mutate",
    checkpoint_timeline=True,
    summary="Set input parameters on a Fusion node (e.g., setting StyledText on a Text+ title).",
    params_doc={
        "item_id": "Timeline item_id",
        "tool_name": "Optional Fusion tool name (defaults to first TextPlus tool if omitted)",
        "inputs": "Dict of input_name -> value, e.g. {'StyledText': 'Chapter 1', 'Size': 0.09}",
        "comp_index": "1-based Fusion composition index (default 1)",
    },
)
def op_fusion_set_tool_input(ctx: OpContext, params: dict[str, Any]) -> dict[str, Any]:
    item_id = str(params.get("item_id", "")).strip()
    inputs = params.get("inputs")
    if not item_id or not isinstance(inputs, dict) or not inputs:
        raise OpError("fusion.set_tool_input requires 'item_id' and non-empty 'inputs'", code="MISSING_PARAM")

    comp_idx = int(params.get("comp_index", 1))
    item = find_timeline_items(ctx.timeline, [item_id])[0][2]
    comp = safe_call(item, "GetFusionCompByIndex", comp_idx)
    if comp is None:
        raise OpError(f"Item {item_id} has no Fusion comp at index {comp_idx}", code="COMP_NOT_FOUND")

    tool_name = str(params.get("tool_name", "")).strip()
    target_tool = None
    if tool_name:
        target_tool = safe_call(comp, "FindTool", tool_name)
    if target_tool is None:
        for tool in lua_list(safe_call(comp, "GetToolList", False, default=[])):
            t_name = str(getattr(tool, "Name", ""))
            t_id = str(getattr(tool, "ID", ""))
            if (tool_name and t_name == tool_name) or (not tool_name and t_id == "TextPlus"):
                target_tool = tool
                break

    if target_tool is None:
        raise OpError(
            f'Fusion tool "{tool_name or "TextPlus"}" not found in item {item_id}',
            code="TOOL_NOT_FOUND",
        )

    for k, v in inputs.items():
        target_tool.SetInput(str(k), v)

    return {
        "item_id": item_id,
        "tool_name": str(getattr(target_tool, "Name", tool_name)),
        "updated_inputs": list(inputs.keys()),
    }