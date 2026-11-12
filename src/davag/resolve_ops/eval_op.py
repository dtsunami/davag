"""Gated `resolve.eval` escape hatch for long-tail DaVinci Resolve / Fusion API calls (stdlib only)."""

from __future__ import annotations

import contextlib
import io
from typing import Any

from .helpers import safe_call
from .schema import OpContext, OpError, resolve_op


@resolve_op(
    "resolve.eval",
    tier="eval",
    checkpoint_timeline=True,
    summary="Execute custom Python code against live (resolve, fusion, bmd, project, mediapool, timeline).",
    params_doc={
        "code": "Python source code (assign to `result` or `print(...)` to return data)",
        "description": "Short human-readable explanation shown in the approval prompt",
    },
)
def op_resolve_eval(ctx: OpContext, params: dict[str, Any]) -> dict[str, Any]:
    code = str(params.get("code", ""))
    if not code.strip():
        raise OpError("resolve.eval requires non-empty 'code'", code="MISSING_PARAM")

    pm = safe_call(ctx.resolve, "GetProjectManager")
    proj = safe_call(pm, "GetCurrentProject") if pm else None
    mp = safe_call(proj, "GetMediaPool") if proj else None
    tl = safe_call(proj, "GetCurrentTimeline") if proj else None

    env_globals: dict[str, Any] = {
        "__builtins__": __builtins__,
        "resolve": ctx.resolve,
        "res": ctx.resolve,
        "fusion": ctx.fusion,
        "fu": ctx.fusion,
        "bmd": ctx.bmd,
        "project_manager": pm,
        "project": proj,
        "mediapool": mp,
        "timeline": tl,
        "result": None,
    }

    stdout_buf = io.StringIO()
    try:
        compiled = compile(code, "<resolve.eval>", "exec")
        with contextlib.redirect_stdout(stdout_buf):
            exec(compiled, env_globals, env_globals)  # noqa: S102 — explicitly gated operator eval op
    except Exception as err:
        raise OpError(
            f"{type(err).__name__}: {err}\nstdout:\n{stdout_buf.getvalue()}",
            code="EVAL_EXCEPTION",
        ) from err

    raw_result = env_globals.get("result")
    safe_result: Any
    if isinstance(raw_result, (str, int, float, bool, list, dict)) or raw_result is None:
        safe_result = raw_result
    else:
        safe_result = repr(raw_result)

    return {
        "stdout": stdout_buf.getvalue(),
        "result": safe_result,
    }