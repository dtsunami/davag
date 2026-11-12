"""In-Resolve TCP JSON-RPC server + main-thread queue dispatcher (stdlib only).

Why this architecture matters in DaVinci Resolve:
- Scripts started via `Workspace -> Scripts` run inside `fuscript.exe`. If the main thread
  returns, Resolve destroys the script session and kills background threads.
- Resolve/Fusion C++ proxy objects (`resolve`, `fusion`, `bmd`) are bound to the main
  script thread; calling them directly from arbitrary socket threads risks races or crashes.
- Therefore:
  1. A background `socketserver.ThreadingTCPServer` handles socket I/O and authentication,
     pushes `(op, params)` onto a thread-safe `queue.Queue`, and waits on a `threading.Event`.
  2. The main thread keeps `fuscript` alive (via a compact `UIManager` status dialog when
     available, or a clean poll loop in headless tests) and drains `drain_pending()` on the
     main thread!
"""

from __future__ import annotations

import os
import queue
import socketserver
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from davag.resolve_ops import OpContext, OpError, dispatch_op

from .protocol import (
    DEFAULT_BRIDGE_HOST,
    DEFAULT_BRIDGE_PORT,
    MAX_LINE_BYTES,
    BridgeDiscovery,
    decode_frame,
    encode_frame,
    generate_bridge_token,
    remove_discovery_if_owned,
    verify_bridge_token,
    write_discovery,
)


@dataclass
class _PendingRPC:
    op: str
    params: dict[str, Any]
    done: threading.Event = field(default_factory=threading.Event)
    response: dict[str, Any] = field(default_factory=dict)


class _BridgeTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(
        self,
        server_address: tuple[str, int],
        handler_cls: type[socketserver.BaseRequestHandler],
        bridge: ResolveBridgeServer,
    ) -> None:
        self.bridge = bridge
        super().__init__(server_address, handler_cls)


class _BridgeConnectionHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        bridge: ResolveBridgeServer = self.server.bridge  # type: ignore[attr-defined]
        while not bridge.stopped:
            try:
                raw_line = self.rfile.readline(MAX_LINE_BYTES + 1)
            except OSError:
                break
            if not raw_line:
                break

            req_id: Any = None
            try:
                msg = decode_frame(raw_line)
                req_id = msg.get("id")
                if not verify_bridge_token(bridge.token, msg.get("token")):
                    self._send(
                        {
                            "id": req_id,
                            "ok": False,
                            "error": {"code": "UNAUTHORIZED", "message": "Invalid or missing bridge token"},
                        }
                    )
                    continue

                op_name = str(msg.get("op", "")).strip()
                if not op_name:
                    self._send(
                        {
                            "id": req_id,
                            "ok": False,
                            "error": {"code": "BAD_REQUEST", "message": "Missing 'op' field"},
                        }
                    )
                    continue

                if op_name == "bridge.shutdown":
                    bridge.request_stop()
                    self._send({"id": req_id, "ok": True, "result": {"stopping": True}})
                    break

                params = msg.get("params") or {}
                if not isinstance(params, dict):
                    raise TypeError("'params' must be a JSON object")

                res = bridge.enqueue_and_wait(op_name, params, timeout=float(msg.get("timeout", 60.0)))
                self._send({"id": req_id, **res})
            except Exception as err:  # noqa: BLE001 — wire boundary must return structured JSON error
                self._send(
                    {
                        "id": req_id,
                        "ok": False,
                        "error": {"code": "PROTOCOL_ERROR", "message": str(err)},
                    }
                )

    def _send(self, payload: dict[str, Any]) -> None:
        try:
            self.wfile.write(encode_frame(payload))
            self.wfile.flush()
        except OSError:
            pass


class ResolveBridgeServer:
    """Owns the localhost TCP socket and main-thread execution queue."""

    def __init__(
        self,
        *,
        resolve: Any,
        fusion: Any = None,
        bmd: Any = None,
        host: str = DEFAULT_BRIDGE_HOST,
        port: int = DEFAULT_BRIDGE_PORT,
        token: str | None = None,
        home: str | Path | None = None,
        write_discovery_file: bool = True,
    ) -> None:
        self.ctx = OpContext(resolve=resolve, fusion=fusion, bmd=bmd, mode="bridge")
        self.host = host
        self.requested_port = port
        self.token = token or generate_bridge_token()
        self.home = home
        self.write_discovery_file = write_discovery_file

        self._queue: queue.Queue[_PendingRPC] = queue.Queue()
        self._stop_event = threading.Event()
        self._tcp: _BridgeTCPServer | None = None
        self._thread: threading.Thread | None = None
        self.ops_executed = 0
        self.last_op: str = "—"

    @property
    def port(self) -> int:
        if self._tcp is not None:
            return int(self._tcp.server_address[1])
        return self.requested_port

    @property
    def stopped(self) -> bool:
        return self._stop_event.is_set()

    def start_background_listener(self) -> BridgeDiscovery:
        self._tcp = _BridgeTCPServer((self.host, self.requested_port), _BridgeConnectionHandler, self)
        self._thread = threading.Thread(
            target=self._tcp.serve_forever,
            kwargs={"poll_interval": 0.1},
            name="davag-bridge-tcp",
            daemon=True,
        )
        self._thread.start()

        info = BridgeDiscovery(
            host=self.host,
            port=self.port,
            token=self.token,
            pid=os.getpid(),
            started_at=time.time(),
        )
        if self.write_discovery_file:
            write_discovery(info, self.home)
        return info

    def enqueue_and_wait(
        self, op_name: str, params: dict[str, Any], *, timeout: float = 60.0
    ) -> dict[str, Any]:
        if self.stopped:
            return {"ok": False, "error": {"code": "STOPPED", "message": "Bridge server is shutting down"}}
        item = _PendingRPC(op=op_name, params=params)
        self._queue.put(item)
        if not item.done.wait(timeout=max(0.5, timeout)):
            return {
                "ok": False,
                "error": {
                    "code": "TIMEOUT",
                    "message": f'Main-thread execution timed out after {timeout:.1f}s waiting for "{op_name}"',
                },
            }
        return item.response

    def drain_pending(self, max_items: int = 32) -> int:
        """Execute queued RPCs on the caller's thread (Resolve's main `fuscript` thread)."""
        processed = 0
        while processed < max_items:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break

            try:
                result = dispatch_op(self.ctx, item.op, item.params)
                item.response = {"ok": True, "result": result}
                self.ops_executed += 1
                self.last_op = item.op
            except OpError as err:
                item.response = {
                    "ok": False,
                    "error": {"code": err.code, "message": str(err)},
                }
            except Exception as err:  # noqa: BLE001 — catch unexpected Resolve proxy errors cleanly
                item.response = {
                    "ok": False,
                    "error": {"code": "INTERNAL_ERROR", "message": f"{type(err).__name__}: {err}"},
                }
            finally:
                item.done.set()
                processed += 1
        return processed

    def request_stop(self) -> None:
        self._stop_event.set()

    def close(self) -> None:
        self._stop_event.set()
        if self._tcp is not None:
            try:
                self._tcp.shutdown()
                self._tcp.server_close()
            except OSError:
                pass
            self._tcp = None
        # Unblock any callers still waiting in the queue
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break
            item.response = {"ok": False, "error": {"code": "STOPPED", "message": "Bridge closed"}}
            item.done.set()
        if self.write_discovery_file:
            remove_discovery_if_owned(os.getpid(), self.home)

    def serve_main_thread(self, *, poll_interval: float = 0.02, prefer_ui: bool = True) -> None:
        """Start listener and pump `drain_pending()` on the main thread until stopped."""
        info = self.start_background_listener()
        try:
            ui = getattr(self.ctx.fusion, "UIManager", None) if prefer_ui and self.ctx.fusion else None
            uidisp_fn = getattr(self.ctx.bmd, "UIDispatcher", None) if prefer_ui and self.ctx.bmd else None
            if ui is not None and callable(uidisp_fn):
                self._run_with_uimanager(ui, uidisp_fn, info, poll_interval)
            else:
                while not self.stopped:
                    if self.drain_pending() == 0:
                        self._stop_event.wait(timeout=poll_interval)
        finally:
            self.close()

    def _run_with_uimanager(
        self, ui: Any, uidisp_fn: Any, info: BridgeDiscovery, poll_interval: float
    ) -> None:
        """Display a tiny Resolve UIManager keep-alive window while pumping the RPC queue."""
        disp = uidisp_fn(ui)
        win = disp.AddWindow(
            {
                "ID": "DavagBridgeWin",
                "WindowTitle": "davag Bridge",
                "Geometry": [120, 120, 340, 115],
            },
            ui.VGroup(
                {"Spacing": 6},
                [
                    ui.Label(
                        {
                            "ID": "StatusLbl",
                            "Text": f"Listening on {info.host}:{info.port}",
                            "Weight": 0,
                        }
                    ),
                    ui.Label(
                        {
                            "ID": "CountLbl",
                            "Text": "Ops executed: 0",
                            "Weight": 0,
                        }
                    ),
                    ui.Button({"ID": "StopBtn", "Text": "Stop davag Bridge", "Weight": 0}),
                ],
            ),
        )
        items = win.GetItems()

        def _on_close(_ev: Any) -> None:
            self.request_stop()
            disp.ExitLoop()

        win.On.DavagBridgeWin.Close = _on_close
        win.On.StopBtn.Clicked = _on_close
        win.Show()

        # Fusion's UIDispatcher blocks if RunLoop() has no timer; check if StepLoop/ProcessEvents
        # is available, else use a lightweight UI timer or fall back cleanly.
        add_timer = getattr(disp, "AddTimer", None)
        if callable(add_timer):
            # Keep strong reference on `self` so Fusion's C++ QTimer proxy is not GC'd mid-loop
            self._ui_timer = add_timer(
                {"ID": "DavagPoll", "Interval": max(15, int(poll_interval * 1000))}
            )
            start_fn = getattr(self._ui_timer, "Start", None)
            if callable(start_fn):
                start_fn()

            def _on_timer(_ev: Any) -> None:
                if self.stopped:
                    disp.ExitLoop()
                    return
                if self.drain_pending() > 0:
                    items["CountLbl"].Text = f"Ops executed: {self.ops_executed} (last: {self.last_op})"

            win.On.DavagPoll.Timeout = _on_timer
            disp.RunLoop()
        else:
            # Fallback if older Fusion build lacks AddTimer: pump drain_pending in short slices
            while not self.stopped:
                if self.drain_pending() > 0:
                    items["CountLbl"].Text = f"Ops executed: {self.ops_executed} (last: {self.last_op})"
                self._stop_event.wait(timeout=poll_interval)

        win.Hide()


def run_inside_resolve(
    resolve_obj: Any = None,
    fusion_obj: Any = None,
    bmd_obj: Any = None,
    *,
    port: int = DEFAULT_BRIDGE_PORT,
) -> None:
    """Entrypoint called by `davag_bridge.py` inside Resolve's `Workspace -> Scripts` menu."""
    import builtins

    res = resolve_obj or getattr(builtins, "resolve", None) or globals().get("resolve")
    fus = fusion_obj or getattr(builtins, "fusion", None) or globals().get("fusion")
    bmd_mod = bmd_obj or getattr(builtins, "bmd", None) or globals().get("bmd")

    if res is None and bmd_mod is not None and hasattr(bmd_mod, "scriptapp"):
        res = bmd_mod.scriptapp("Resolve")
    if fus is None and res is not None and hasattr(res, "Fusion"):
        fus = res.Fusion()

    if res is None:
        raise RuntimeError("davag_bridge must be run inside DaVinci Resolve (no 'resolve' handle found).")

    print(f"[davag] Starting internal Resolve bridge on 127.0.0.1:{port} ...")
    srv = ResolveBridgeServer(resolve=res, fusion=fus, bmd=bmd_mod, port=port)
    srv.serve_main_thread()
    print("[davag] Bridge stopped.")