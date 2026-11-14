"""Save, Stage, and Step-Boundary Apply for live prompt editing (`davai/src/session/promptedit.js`)."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from davag.agent.grounding import build_grounding_text
from davag.agent.prompt import (
    PROMPT_SPECS,
    PromptSpec,
    build_system_prompt,
    check_override,
    prompt_dirs,
    resolve_prompts_layered,
)
from davag.agent.promptfork import snapshot_prompt_file, stamp_fork


def _find_spec(key: str) -> PromptSpec:
    spec = next((s for s in PROMPT_SPECS if s.key == key), None)
    if spec is None:
        raise ValueError(f'Unknown prompt "{key}" (expected one of: {", ".join(s.key for s in PROMPT_SPECS)})')
    return spec


def prompt_editor_state(session: Any) -> dict[str, Any]:
    cfg = session.cfg
    dirs = prompt_dirs(cfg.home, cfg.cwd)
    err_msg: str | None = None
    try:
        disk_vals, disk_sources, disk_layers = resolve_prompts_layered(home=cfg.home, cwd=cfg.cwd)
    except ValueError as err:
        disk_vals, disk_sources, disk_layers = dict(session.prompts), dict(session.prompt_sources), {}
        err_msg = str(err)

    rows = []
    for spec in PROMPT_SPECS:
        k = spec.key
        live_txt = session.prompts.get(k, "")
        disk_txt = disk_vals.get(k, live_txt)
        rows.append(
            {
                "key": k,
                "filename": spec.filename,
                "placeholder": spec.required_placeholder,
                "live": live_txt,
                "disk": disk_txt,
                "dirty": disk_txt != live_txt,
                "layer": disk_layers.get(k, session.prompt_layers.get(k, "default")),
                "source": disk_sources.get(k, session.prompt_sources.get(k, "default")),
            }
        )

    return {
        "dirs": {k: str(v) for k, v in dirs.items()},
        "pending_apply": bool(getattr(session, "pending_prompts", None)),
        "system_tokens": session.ledger.system_tokens,
        "system": session.ledger.system,
        "prompts": rows,
        "error": err_msg,
    }


def save_prompt_override(
    session: Any, *, key: str, text: str, layer: str = "project"
) -> dict[str, Any]:
    spec = _find_spec(key)
    dirs = prompt_dirs(session.cfg.home, session.cfg.cwd)
    target_dir = dirs["home"] if layer == "home" else dirs["project"]
    file_path = target_dir / spec.filename

    check_override(spec, text, str(file_path))
    target_dir.mkdir(parents=True, exist_ok=True)
    snap = snapshot_prompt_file(file_path)
    file_path.write_text(text, encoding="utf-8")
    stamp_fork(target_dir, [key])
    return {"file": str(file_path), "snapshot": snap, "layer": layer}


def revert_prompt_override(
    session: Any, *, key: str, layer: str = "project"
) -> dict[str, Any]:
    spec = _find_spec(key)
    dirs = prompt_dirs(session.cfg.home, session.cfg.cwd)
    target_dir = dirs["home"] if layer == "home" else dirs["project"]
    file_path = target_dir / spec.filename
    if not file_path.is_file():
        return {"file": str(file_path), "removed": False, "snapshot": None}
    snap = snapshot_prompt_file(file_path)
    file_path.unlink()
    return {"file": str(file_path), "removed": True, "snapshot": snap}


def stage_prompt_apply(session: Any) -> dict[str, Any]:
    # Validate immediately so UI catches missing placeholders at button click!
    resolve_prompts_layered(home=session.cfg.home, cwd=session.cfg.cwd)
    session.pending_prompts = {"staged_at": time.time()}
    if not session.agent.busy:
        return apply_staged_prompts(session)
    return {"staged": True, "applied": False}


def apply_staged_prompts(session: Any) -> dict[str, Any]:
    vals, sources, layers = resolve_prompts_layered(home=session.cfg.home, cwd=session.cfg.cwd)
    before = session.ledger.system_tokens
    session.prompts = vals
    session.prompt_sources = sources
    session.prompt_layers = layers
    session.agent.prompts = vals

    g_text, _, _ = build_grounding_text(
        cfg=session.cfg,
        sandbox=session.sandbox,
        resolve_section=getattr(session, "resolve_grounding_snapshot", ""),
    )
    session.ledger.set_system(build_system_prompt(prompts=vals, grounding=g_text, role=session.cfg.role))
    session.pending_prompts = None
    res = {"applied": True, "staged": False, "before_tokens": before, "after_tokens": session.ledger.system_tokens}
    session.bus.publish("prompts-applied", res)
    return res


def export_prompts_to_project(session: Any, *, force: bool = False) -> dict[str, Any]:
    dirs = prompt_dirs(session.cfg.home, session.cfg.cwd)
    proj_dir: Path = dirs["project"]
    targets = [(spec, session.sandbox.resolve_for_write(proj_dir / spec.filename)) for spec in PROMPT_SPECS]
    existing = [session.sandbox.rel(p) for _, p in targets if p.exists()]
    if existing and not force:
        raise FileExistsError(
            f"Not exported: {', '.join(existing)} already exist — run `/prompts export --force` to overwrite."
        )
    proj_dir.mkdir(parents=True, exist_ok=True)
    for spec, abs_p in targets:
        if force:
            snapshot_prompt_file(abs_p)
        abs_p.write_text(session.prompts[spec.key], encoding="utf-8")
    stamp_fork(proj_dir, [s.key for s, _ in targets])
    return {"dir": session.sandbox.rel(proj_dir), "count": len(targets)}