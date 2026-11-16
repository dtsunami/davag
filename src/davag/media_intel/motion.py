"""Deterministic Code-Based HTML / CSS / SVG / Canvas / JS Motion Overlay Renderer (`motion.py`).

How it works (Remotion-style deterministic frame stepping with zero extra pip dependencies):
1. Accepts self-contained HTML/CSS/SVG/JS (`html`) OR an existing `.html`/`.svg` file.
   - Authors can animate via:
     a) `window.renderFrame = (frame, timeSec, durationSec, fps) => { ... }` (Canvas / SVG / DOM),
     b) Standard CSS `@keyframes` or Web Animations API (`document.getAnimations()` are paused and
        scrubbed to exact `timeSec * 1000` on every frame!),
     c) Native `<svg>` SMIL animations (`svg.setCurrentTime(timeSec)`).
2. Launches local Headless Chrome / Microsoft Edge with `--remote-debugging-port=0` and
   `Emulation.setDefaultBackgroundColorOverride({r:0, g:0, b:0, a:0})` for true RGBA transparency.
3. Steps frame `0 .. total_frames - 1` over local CDP WebSocket (`aiohttp`) and muxes lossless RGBA
   PNG frames directly into a QuickTime PNG `.mov` (`-c:v copy`, zero re-encode, ~10-25x smaller on
   sparse overlays, Straight Alpha native to DaVinci Resolve Free & Studio!), with optional `codec="prores"`
   for 10-bit ProRes 4444 (`prores_ks -profile:v 4444 -pix_fmt yuva444p10le`).
   - For static callouts/cards (`duration_sec == 0` or `static=True`), outputs a crisp 32-bit RGBA `.png`.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

import aiohttp

DEVTOOLS_WS_RE = re.compile(r"DevTools listening on (ws://[^\s]+)")


def find_headless_browser() -> str:
    """Locate Chrome or Microsoft Edge executable for deterministic headless CDP rendering."""
    candidates = [
        shutil.which("chrome"),
        shutil.which("google-chrome"),
        shutil.which("chromium"),
        shutil.which("msedge"),
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    ]
    for c in candidates:
        if c and Path(c).is_file():
            return str(Path(c))
    raise RuntimeError(
        "Neither Chrome nor Microsoft Edge was found for HTML/SVG overlay rendering."
    )


def _wrap_svg_or_html(raw: str, width: int, height: int) -> str:
    stripped = raw.lstrip()
    if stripped.lower().startswith("<svg"):
        return f"""<!doctype html>
<html><head><meta charset="utf-8"><style>
  html, body {{ margin:0; padding:0; width:{width}px; height:{height}px; overflow:hidden; background:transparent !important; }}
  svg {{ width:100%; height:100%; display:block; }}
</style></head><body>{raw}</body></html>"""

    if "<html" not in stripped.lower():
        return f"""<!doctype html>
<html><head><meta charset="utf-8"><style>
  * {{ box-sizing:border-box; }}
  html, body {{ margin:0; padding:0; width:{width}px; height:{height}px; overflow:hidden; background:transparent !important; font-family: system-ui, -apple-system, 'Segoe UI', Inter, sans-serif; }}
</style></head><body>{raw}</body></html>"""
    return raw


class _CDPSession:
    def __init__(self, ws: aiohttp.ClientWebSocketResponse, session_id: str | None = None) -> None:
        self.ws = ws
        self.session_id = session_id
        self._seq = 0

    async def call(
        self, method: str, params: dict[str, Any] | None = None, *, session_id: str | None = None
    ) -> dict[str, Any]:
        self._seq += 1
        req_id = self._seq
        payload: dict[str, Any] = {"id": req_id, "method": method, "params": params or {}}
        eff_sid = session_id if session_id is not None else self.session_id
        if eff_sid:
            payload["sessionId"] = eff_sid
        await self.ws.send_json(payload)
        while True:
            msg = await self.ws.receive()
            if msg.type == aiohttp.WSMsgType.TEXT:
                data = msg.json()
                if data.get("id") == req_id:
                    if "error" in data:
                        raise RuntimeError(f"CDP {method} error: {data['error']}")
                    return data.get("result") or {}
            elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSING, aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.ERROR):
                raise RuntimeError(f"Headless browser WebSocket closed during {method}: type={msg.type} data={msg.data!r} extra={msg.extra!r} close_code={self.ws.close_code}")


async def _wait_ws_endpoint(proc: asyncio.subprocess.Process, timeout: float = 10.0) -> str:
    assert proc.stderr is not None
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        line_bytes = await asyncio.wait_for(proc.stderr.readline(), timeout=max(0.2, deadline - asyncio.get_running_loop().time()))
        if not line_bytes:
            break
        line = line_bytes.decode("utf-8", errors="replace")
        m = DEVTOOLS_WS_RE.search(line)
        if m:
            return m.group(1)
    raise RuntimeError("Timed out waiting for headless browser DevTools WebSocket.")


async def render_html_svg_overlay(
    *,
    html_or_svg: str,
    output_path: str | Path,
    width: int = 1920,
    height: int = 1080,
    fps: float = 30.0,
    duration_sec: float = 4.0,
    static: bool = False,
    codec: str = "png",
) -> dict[str, Any]:
    """Deterministically render HTML/CSS/SVG/Canvas/JS into a QuickTime PNG or ProRes 4444 Alpha `.mov` (or RGBA `.png`)."""
    browser_exe = find_headless_browser()
    ffmpeg = shutil.which("ffmpeg")
    if not static and not ffmpeg:
        raise RuntimeError("ffmpeg is required to mux/encode transparent overlay video.")

    codec_norm = (codec or "png").strip().lower()
    if codec_norm not in ("png", "prores", "prores_4444"):
        raise ValueError(f"Unsupported overlay codec '{codec}' (expected 'png' or 'prores').")
    use_prores = codec_norm in ("prores", "prores_4444")

    dst = Path(output_path).resolve()
    is_static = static or duration_sec <= 0 or dst.suffix.lower() == ".png"
    if is_static and dst.suffix.lower() != ".png":
        dst = dst.with_suffix(".png")
    elif not is_static and dst.suffix.lower() != ".mov":
        dst = dst.with_suffix(".mov")
    dst.parent.mkdir(parents=True, exist_ok=True)

    full_doc = _wrap_svg_or_html(html_or_svg, width, height)
    tmp_dir = Path(tempfile.mkdtemp(prefix="davag_cdp_"))
    html_file = tmp_dir / "overlay.html"
    html_file.write_text(full_doc, encoding="utf-8")
    profile_dir = tmp_dir / "profile"

    proc = await asyncio.create_subprocess_exec(
        browser_exe,
        "--headless=new",
        "--no-sandbox",
        "--remote-allow-origins=*",
        "--no-first-run",
        "--no-default-browser-check",
        "--hide-scrollbars",
        "--mute-audio",
        f"--user-data-dir={profile_dir}",
        f"--window-size={width},{height}",
        "--remote-debugging-port=0",
        "about:blank",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )

    ff_proc: asyncio.subprocess.Process | None = None
    total_frames = 1 if is_static else max(1, round(duration_sec * fps))

    try:
        browser_ws_url = await _wait_ws_endpoint(proc)
        http_base = browser_ws_url.split("/devtools/")[0].replace("ws://", "http://")
        async with aiohttp.ClientSession() as http:
            page_ws_url = ""
            async with http.get(f"{http_base}/json/list") as r_list:
                targets = await r_list.json()
                page_t = next((t for t in targets if t.get("type") == "page" and t.get("webSocketDebuggerUrl")), None)
                if page_t:
                    page_ws_url = page_t["webSocketDebuggerUrl"]
            if not page_ws_url:
                async with http.put(f"{http_base}/json/new?about:blank") as r_new:
                    new_t = await r_new.json()
                    page_ws_url = str(new_t.get("webSocketDebuggerUrl", ""))
            if not page_ws_url:
                raise RuntimeError("Headless browser exposed no inspectable page target.")

            async with http.ws_connect(page_ws_url, max_msg_size=64 * 1024 * 1024) as p_ws:
                cdp = _CDPSession(p_ws)
                await cdp.call("Page.enable")
                await cdp.call("Runtime.enable")
                await cdp.call(
                    "Emulation.setDeviceMetricsOverride",
                    {"width": width, "height": height, "deviceScaleFactor": 1, "mobile": False},
                )
                await cdp.call(
                    "Emulation.setDefaultBackgroundColorOverride",
                    {"color": {"r": 0, "g": 0, "b": 0, "a": 0}},
                )
                ft = await cdp.call("Page.getFrameTree")
                frame_id = ft["frameTree"]["frame"]["id"]
                await cdp.call("Page.setDocumentContent", {"frameId": frame_id, "html": full_doc})

                if is_static:
                    await cdp.call(
                        "Runtime.evaluate",
                        {
                            "expression": f"""(() => {{
                                if (typeof window.renderFrame === 'function') window.renderFrame(0, 0, {duration_sec}, {fps});
                            }})()""",
                            "awaitPromise": True,
                        },
                    )
                    shot = await cdp.call("Page.captureScreenshot", {"format": "png", "omitBackground": True})
                    png_bytes = base64.b64decode(shot["data"])
                    dst.write_bytes(png_bytes)
                else:
                    assert ffmpeg is not None
                    v_codec_args = (
                        [
                            "-c:v",
                            "prores_ks",
                            "-profile:v",
                            "4444",
                            "-pix_fmt",
                            "yuva444p10le",
                            "-vendor",
                            "apl0",
                        ]
                        if use_prores
                        else ["-c:v", "copy"]
                    )
                    ff_cmd = [
                        ffmpeg,
                        "-y",
                        "-hide_banner",
                        "-loglevel",
                        "error",
                        "-f",
                        "image2pipe",
                        "-vcodec",
                        "png",
                        "-framerate",
                        f"{fps:.4f}",
                        "-i",
                        "-",
                        *v_codec_args,
                        str(dst),
                    ]
                    ff_proc = await asyncio.create_subprocess_exec(
                        *ff_cmd,
                        stdin=asyncio.subprocess.PIPE,
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    assert ff_proc.stdin is not None

                    for f_idx in range(total_frames):
                        t_sec = f_idx / fps
                        step_js = f"""(() => {{
                            const t = {t_sec:.6f};
                            document.getAnimations({{ subtree: true }}).forEach(a => {{
                                a.pause();
                                a.currentTime = t * 1000;
                            }});
                            document.querySelectorAll('svg').forEach(s => {{
                                if (typeof s.pauseAnimations === 'function') {{
                                    s.pauseAnimations();
                                    s.setCurrentTime(t);
                                }}
                            }});
                            if (typeof window.renderFrame === 'function') {{
                                return window.renderFrame({f_idx}, t, {duration_sec:.6f}, {fps:.4f});
                            }}
                        }})()"""
                        await cdp.call("Runtime.evaluate", {"expression": step_js, "awaitPromise": True})
                        shot = await cdp.call("Page.captureScreenshot", {"format": "png", "omitBackground": True})
                        ff_proc.stdin.write(base64.b64decode(shot["data"]))
                        await ff_proc.stdin.drain()

                    ff_proc.stdin.close()
                    _, ff_err_bytes = await asyncio.wait_for(ff_proc.communicate(), timeout=120)
                    rc = ff_proc.returncode
                    ff_proc = None
                    if rc != 0:
                        label = "ProRes 4444 encode" if use_prores else "QuickTime PNG mux"
                        raise RuntimeError(f"FFmpeg {label} failed: {ff_err_bytes.decode('utf-8', errors='replace').strip()}")
    finally:
        if ff_proc is not None:
            with contextlib.suppress(Exception):
                if ff_proc.stdin:
                    ff_proc.stdin.close()
                ff_proc.kill()
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        with contextlib.suppress(Exception):
            await proc.wait()
        shutil.rmtree(tmp_dir, ignore_errors=True)

    return {
        "path": str(dst),
        "bytes": dst.stat().st_size,
        "width": width,
        "height": height,
        "fps": fps,
        "frames": total_frames,
        "duration_sec": 0.0 if is_static else round(total_frames / fps, 3),
        "format": "png_rgba" if is_static else ("prores_4444_alpha" if use_prores else "mov_png_rgba"),
    }