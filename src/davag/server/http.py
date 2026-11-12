"""Asyncio `aiohttp` HTTP + SSE Web Sidecar (`davag.server.http`).

Mirrors `davai/src/server/http.js`:
- Unauthenticated static shell (`/`, `/assets/*`, `/favicon.svg`) so browser shows token prompt cleanly.
- Bearer token required on every `/api/*` endpoint.
- SSE stream on `GET /api/events` with `Last-Event-ID` / `?since=` replay from `SessionBus`.
- REST endpoints for submit, cancel, raced approval, Resolve live inspector + frame grab,
  context management, live model/key/.env switching, and artifact download.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aiohttp import web

from davag import __version__
from davag.context.compact import compact_ledger
from davag.context.media import load_image_part
from davag.session.bootstrap import Session
from davag.session.keys_ctl import clear_provider_key, key_state, set_provider_key
from davag.session.models_ctl import apply_model_change, provider_catalog_state
from davag.session.profiles_ctl import choose_profile_var, profile_state, switch_profile
from davag.ui.commands import command_list

from .auth import assert_safe_bind, make_web_token, read_request_token, token_matches

WEB_STATIC_DIR = Path(__file__).resolve().parents[1] / "ui" / "web"


def _detect_lan_ipv4() -> str | None:
    import socket

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            ip = s.getsockname()[0]
            if ip and not ip.startswith("127."):
                return ip
    except OSError:
        return None
    return None


@dataclass
class RunningWebServer:
    host: str
    port: int
    token: str
    runner: web.AppRunner
    url: str
    auth_url: str
    lan_auth_url: str | None
    shutdown_event: asyncio.Event
    sse_queues: set[asyncio.Queue[dict[str, Any] | None]]

    async def close(self) -> None:
        self.shutdown_event.set()
        for q in list(self.sse_queues):
            q.put_nowait(None)
        await self.runner.cleanup()


def _describe_session(session: Session) -> dict[str, Any]:
    cfg = session.cfg
    return {
        "version": __version__,
        "session_id": session.log.id,
        "cwd": str(cfg.cwd),
        "provider": cfg.provider,
        "model": cfg.model.id,
        "model_label": cfg.model.label,
        "video_capable": cfg.model.video,
        "effort": cfg.effort,
        "yolo": cfg.yolo,
        "resolve_connected": session.transport is not None,
        "resolve_mode": session.transport.mode if session.transport else None,
        "stats": dict(session.agent.stats),
        "context": {
            "tokens": session.ledger.total_tokens,
            "limit": session.ledger.limit,
            "fraction": round(session.ledger.usage_fraction, 4),
        },
    }


async def start_web_server(
    *,
    session: Session,
    submit_fn: Callable[[str, str], Any],
    host: str = "127.0.0.1",
    port: int = 7879,
    token: str | None = None,
) -> RunningWebServer:
    assert_safe_bind(host=host, yolo=session.cfg.yolo)
    secret = token or session.cfg.web_token or make_web_token()
    bg_tasks: set[asyncio.Task[Any]] = set()
    sse_queues: set[asyncio.Queue[dict[str, Any] | None]] = set()
    shutdown_ev = asyncio.Event()
    grab_files: dict[str, Path] = {}  # id -> Path for safe viewer still serving

    @web.middleware
    async def auth_middleware(req: web.Request, handler: Any) -> web.StreamResponse:
        path = req.path
        is_open = (
            path == "/"
            or path.startswith("/assets/")
            or path in ("/favicon.svg", "/favicon.ico")
        )
        if is_open:
            return await handler(req)

        allow_q = path == "/api/events" or path.startswith("/api/media/")
        given = read_request_token(req, allow_query=allow_q)
        if not token_matches(secret, given):
            return web.json_response({"error": "bad or missing token"}, status=401)
        return await handler(req)

    app = web.Application(middlewares=[auth_middleware])

    async def serve_index(_req: web.Request) -> web.FileResponse:
        return web.FileResponse(WEB_STATIC_DIR / "index.html")

    async def serve_favicon(_req: web.Request) -> web.FileResponse:
        return web.FileResponse(WEB_STATIC_DIR / "favicon.svg")

    async def serve_asset(req: web.Request) -> web.StreamResponse:
        name = req.match_info["name"]
        target = (WEB_STATIC_DIR / name).resolve()
        if target.parent != WEB_STATIC_DIR or not target.is_file():
            return web.json_response({"error": "not found"}, status=404)
        return web.FileResponse(target)

    async def api_session(_req: web.Request) -> web.Response:
        return web.json_response(_describe_session(session))

    async def api_commands(_req: web.Request) -> web.Response:
        return web.json_response({"commands": command_list()})

    async def api_perf(_req: web.Request) -> web.Response:
        return web.json_response(session.perf.to_dict())

    async def api_events(req: web.Request) -> web.StreamResponse:
        resp = web.StreamResponse(
            status=200,
            reason="OK",
            headers={
                "Content-Type": "text/event-stream; charset=utf-8",
                "Cache-Control": "no-cache, no-transform",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )
        await resp.prepare(req)

        async def _send_sse(ev_type: str, data: Any, seq_id: int | None = None) -> None:
            chunks = []
            if seq_id is not None:
                chunks.append(f"id: {seq_id}\n")
            chunks.append(f"event: {ev_type}\n")
            chunks.append(f"data: {json.dumps(data, ensure_ascii=False)}\n\n")
            await resp.write("".join(chunks).encode("utf-8"))

        last_raw = req.headers.get("Last-Event-ID") or req.query.get("since") or "0"
        last_seq = int(last_raw) if str(last_raw).isdigit() else 0
        missed, gap = session.bus.since(last_seq)

        hello = {**_describe_session(session), **session.bus.snapshot(), "gap": gap}
        await _send_sse("hello", hello)
        for ev in missed:
            await _send_sse(ev["type"], ev["data"], ev["seq"])

        q: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        sse_queues.add(q)
        unsub = session.bus.subscribe(lambda ev: q.put_nowait(ev))

        try:
            while not shutdown_ev.is_set():
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=15.0)
                    if ev is None:
                        break
                    await _send_sse(ev["type"], ev["data"], ev["seq"])
                except TimeoutError:
                    await resp.write(b": ping\n\n")
        except (ConnectionResetError, asyncio.CancelledError, OSError):
            pass
        finally:
            sse_queues.discard(q)
            unsub()
        return resp

    async def api_submit(req: web.Request) -> web.Response:
        body = await req.json()
        text = str(body.get("text", "")).strip()
        if not text:
            return web.json_response({"error": "empty text"}, status=400)

        task = asyncio.create_task(submit_fn(text, "web"))
        bg_tasks.add(task)
        task.add_done_callback(bg_tasks.discard)
        return web.json_response({"ok": True})

    async def api_cancel(_req: web.Request) -> web.Response:
        session.agent.cancel()
        return web.json_response({"ok": True})

    async def api_approve(req: web.Request) -> web.Response:
        body = await req.json()
        ap_id = str(body.get("id", ""))
        decision = {
            "allow": bool(body.get("allow", False)),
            "reason": body.get("reason"),
            "checkpoint_first": bool(body.get("checkpoint_first", False)),
        }
        settled = session.bus.resolve_approval(ap_id, decision)
        return web.json_response({"ok": True, "settled": settled, "stale": not settled})

    def _register_media_file(p: Path) -> str:
        resolved = p.resolve()
        for existing_id, existing_p in grab_files.items():
            if existing_p == resolved:
                return f"/api/media/still/{existing_id}"
        mid = f"m{len(grab_files) + 1}"
        grab_files[mid] = resolved
        return f"/api/media/still/{mid}"

    def _on_bus_media(ev: dict[str, Any]) -> None:
        # Automatically expose contact sheets & proxy MP4s generated by TUI/Agent to the Web GUI drawer!
        if ev["type"] == "media-preview":
            d = ev["data"]
            p = Path(str(d["path"]))
            if p.is_file():
                url = _register_media_file(p)
                session.bus.publish("viewer-media", {"kind": d["kind"], "url": url, "title": d.get("title", p.name)})
        elif ev["type"] == "ops-result":
            for m in (ev["data"] or {}).get("attached_media") or []:
                p = Path(str(m.get("path", "")))
                if p.is_file():
                    kind = "video" if m["kind"] == "video" else "image"
                    url = _register_media_file(p)
                    session.bus.publish("viewer-media", {"kind": kind, "url": url, "title": p.name})

    session.bus.subscribe(_on_bus_media)

    # --- Resolve Live Inspector & Frame Grab ---
    async def api_resolve_state(_req: web.Request) -> web.Response:
        from davag.media_intel.digest import load_clip_digest

        if session.transport is None:
            return web.json_response({"connected": False, "note": session.resolve_connect_note})
        try:
            status = await session.transport.call("resolve.status", timeout=4.0)
            tree = None
            clips_list = []
            tl_state = None
            if status.get("project"):
                tree = await session.transport.call("mediapool.tree", timeout=4.0)
                c_res = await session.transport.call("mediapool.clips", {"recursive": True}, timeout=4.0)
                for c in c_res.get("clips") or []:
                    dig = load_clip_digest(session.cfg.home / "cache", c["file_path"]) if c.get("file_path") else None
                    proxy_url = None
                    if c.get("proxy_path") and Path(str(c["proxy_path"])).is_file():
                        proxy_url = _register_media_file(Path(str(c["proxy_path"])))
                    sheet_url = None
                    if dig and dig.get("contact_sheet_path") and Path(str(dig["contact_sheet_path"])).is_file():
                        sheet_url = _register_media_file(Path(str(dig["contact_sheet_path"])))
                    clips_list.append(
                        {
                            **c,
                            "digest": dig.get("summary") if dig else None,
                            "scout_model": dig.get("scout_model") if dig else None,
                            "proxy_url": proxy_url,
                            "sheet_url": sheet_url,
                        }
                    )
            if status.get("timeline"):
                tl_state = await session.transport.call("timeline.get_state", timeout=4.0)
            return web.json_response(
                {
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
            )
        except Exception as err:  # noqa: BLE001
            return web.json_response({"connected": False, "error": str(err)})

    async def api_resolve_reconnect(req: web.Request) -> web.Response:
        body = await req.json()
        mode = body.get("mode")
        try:
            st = await session.reconnect_resolve(mode)
            return web.json_response({"ok": True, "status": st})
        except Exception as err:  # noqa: BLE001
            return web.json_response({"ok": False, "error": str(err)}, status=400)

    async def api_resolve_grab(req: web.Request) -> web.Response:
        if session.transport is None:
            return web.json_response({"error": "DaVinci Resolve is not connected"}, status=400)
        body = await req.json()
        attach = bool(body.get("attach", True))
        params: dict[str, Any] = {}
        if body.get("tc"):
            params["tc"] = str(body["tc"])
        try:
            still = await session.transport.call("timeline.grab_still", params)
            p = Path(still["path"]).resolve()
            still_id = f"s{len(grab_files) + 1}"
            grab_files[still_id] = p
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
            return web.json_response({"ok": True, "url": url, "playhead_tc": still.get("playhead_tc")})
        except Exception as err:  # noqa: BLE001
            return web.json_response({"error": str(err)}, status=400)

    async def api_serve_still(req: web.Request) -> web.StreamResponse:
        sid = req.match_info["id"]
        p = grab_files.get(sid)
        if p is None or not p.is_file():
            return web.json_response({"error": "not found"}, status=404)
        return web.FileResponse(p)

    async def api_resolve_undo(_req: web.Request) -> web.Response:
        if session.transport is None:
            return web.json_response({"error": "Resolve not connected"}, status=400)
        try:
            res = await session.journal.undo_last_resolve(session.transport)
            session.bus.publish("transcript", {"type": "notice", "message": f"Restored timeline '{res['restored_timeline']}'", "origin": "web"})
            return web.json_response({"ok": True, **res})
        except Exception as err:  # noqa: BLE001
            return web.json_response({"error": str(err)}, status=400)

    # --- Context Endpoints ---
    async def api_context_get(_req: web.Request) -> web.Response:
        led = session.ledger
        return web.json_response(
            {
                "system_tokens": led.system_tokens,
                "total_tokens": led.total_tokens,
                "limit": led.limit,
                "fraction": round(led.usage_fraction, 4),
                "segments": [s.to_summary_dict() for s in led.segments],
            }
        )

    async def api_context_drop(req: web.Request) -> web.Response:
        body = await req.json()
        ok = session.ledger.drop(int(body["id"]))
        return web.json_response({"ok": ok})

    async def api_context_pin(req: web.Request) -> web.Response:
        body = await req.json()
        pinned = session.ledger.toggle_pin(int(body["id"]), body.get("pinned"))
        return web.json_response({"ok": True, "pinned": pinned})

    async def api_compact(_req: web.Request) -> web.Response:
        res = await compact_ledger(session.ledger)
        return web.json_response(res)

    # --- Prompts & System Prefix Editor (matches davai /api/prompts*) ---
    async def api_prompts_get(_req: web.Request) -> web.Response:
        from davag.session.promptedit import prompt_editor_state

        return web.json_response(prompt_editor_state(session))

    async def api_prompts_save(req: web.Request) -> web.Response:
        from davag.session.promptedit import save_prompt_override

        body = await req.json()
        try:
            res = save_prompt_override(
                session,
                key=str(body["key"]),
                text=str(body["text"]),
                layer=str(body.get("layer", "project")),
            )
            return web.json_response({"ok": True, **res})
        except Exception as err:  # noqa: BLE001
            return web.json_response({"error": str(err)}, status=400)

    async def api_prompts_revert(req: web.Request) -> web.Response:
        from davag.session.promptedit import revert_prompt_override

        body = await req.json()
        try:
            res = revert_prompt_override(
                session,
                key=str(body["key"]),
                layer=str(body.get("layer", "project")),
            )
            return web.json_response({"ok": True, **res})
        except Exception as err:  # noqa: BLE001
            return web.json_response({"error": str(err)}, status=400)

    async def api_prompts_apply(_req: web.Request) -> web.Response:
        from davag.session.promptedit import stage_prompt_apply

        try:
            res = stage_prompt_apply(session)
            return web.json_response({"ok": True, **res})
        except Exception as err:  # noqa: BLE001
            return web.json_response({"error": str(err)}, status=400)

    # --- Models, Keys & .env Profiles ---
    async def api_models_get(_req: web.Request) -> web.Response:
        return web.json_response(provider_catalog_state(session))

    async def api_model_post(req: web.Request) -> web.Response:
        body = await req.json()
        try:
            res = apply_model_change(
                session,
                provider=body.get("provider"),
                model_id=body.get("model"),
                effort=body.get("effort"),
                persist=bool(body.get("persist", False)),
            )
            return web.json_response({"ok": True, **res})
        except ValueError as err:
            return web.json_response({"error": str(err)}, status=400)

    async def api_keys_get(_req: web.Request) -> web.Response:
        return web.json_response({"providers": key_state(session)})

    async def api_keys_post(req: web.Request) -> web.Response:
        body = await req.json()
        prov = str(body.get("provider", ""))
        if body.get("clear"):
            res = clear_provider_key(session, prov, save=bool(body.get("save", False)))
            return web.json_response({"ok": True, **res})
        try:
            res = set_provider_key(
                session, prov, str(body.get("key", "")), save=bool(body.get("save", False))
            )
            return web.json_response({"ok": True, **res})
        except ValueError as err:
            return web.json_response({"error": str(err)}, status=400)

    async def api_env_get(_req: web.Request) -> web.Response:
        return web.json_response(profile_state(session))

    async def api_env_post(req: web.Request) -> web.Response:
        body = await req.json()
        try:
            if body.get("var_name"):
                st = choose_profile_var(
                    session,
                    str(body["block"]),
                    str(body["var_name"]),
                    line=body.get("line"),
                    value=body.get("value"),
                )
            else:
                blk = body.get("block")
                st = switch_profile(session, None if blk in (None, "", "off") else str(blk))
            return web.json_response({"ok": True, **st})
        except Exception as err:  # noqa: BLE001
            return web.json_response({"error": str(err)}, status=400)

    # --- Artifacts ---
    async def api_artifacts_list(_req: web.Request) -> web.Response:
        return web.json_response({"artifacts": [a.to_meta_dict() for a in session.artifacts.items]})

    async def api_artifact_get(req: web.Request) -> web.Response:
        aid = int(req.match_info["id"])
        art = session.artifacts.get(aid)
        if art is None:
            return web.json_response({"error": "not found"}, status=404)
        return web.json_response({**art.to_meta_dict(), "body": art.body})

    app.router.add_get("/", serve_index)
    app.router.add_get("/favicon.svg", serve_favicon)
    app.router.add_get("/favicon.ico", serve_favicon)
    app.router.add_get("/assets/{name}", serve_asset)

    app.router.add_get("/api/session", api_session)
    app.router.add_get("/api/commands", api_commands)
    app.router.add_get("/api/perf", api_perf)
    app.router.add_get("/api/events", api_events)
    app.router.add_post("/api/submit", api_submit)
    app.router.add_post("/api/cancel", api_cancel)
    app.router.add_post("/api/approve", api_approve)

    app.router.add_get("/api/resolve", api_resolve_state)
    app.router.add_post("/api/resolve/reconnect", api_resolve_reconnect)
    app.router.add_post("/api/resolve/grab", api_resolve_grab)
    app.router.add_post("/api/resolve/undo", api_resolve_undo)
    app.router.add_get("/api/media/still/{id}", api_serve_still)

    app.router.add_get("/api/context", api_context_get)
    app.router.add_post("/api/context/drop", api_context_drop)
    app.router.add_post("/api/context/pin", api_context_pin)
    app.router.add_post("/api/compact", api_compact)

    app.router.add_get("/api/prompts", api_prompts_get)
    app.router.add_post("/api/prompts/save", api_prompts_save)
    app.router.add_post("/api/prompts/revert", api_prompts_revert)
    app.router.add_post("/api/prompts/apply", api_prompts_apply)

    app.router.add_get("/api/models", api_models_get)
    app.router.add_post("/api/model", api_model_post)
    app.router.add_get("/api/keys", api_keys_get)
    app.router.add_post("/api/keys", api_keys_post)
    app.router.add_get("/api/env", api_env_get)
    app.router.add_post("/api/env", api_env_post)

    app.router.add_get("/api/artifacts", api_artifacts_list)
    app.router.add_get("/api/artifact/{id}", api_artifact_get)

    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, host=host, port=port)
    await site.start()

    # Resolve actual port if port=0 was passed
    actual_port = port
    if site._server and site._server.sockets:
        actual_port = int(site._server.sockets[0].getsockname()[1])

    from urllib.parse import quote

    disp_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    url = f"http://{disp_host}:{actual_port}"
    q_tok = quote(secret, safe="")
    auth_url = f"{url}/?token={q_tok}"

    lan_auth_url: str | None = None
    if host in ("0.0.0.0", "::"):
        lan_ip = _detect_lan_ipv4()
        if lan_ip:
            lan_auth_url = f"http://{lan_ip}:{actual_port}/?token={q_tok}"

    return RunningWebServer(
        host=host,
        port=actual_port,
        token=secret,
        runner=runner,
        url=url,
        auth_url=auth_url,
        lan_auth_url=lan_auth_url,
        shutdown_event=shutdown_ev,
        sse_queues=sse_queues,
    )