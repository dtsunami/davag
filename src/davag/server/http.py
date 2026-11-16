"""Modular FastAPI + Uvicorn Web Sidecar (`davag.server.http`).

Replaces the legacy 1,000-line `aiohttp.web` closure monolith with clean typed FastAPI routers:
- `routes_core.py`: session state, SSE stream (`Last-Event-ID` / `?since=` replay), submit/cancel/approve,
  context ledger, prompt editor, model/key/.env profiles, roles, artifacts, and `/api/docs`.
- `routes_resolve.py`: DaVinci Resolve Live Inspector, Clip Studio, Voiceover Teleprompter, & media stills.
- `routes_overlays.py`: Interactive visual timeline, lazy-loaded disk-cached frame thumbnails,
  drag-to-adjust markers/overlays, and ISO SVG QR + QuickTime PNG / ProRes 4444 motion overlay rendering.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse, JSONResponse

from davag.session.bootstrap import Session

from .auth import assert_safe_bind, make_web_token, read_request_token, token_matches
from .routes_core import router as core_router
from .routes_overlays import router as overlays_router
from .routes_resolve import router as resolve_router
from .state import WebState

WEB_STATIC_DIR = Path(__file__).resolve().parents[1] / "ui" / "web"
_PROACTOR_PATCHED = False


def is_benign_socket_disconnect(exc: BaseException | None) -> bool:
    if isinstance(exc, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
        return True
    return bool(isinstance(exc, OSError) and getattr(exc, "winerror", None) in (64, 121, 995, 10038, 10053, 10054))


def install_win32_proactor_reset_guard() -> None:
    """Fix CPython Win32 Proactor `_call_connection_lost` raising unhandled `WinError 10054`
    on `self._sock.shutdown(SHUT_RDWR)` when browsers abort in-flight `<img>` / `<video>` sockets
    (which otherwise leaks the socket AND triggers prompt_toolkit's blocking 'Press ENTER' prompt!).
    """
    global _PROACTOR_PATCHED
    if _PROACTOR_PATCHED or sys.platform != "win32":
        return
    _PROACTOR_PATCHED = True
    with contextlib.suppress(Exception):
        from asyncio import proactor_events

        cls = proactor_events._ProactorBasePipeTransport

        def _safe_call_connection_lost(self: Any, exc: Exception | None) -> None:
            if self._called_connection_lost:
                return
            try:
                self._protocol.connection_lost(exc)
            finally:
                if hasattr(self._sock, "shutdown") and self._sock.fileno() != -1:
                    with contextlib.suppress(OSError):
                        self._sock.shutdown(socket.SHUT_RDWR)
                if self._sock is not None:
                    with contextlib.suppress(OSError):
                        self._sock.close()
                    self._sock = None
                server = self._server
                if server is not None:
                    server._detach(self)
                    self._server = None
                self._called_connection_lost = True

        cls._call_connection_lost = _safe_call_connection_lost  # type: ignore[method-assign]


def _detect_lan_ipv4() -> str | None:
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
    server: uvicorn.Server
    serve_task: asyncio.Task[None]
    sock: socket.socket
    url: str
    auth_url: str
    lan_auth_url: str | None
    shutdown_event: asyncio.Event
    sse_queues: set[asyncio.Queue[dict[str, Any] | None]]

    async def close(self) -> None:
        self.shutdown_event.set()
        for q in list(self.sse_queues):
            q.put_nowait(None)
        self.server.should_exit = True
        with contextlib.suppress(asyncio.CancelledError, TimeoutError):
            await asyncio.wait_for(self.serve_task, timeout=3.0)
        with contextlib.suppress(OSError):
            self.sock.close()


def create_fastapi_app(state: WebState) -> FastAPI:
    app = FastAPI(title="davag Web Studio", docs_url=None, redoc_url=None, openapi_url="/api/openapi.json")
    app.state.davag = state

    @app.middleware("http")
    async def bearer_auth_middleware(req: Request, call_next: Callable[[Request], Any]) -> Response:
        path = req.url.path
        if path == "/" or path.startswith("/assets/") or path in ("/favicon.svg", "/favicon.ico"):
            res_open: Response = await call_next(req)
            return res_open

        allow_q = (
            path in ("/api/events", "/api/overlays/thumb")
            or path.startswith(("/api/media/", "/api/overlays/preview/", "/api/overlays/render-video/"))
        )
        given = read_request_token(req, allow_query=allow_q)
        if not token_matches(state.secret, given):
            return JSONResponse({"error": "bad or missing token"}, status_code=401)
        res_authed: Response = await call_next(req)
        return res_authed

    @app.get("/", include_in_schema=False)
    async def serve_index() -> FileResponse:
        return FileResponse(WEB_STATIC_DIR / "index.html")

    @app.get("/favicon.svg", include_in_schema=False)
    @app.get("/favicon.ico", include_in_schema=False)
    async def serve_favicon() -> FileResponse:
        return FileResponse(WEB_STATIC_DIR / "favicon.svg")

    @app.get("/assets/{name}", include_in_schema=False)
    async def serve_asset(name: str) -> Response:
        target = (WEB_STATIC_DIR / Path(name).name).resolve()
        if target.parent != WEB_STATIC_DIR or not target.is_file():
            return JSONResponse({"error": "not found"}, status_code=404)
        return FileResponse(target)

    app.include_router(core_router)
    app.include_router(resolve_router)
    app.include_router(overlays_router)
    return app


async def start_web_server(
    *,
    session: Session,
    submit_fn: Callable[[str, str], Any],
    host: str = "127.0.0.1",
    port: int = 7879,
    token: str | None = None,
) -> RunningWebServer:
    assert_safe_bind(host=host, yolo=session.cfg.yolo)
    install_win32_proactor_reset_guard()
    loop = asyncio.get_running_loop()
    prev_eh = loop.get_exception_handler()

    def _loop_eh(lp: asyncio.AbstractEventLoop, ctx: dict[str, Any]) -> None:
        if is_benign_socket_disconnect(ctx.get("exception")):
            return
        if prev_eh is not None:
            prev_eh(lp, ctx)
        else:
            lp.default_exception_handler(ctx)

    loop.set_exception_handler(_loop_eh)
    secret = token or session.cfg.web_token or make_web_token()
    state = WebState(session=session, submit_fn=submit_fn, secret=secret)

    def _on_bus_media(ev: dict[str, Any]) -> None:
        if ev["type"] == "media-preview":
            d = ev["data"]
            p = Path(str(d["path"]))
            if p.is_file():
                url = state.register_media_file(p)
                session.bus.publish("viewer-media", {"kind": d["kind"], "url": url, "title": d.get("title", p.name)})
        elif ev["type"] == "ops-result":
            for m in (ev["data"] or {}).get("attached_media") or []:
                p = Path(str(m.get("path", "")))
                if p.is_file():
                    kind = "video" if m["kind"] == "video" else "image"
                    url = state.register_media_file(p)
                    session.bus.publish("viewer-media", {"kind": kind, "url": url, "title": p.name})

    session.bus.subscribe(_on_bus_media)

    app = create_fastapi_app(state)

    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, port))
    sock.setblocking(False)
    actual_port = int(sock.getsockname()[1])

    uv_cfg = uvicorn.Config(app, host=host, port=actual_port, log_level="warning", access_log=False)
    server = uvicorn.Server(uv_cfg)
    # Prevent uvicorn from installing OS signal handlers when embedded inside prompt_toolkit REPL!
    server.install_signal_handlers = lambda: None  # type: ignore[method-assign]
    serve_task = asyncio.create_task(server.serve(sockets=[sock]))

    for _ in range(100):
        if server.started or serve_task.done():
            break
        await asyncio.sleep(0.01)

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
        server=server,
        serve_task=serve_task,
        sock=sock,
        url=url,
        auth_url=auth_url,
        lan_auth_url=lan_auth_url,
        shutdown_event=state.shutdown_ev,
        sse_queues=state.sse_queues,
    )