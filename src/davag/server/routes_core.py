"""Core FastAPI routes: session info, SSE event stream, submit/cancel/approval, context, prompts, models, keys & docs."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from davag.context.compact import compact_ledger
from davag.context.utf8 import sanitize_utf8, sanitize_utf8_str
from davag.session.keys_ctl import clear_provider_key, key_state, set_provider_key
from davag.session.models_ctl import apply_model_change, provider_catalog_state
from davag.session.profiles_ctl import choose_profile_var, profile_state, switch_profile
from davag.ui.commands import command_list

from .state import StateDep

router = APIRouter()


@router.get("/api/session")
async def api_session(st: StateDep) -> dict[str, Any]:
    return st.describe_session()


@router.get("/api/commands")
async def api_commands() -> dict[str, Any]:
    return {"commands": command_list()}


@router.get("/api/history")
async def api_history(st: StateDep) -> dict[str, Any]:
    return {"prompts": st.session.history.strings()}


@router.get("/api/perf")
async def api_perf(st: StateDep) -> dict[str, Any]:
    return st.session.perf.to_dict()


@router.get("/api/events")
async def api_events(req: Request, st: StateDep) -> StreamingResponse:
    last_raw = req.headers.get("Last-Event-ID") or req.query_params.get("since") or "0"
    last_seq = int(last_raw) if str(last_raw).isdigit() else 0
    missed, gap = st.session.bus.since(last_seq)
    hello = {**st.describe_session(), **st.session.bus.snapshot(), "gap": gap}

    q: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
    st.sse_queues.add(q)
    unsub = st.session.bus.subscribe(lambda ev: q.put_nowait(ev))

    def _fmt(ev_type: str, data: Any, seq_id: int | None = None) -> bytes:
        parts = [f"id: {seq_id}\n"] if seq_id is not None else []
        parts.append(f"event: {ev_type}\n")
        safe_json = sanitize_utf8_str(json.dumps(sanitize_utf8(data), ensure_ascii=False))
        parts.append(f"data: {safe_json}\n\n")
        return "".join(parts).encode("utf-8", "replace")

    async def _gen() -> AsyncIterator[bytes]:
        try:
            init_bytes = [_fmt("hello", hello), *(_fmt(ev["type"], ev["data"], ev["seq"]) for ev in missed)]
            yield b"".join(init_bytes)
            while not st.shutdown_ev.is_set():
                if await req.is_disconnected():
                    break
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=15.0)
                    if ev is None:
                        break
                    yield _fmt(ev["type"], ev["data"], ev["seq"])
                except TimeoutError:
                    yield b": ping\n\n"
        except ( asyncio.CancelledError, ConnectionResetError, OSError):
            pass
        finally:
            st.sse_queues.discard(q)
            unsub()

    return StreamingResponse(
        _gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/api/submit")
async def api_submit(req: Request, st: StateDep) -> JSONResponse:
    body = await req.json()
    text = str(body.get("text", "")).strip()
    if not text:
        return JSONResponse({"error": "empty text"}, status_code=400)

    async def _safe_web_submit() -> None:
        try:
            await st.submit_fn(text, "web")
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001
            st.session.bus.publish("error", {"message": f"{type(err).__name__}: {err}"})

    task = asyncio.create_task(_safe_web_submit())
    st.bg_tasks.add(task)
    task.add_done_callback(st.bg_tasks.discard)
    return JSONResponse({"ok": True})


@router.post("/api/cancel")
async def api_cancel(st: StateDep) -> dict[str, Any]:
    st.session.agent.cancel()
    return {"ok": True}


@router.post("/api/approve")
async def api_approve(req: Request, st: StateDep) -> dict[str, Any]:
    body = await req.json()
    ap_id = str(body.get("id", ""))
    decision = {
        "allow": bool(body.get("allow", False)),
        "reason": body.get("reason"),
        "checkpoint_first": bool(body.get("checkpoint_first", False)),
    }
    settled = st.session.bus.resolve_approval(ap_id, decision)
    return {"ok": True, "settled": settled, "stale": not settled}


@router.get("/api/context")
async def api_context_get(st: StateDep) -> dict[str, Any]:
    led = st.session.ledger
    return {
        "system_tokens": led.system_tokens,
        "total_tokens": led.total_tokens,
        "limit": led.limit,
        "fraction": round(led.usage_fraction, 4),
        "segments": [s.to_summary_dict() for s in led.segments],
    }


@router.post("/api/context/drop")
async def api_context_drop(req: Request, st: StateDep) -> dict[str, Any]:
    body = await req.json()
    return {"ok": st.session.ledger.drop(int(body["id"]))}


@router.post("/api/context/pin")
async def api_context_pin(req: Request, st: StateDep) -> dict[str, Any]:
    body = await req.json()
    pinned = st.session.ledger.toggle_pin(int(body["id"]), body.get("pinned"))
    return {"ok": True, "pinned": pinned}


@router.post("/api/compact")
async def api_compact(st: StateDep) -> dict[str, Any]:
    return await compact_ledger(st.session.ledger)


@router.get("/api/prompts")
async def api_prompts_get(st: StateDep) -> dict[str, Any]:
    from davag.session.promptedit import prompt_editor_state

    return prompt_editor_state(st.session)


@router.post("/api/prompts/save")
async def api_prompts_save(req: Request, st: StateDep) -> JSONResponse:
    from davag.session.promptedit import save_prompt_override

    body = await req.json()
    try:
        res = save_prompt_override(
            st.session,
            key=str(body["key"]),
            text=str(body["text"]),
            layer=str(body.get("layer", "project")),
        )
        return JSONResponse({"ok": True, **res})
    except Exception as err:  # noqa: BLE001
        return JSONResponse({"error": str(err)}, status_code=400)


@router.post("/api/prompts/revert")
async def api_prompts_revert(req: Request, st: StateDep) -> JSONResponse:
    from davag.session.promptedit import revert_prompt_override

    body = await req.json()
    try:
        res = revert_prompt_override(
            st.session,
            key=str(body["key"]),
            layer=str(body.get("layer", "project")),
        )
        return JSONResponse({"ok": True, **res})
    except Exception as err:  # noqa: BLE001
        return JSONResponse({"error": str(err)}, status_code=400)


@router.post("/api/prompts/apply")
async def api_prompts_apply(st: StateDep) -> JSONResponse:
    from davag.session.promptedit import stage_prompt_apply

    try:
        res = stage_prompt_apply(st.session)
        return JSONResponse({"ok": True, **res})
    except Exception as err:  # noqa: BLE001
        return JSONResponse({"error": str(err)}, status_code=400)


@router.get("/api/models")
async def api_models_get(st: StateDep) -> dict[str, Any]:
    return provider_catalog_state(st.session)


@router.post("/api/model")
async def api_model_post(req: Request, st: StateDep) -> JSONResponse:
    body = await req.json()
    try:
        res = apply_model_change(
            st.session,
            provider=body.get("provider"),
            model_id=body.get("model"),
            effort=body.get("effort"),
            persist=bool(body.get("persist", False)),
        )
        return JSONResponse({"ok": True, **res})
    except ValueError as err:
        return JSONResponse({"error": str(err)}, status_code=400)


@router.get("/api/keys")
async def api_keys_get(st: StateDep) -> dict[str, Any]:
    return {"providers": key_state(st.session)}


@router.post("/api/keys")
async def api_keys_post(req: Request, st: StateDep) -> JSONResponse:
    body = await req.json()
    prov = str(body.get("provider", ""))
    if body.get("clear"):
        res = clear_provider_key(st.session, prov, save=bool(body.get("save", False)))
        return JSONResponse({"ok": True, **res})
    try:
        res = set_provider_key(st.session, prov, str(body.get("key", "")), save=bool(body.get("save", False)))
        return JSONResponse({"ok": True, **res})
    except ValueError as err:
        return JSONResponse({"error": str(err)}, status_code=400)


@router.get("/api/env")
async def api_env_get(st: StateDep) -> dict[str, Any]:
    return profile_state(st.session)


@router.post("/api/env")
async def api_env_post(req: Request, st: StateDep) -> JSONResponse:
    body = await req.json()
    try:
        if body.get("var_name"):
            res = choose_profile_var(
                st.session,
                str(body["block"]),
                str(body["var_name"]),
                line=body.get("line"),
                value=body.get("value"),
            )
        else:
            blk = body.get("block")
            res = switch_profile(st.session, None if blk in (None, "", "off") else str(blk))
        return JSONResponse({"ok": True, **res})
    except Exception as err:  # noqa: BLE001
        return JSONResponse({"error": str(err)}, status_code=400)


@router.post("/api/role")
async def api_role_post(req: Request, st: StateDep) -> JSONResponse:
    from davag.agent.roles import ROLE_SPECS, ROLES
    from davag.session.promptedit import apply_staged_prompts

    body = await req.json()
    target = str(body.get("role", "")).strip().lower()
    if target not in ROLES:
        return JSONResponse({"error": f"Unknown role '{target}'"}, status_code=400)
    st.session.cfg.role = target
    reb = apply_staged_prompts(st.session)
    st.session.bus.publish("config-changed", {"role": target})
    return JSONResponse({"ok": True, "role": target, "title": ROLE_SPECS[target].title, **reb})


@router.get("/api/artifacts")
async def api_artifacts_list(st: StateDep) -> dict[str, Any]:
    return {"artifacts": [a.to_meta_dict() for a in st.session.artifacts.items]}


@router.get("/api/artifact/{aid}")
async def api_artifact_get(aid: int, st: StateDep) -> JSONResponse:
    art = st.session.artifacts.get(aid)
    if art is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse(sanitize_utf8({**art.to_meta_dict(), "body": art.body}))


@router.get("/api/docs")
async def api_docs_get() -> dict[str, Any]:
    docs_dir = Path(__file__).resolve().parent.parent / "docs"
    repo_root = Path(__file__).resolve().parent.parent.parent.parent
    items: list[dict[str, str]] = []

    for top_name, label in (("README.md", "Overview (README)"), ("DAVAG.md", "Invariants & Architecture (DAVAG.md)")):
        cand = repo_root / top_name
        if cand.is_file():
            items.append({"id": top_name, "title": label, "markdown": cand.read_text(encoding="utf-8")})

    if docs_dir.is_dir():
        for md_file in sorted(docs_dir.glob("*.md")):
            body_md = md_file.read_text(encoding="utf-8")
            first_h1 = next(
                (ln.lstrip("# ").strip() for ln in body_md.splitlines() if ln.startswith("# ")),
                md_file.stem,
            )
            items.append({"id": md_file.name, "title": first_h1, "markdown": body_md})

    return {"docs": items}