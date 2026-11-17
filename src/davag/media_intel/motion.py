"""Dual-Path Deterministic Motion Overlay Renderer (`motion.py`).

Supports three deterministic RGBA Straight-Alpha rendering backends (`backend="auto" | "native" | "cdp"`):
1. `native_v8_resvg` (Pure `<svg>` + optional JS `window.renderFrame`):
   - Evaluates frame state in a single Node.js V8 batch (or zero-JS direct path for static `<svg>`)
     and rasterizes SVG frames across CPU cores via Rust `resvg-py` SIMD (`pip install -e ".[motion]"`).
2. `native_node_canvas` (Pure `<canvas>` + JS `window.renderFrame`):
   - Evaluates frames in Node.js V8 with `@napi-rs/canvas` (or `canvas`) and streams raw RGBA bytes
     straight into FFmpeg with zero PNG intermediate overhead.
3. `cdp_parallel` (Universal HTML/CSS/SVG/Canvas/SMIL/Web-Animations fallback, zero extra pip deps):
   - Reuses a persistent warm Headless Chrome/Edge daemon across renders (eliminating cold-start tax).
   - Runs a `<4ms` dynamic DOM/SVG/`box-shadow`/`filter` bounding-box `auto_clip` probe so Chromium
     only rasterizes and PNG-encodes the active card/callout region, while FFmpeg `pad` restores the
     full transparent timeline canvas (`width x height`).
   - Renders frames concurrently across a multi-tab CDP worker pool (`eff_workers`).
   - Patches QuickTime `stsd` sample entry `depth = 32` (`_ensure_mov_png_rgba_depth32`) so DaVinci
     Resolve Free & Studio recognize Straight Alpha automatically on `"png"` and `"qtrle"` `.mov` files.
"""

from __future__ import annotations

import asyncio
import atexit
import base64
import concurrent.futures
import contextlib
import os
import re
import shutil
import struct
import subprocess
import tempfile
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any

import aiohttp

DEVTOOLS_WS_RE = re.compile(r"DevTools listening on (ws://[^\s]+)")
SUPPORTED_CODECS = ("png", "qtrle", "prores", "prores_4444")
SUPPORTED_BACKENDS = ("auto", "native", "cdp")

_SCRIPT_RE = re.compile(r"<script\b[^>]*>(?P<code>[\s\S]*?)</script>", re.IGNORECASE)
_COMMENT_RE = re.compile(r"<!--[\s\S]*?-->")
_STYLE_RE = re.compile(r"<style\b[^>]*>(?P<css>[\s\S]*?)</style>", re.IGNORECASE)
_HTML_WRAPPER_TAG_RE = re.compile(r"</?(?:!doctype|html|head|meta|body)\b[^>]*>", re.IGNORECASE)
_SVG_OPEN_RE = re.compile(r"<svg\b[^>]*>", re.IGNORECASE)

_BROWSER_LOCK = threading.Lock()
_WARM_BROWSER: dict[str, Any] | None = None
_NODE_CANVAS_MODULE_CACHE: tuple[str, str] | None | bool = False  # False = unprobed
_GLOBAL_NPM_ROOT_CACHE: str | None | bool = False


def _get_global_npm_root() -> str | None:
    global _GLOBAL_NPM_ROOT_CACHE
    if _GLOBAL_NPM_ROOT_CACHE is not False:
        return _GLOBAL_NPM_ROOT_CACHE  # type: ignore[return-value]
    appdata = os.environ.get("APPDATA")
    if appdata and (Path(appdata) / "npm" / "node_modules").is_dir():
        _GLOBAL_NPM_ROOT_CACHE = str((Path(appdata) / "npm" / "node_modules").resolve())
        return _GLOBAL_NPM_ROOT_CACHE
    npm_exe = shutil.which("npm")
    if npm_exe:
        with contextlib.suppress(Exception):
            res = subprocess.run(
                [npm_exe, "root", "-g"],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
            if res.returncode == 0 and res.stdout.strip():
                _GLOBAL_NPM_ROOT_CACHE = res.stdout.strip()
                return _GLOBAL_NPM_ROOT_CACHE
    _GLOBAL_NPM_ROOT_CACHE = None
    return None


def _build_node_env() -> dict[str, str]:
    """Construct environment with `NODE_PATH` covering workspace, `davag` repo, `~/.davag`, and `npm link` global roots."""
    env = dict(os.environ)
    repo_root = Path(__file__).resolve().parents[3]
    davag_home = Path(os.environ.get("DAVAG_HOME") or (Path.home() / ".davag"))
    g_root = _get_global_npm_root()

    cand_dirs: list[Path] = [
        Path.cwd() / "node_modules",
        repo_root / "node_modules",
        davag_home / "node_modules",
    ]
    if g_root:
        gp = Path(g_root)
        cand_dirs.append(gp / "davag-motion" / "node_modules")
        cand_dirs.append(gp)
    for sys_nm in ("/usr/local/lib/node_modules", "/opt/homebrew/lib/node_modules"):
        cand_dirs.append(Path(sys_nm) / "davag-motion" / "node_modules")
        cand_dirs.append(Path(sys_nm))

    seen: set[str] = set()
    node_paths: list[str] = []
    for p in cand_dirs:
        with contextlib.suppress(OSError):
            if p.is_dir():
                rp = str(p.resolve())
                if rp.lower() not in seen:
                    seen.add(rp.lower())
                    node_paths.append(rp)
    if env.get("NODE_PATH"):
        for raw_p in env["NODE_PATH"].split(os.pathsep):
            if raw_p and raw_p.lower() not in seen:
                seen.add(raw_p.lower())
                node_paths.append(raw_p)
    if node_paths:
        env["NODE_PATH"] = os.pathsep.join(node_paths)
    return env


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


def probe_motion_capabilities() -> dict[str, Any]:
    """Inspect available motion-overlay rendering backends (`native_v8_resvg`, `native_node_canvas`, `cdp_parallel`)."""
    import importlib.metadata
    import importlib.util

    resvg_ver: str | None = None
    if importlib.util.find_spec("resvg_py") is not None:
        try:
            resvg_ver = importlib.metadata.version("resvg-py")
        except importlib.metadata.PackageNotFoundError:
            resvg_ver = "installed"

    node_path = shutil.which("node")
    canvas_hit = _probe_node_canvas_module() if node_path else None
    canvas_mod = canvas_hit[0] if canvas_hit else None
    repo_root = Path(__file__).resolve().parents[3]
    try:
        browser_path: str | None = find_headless_browser()
    except RuntimeError:
        browser_path = None

    if resvg_ver and node_path:
        svg_status = f"native_v8_resvg (resvg-py {resvg_ver} + node)"
    elif resvg_ver:
        svg_status = f"native_v8_resvg static-only (resvg-py {resvg_ver}; install node for JS)"
    elif browser_path:
        svg_status = "cdp_parallel fallback (install `pip install -e '.[motion]'` for native_v8_resvg)"
    else:
        svg_status = "unavailable (install `resvg-py` or Chrome/Edge)"

    if node_path and canvas_mod:
        canvas_status = f"native_node_canvas ({canvas_mod} + node)"
    elif browser_path:
        canvas_status = f"cdp_parallel fallback (run `npm --prefix {repo_root} install` for @napi-rs/canvas)"
    else:
        canvas_status = "unavailable (install Chrome/Edge or `@napi-rs/canvas`)"

    return {
        "resvg_version": resvg_ver,
        "node_path": node_path,
        "node_canvas_module": canvas_mod,
        "browser_path": browser_path,
        "svg_status": svg_status,
        "canvas_status": canvas_status,
        "cdp_ready": browser_path is not None,
        "codecs": ["png (stsd depth=32)", "qtrle", "prores (4444)"],
    }


def shutdown_warm_browser() -> None:
    """Terminate the persistent warm headless browser daemon if running."""
    global _WARM_BROWSER
    with _BROWSER_LOCK:
        info = _WARM_BROWSER
        _WARM_BROWSER = None
    if not info:
        return
    proc: subprocess.Popen[bytes] | None = info.get("proc")
    profile_dir: Path | None = info.get("profile_dir")
    if proc is not None:
        with contextlib.suppress(Exception):
            proc.kill()
        with contextlib.suppress(Exception):
            proc.wait(timeout=3.0)
    if profile_dir is not None:
        shutil.rmtree(profile_dir, ignore_errors=True)


atexit.register(shutdown_warm_browser)


def _is_warm_browser_healthy(info: dict[str, Any] | None) -> bool:
    if not info:
        return False
    proc: subprocess.Popen[bytes] | None = info.get("proc")
    http_base: str = info.get("http_base", "")
    if proc is None or proc.poll() is not None or not http_base:
        return False
    try:
        with urllib.request.urlopen(f"{http_base}/json/version", timeout=1.2) as resp:
            return resp.status == 200
    except (OSError, ValueError):
        return False


def _ensure_warm_browser_sync(browser_exe: str) -> str:
    """Return `http://127.0.0.1:<port>` for a warm headless Chromium daemon (loop-independent)."""
    global _WARM_BROWSER
    with _BROWSER_LOCK:
        if _is_warm_browser_healthy(_WARM_BROWSER):
            assert _WARM_BROWSER is not None
            return str(_WARM_BROWSER["http_base"])

        if _WARM_BROWSER is not None:
            old_proc = _WARM_BROWSER.get("proc")
            old_dir = _WARM_BROWSER.get("profile_dir")
            _WARM_BROWSER = None
            if old_proc is not None:
                with contextlib.suppress(Exception):
                    old_proc.kill()
            if old_dir is not None:
                shutil.rmtree(old_dir, ignore_errors=True)

        profile_dir = Path(tempfile.mkdtemp(prefix="davag_warm_cdp_"))
        proc = subprocess.Popen(
            [
                browser_exe,
                "--headless=new",
                "--no-sandbox",
                "--disable-gpu-vsync",
                "--disable-background-timer-throttling",
                "--disable-renderer-backgrounding",
                "--disable-backgrounding-occluded-windows",
                "--disable-ipc-flooding-protection",
                "--remote-allow-origins=*",
                "--no-first-run",
                "--no-default-browser-check",
                "--hide-scrollbars",
                "--mute-audio",
                f"--user-data-dir={profile_dir}",
                "--window-size=1920,1080",
                "--remote-debugging-port=0",
                "about:blank",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        assert proc.stderr is not None

        ws_url = ""
        deadline = time.monotonic() + 12.0
        while time.monotonic() < deadline:
            raw_line = proc.stderr.readline()
            if not raw_line:
                if proc.poll() is not None:
                    break
                time.sleep(0.02)
                continue
            line = raw_line.decode("utf-8", errors="replace")
            m = DEVTOOLS_WS_RE.search(line)
            if m:
                ws_url = m.group(1)
                break

        if not ws_url:
            with contextlib.suppress(Exception):
                proc.kill()
            shutil.rmtree(profile_dir, ignore_errors=True)
            raise RuntimeError("Timed out waiting for headless browser DevTools WebSocket.")

        def _drain_stderr(pipe: Any) -> None:
            with contextlib.suppress(Exception):
                while pipe.read(4096):
                    pass

        threading.Thread(target=_drain_stderr, args=(proc.stderr,), daemon=True).start()
        http_base = ws_url.split("/devtools/")[0].replace("ws://", "http://")
        _WARM_BROWSER = {
            "proc": proc,
            "http_base": http_base,
            "ws_url": ws_url,
            "profile_dir": profile_dir,
        }
        return http_base


def _patch_stsd_atoms_in_buffer(buf: bytearray, start: int, end: int) -> bool:
    """Recursively locate QuickTime `stsd` VisualSampleEntries (`png `, `rle `) and enforce `depth = 32`."""
    containers = {b"moov", b"trak", b"mdia", b"minf", b"stbl"}
    patched = False
    pos = start
    while pos + 8 <= end:
        size = struct.unpack_from(">I", buf, pos)[0]
        btype = bytes(buf[pos + 4 : pos + 8])
        hdr = 8
        if size == 1:
            if pos + 16 > end:
                break
            size = struct.unpack_from(">Q", buf, pos + 8)[0]
            hdr = 16
        elif size == 0:
            size = end - pos
        if size < hdr or pos + size > end:
            break

        box_end = pos + size
        if btype in containers:
            if _patch_stsd_atoms_in_buffer(buf, pos + hdr, box_end):
                patched = True
        elif btype == b"stsd" and pos + hdr + 8 <= box_end:
            entry_count = struct.unpack_from(">I", buf, pos + hdr + 4)[0]
            epos = pos + hdr + 8
            for _ in range(entry_count):
                if epos + 86 > box_end:
                    break
                esize = struct.unpack_from(">I", buf, epos)[0]
                efmt = bytes(buf[epos + 4 : epos + 8])
                if esize < 86 or epos + esize > box_end:
                    break
                if efmt in (b"png ", b"rle "):
                    cur_depth = struct.unpack_from(">H", buf, epos + 82)[0]
                    if cur_depth != 32:
                        struct.pack_into(">H", buf, epos + 82, 32)
                        struct.pack_into(">h", buf, epos + 84, -1)
                        patched = True
                epos += esize
        pos = box_end
    return patched


def _ensure_mov_png_rgba_depth32(mov_path: Path) -> bool:
    """Ensure QuickTime `.mov` `stsd` (`png ` / `rle `) reports 32-bit RGBA depth for Resolve Straight Alpha."""
    if not mov_path.is_file():
        return False
    file_size = mov_path.stat().st_size
    if file_size < 32:
        return False

    with mov_path.open("r+b") as f:
        pos = 0
        patched_any = False
        while pos + 8 <= file_size:
            f.seek(pos)
            hdr_bytes = f.read(16)
            if len(hdr_bytes) < 8:
                break
            size = struct.unpack_from(">I", hdr_bytes, 0)[0]
            btype = hdr_bytes[4:8]
            hdr = 8
            if size == 1:
                if len(hdr_bytes) < 16:
                    break
                size = struct.unpack_from(">Q", hdr_bytes, 8)[0]
                hdr = 16
            elif size == 0:
                size = file_size - pos
            if size < hdr or pos + size > file_size:
                break

            if btype == b"moov" and size <= 64 * 1024 * 1024:
                f.seek(pos)
                moov_buf = bytearray(f.read(size))
                if _patch_stsd_atoms_in_buffer(moov_buf, 0, len(moov_buf)):
                    f.seek(pos)
                    f.write(moov_buf)
                    patched_any = True
            pos += size
        return patched_any


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


def _classify_overlay_source(raw: str) -> tuple[str, str, str]:
    """Classify input into `('svg' | 'canvas' | 'html', clean_markup, combined_js)`."""
    no_comments = _COMMENT_RE.sub("", raw)
    scripts = [m.group("code") for m in _SCRIPT_RE.finditer(no_comments)]
    combined_js = "\n".join(scripts).strip()
    without_scripts = _SCRIPT_RE.sub("", no_comments)

    css_blocks = [m.group("css") for m in _STYLE_RE.finditer(without_scripts)]
    combined_css = "\n".join(css_blocks)
    body_only = _HTML_WRAPPER_TAG_RE.sub("", _STYLE_RE.sub("", without_scripts)).strip()

    low_body = body_only.lower()
    low_css = combined_css.lower()

    # Pure SVG check (no HTML layout elements, no CSS @keyframes, no SMIL <animate>, no foreignObject)
    if (
        low_body.startswith("<svg")
        and low_body.endswith("</svg>")
        and low_body.count("<svg") == 1
        and "<foreignobject" not in low_body
        and "<animate" not in low_body
        and "@keyframes" not in low_css
        and "document.getanimations" not in combined_js.lower()
        and not combined_css.strip()
    ):
        return "svg", body_only, combined_js

    # Pure Canvas check (single <canvas> element + JS renderFrame, no sibling DOM cards)
    if (
        low_body.startswith("<canvas")
        and low_body.count("<canvas") == 1
        and re.fullmatch(r"<canvas\b[^>]*>\s*</canvas>", body_only, flags=re.IGNORECASE)
        and "renderframe" in combined_js.lower()
        and "@keyframes" not in low_css
    ):
        return "canvas", body_only, combined_js

    return "html", raw, combined_js


def _ensure_svg_root_dimensions(svg_markup: str, width: int, height: int) -> str:
    m = _SVG_OPEN_RE.search(svg_markup)
    if not m:
        return svg_markup
    tag = m.group(0)
    tag_clean = re.sub(r'\s+(?:width|height)\s*=\s*(?:"[^"]*"|\'[^\']*\')', "", tag, flags=re.IGNORECASE)
    if "xmlns=" not in tag_clean.lower():
        tag_clean = tag_clean[:-1] + ' xmlns="http://www.w3.org/2000/svg">'
    injected = tag_clean[:-1] + f' width="{width}" height="{height}">'
    return svg_markup[: m.start()] + injected + svg_markup[m.end() :]


def _build_ffmpeg_encode_args(
    *,
    ffmpeg: str,
    dst: Path,
    fps: float,
    codec_norm: str,
    width: int,
    height: int,
    raw_rgba_input: bool = False,
    clip_rect: dict[str, int] | None = None,
) -> list[str]:
    in_args = (
        [
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgba",
            "-s",
            f"{width}x{height}",
            "-framerate",
            f"{fps:.4f}",
            "-i",
            "-",
        ]
        if raw_rgba_input
        else [
            "-f",
            "image2pipe",
            "-vcodec",
            "png",
            "-framerate",
            f"{fps:.4f}",
            "-i",
            "-",
        ]
    )

    vf_args: list[str] = []
    if clip_rect is not None:
        cx, cy = clip_rect["x"], clip_rect["y"]
        vf_args = ["-vf", f"format=rgba,pad={width}:{height}:{cx}:{cy}:color=0x00000000"]

    if codec_norm in ("prores", "prores_4444"):
        v_codec_args = [
            "-c:v",
            "prores_ks",
            "-profile:v",
            "4444",
            "-pix_fmt",
            "yuva444p10le",
            "-vendor",
            "apl0",
        ]
    elif codec_norm == "qtrle":
        v_codec_args = ["-c:v", "qtrle", "-pix_fmt", "argb"]
    else:
        if not raw_rgba_input and clip_rect is None:
            v_codec_args = ["-c:v", "copy"]
        else:
            v_codec_args = ["-c:v", "png", "-pix_fmt", "rgba", "-pred", "mixed"]

    return [
        ffmpeg,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        *in_args,
        *vf_args,
        *v_codec_args,
        str(dst),
    ]


_V8_SVG_BATCH_SCRIPT = r"""
const fs = require('fs');
const cfg = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));

function escXml(s) {
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
}
function escAttr(s) {
  return escXml(s).replace(/"/g, '&quot;');
}
function unescXml(s) {
  return String(s)
    .replace(/&quot;/g, '"')
    .replace(/&apos;/g, "'")
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/&amp;/g, '&');
}
function camelToKebab(k) {
  return k.replace(/[A-Z]/g, (m) => '-' + m.toLowerCase());
}

class MiniStyle {
  constructor(el) {
    this._el = el;
    this._map = new Map();
    return new Proxy(this, {
      get: (target, prop) => {
        if (prop in target || typeof prop === 'symbol') return target[prop];
        if (prop === 'cssText') return target._serialize();
        return target._map.get(camelToKebab(String(prop))) || '';
      },
      set: (target, prop, val) => {
        if (prop === 'cssText') {
          target._parse(String(val));
          return true;
        }
        const k = camelToKebab(String(prop));
        if (val === '' || val === null || val === undefined) target._map.delete(k);
        else target._map.set(k, String(val));
        target._sync();
        return true;
      }
    });
  }
  _parse(raw) {
    this._map.clear();
    for (const part of String(raw || '').split(';')) {
      const idx = part.indexOf(':');
      if (idx > 0) {
        const k = part.slice(0, idx).trim().toLowerCase();
        const v = part.slice(idx + 1).trim();
        if (k) this._map.set(k, v);
      }
    }
    this._sync();
  }
  _serialize() {
    const out = [];
    for (const [k, v] of this._map.entries()) out.push(`${k}:${v}`);
    return out.join(';');
  }
  _sync() {
    const s = this._serialize();
    if (s) this._el._attrs.set('style', s);
    else this._el._attrs.delete('style');
  }
  setProperty(k, v) { this._map.set(String(k), String(v)); this._sync(); }
  getPropertyValue(k) { return this._map.get(String(k)) || ''; }
  removeProperty(k) { this._map.delete(String(k)); this._sync(); }
}

class MiniNode {
  constructor(tag) {
    this.tagName = tag;
    this.nodeName = tag;
    this.nodeType = tag === '#text' ? 3 : 1;
    this._attrs = new Map();
    this.childNodes = [];
    this.parentNode = null;
    this._text = '';
    this._style = null;
  }
  get id() { return this._attrs.get('id') || ''; }
  set id(v) { this.setAttribute('id', v); }
  get className() { return this._attrs.get('class') || ''; }
  set className(v) { this.setAttribute('class', v); }
  get style() {
    if (!this._style) {
      this._style = new MiniStyle(this);
      if (this._attrs.has('style')) this._style._parse(this._attrs.get('style'));
    }
    return this._style;
  }
  get children() { return this.childNodes.filter((c) => c.nodeType === 1); }
  get firstElementChild() { return this.children[0] || null; }
  get lastElementChild() { const ch = this.children; return ch[ch.length - 1] || null; }
  get textContent() {
    if (this.nodeType === 3) return this._text;
    return this.childNodes.map((c) => c.textContent).join('');
  }
  set textContent(v) {
    if (this.nodeType === 3) { this._text = String(v); return; }
    this.childNodes = [];
    if (v !== '' && v !== null && v !== undefined) {
      const tn = new MiniNode('#text');
      tn._text = String(v);
      tn.parentNode = this;
      this.childNodes.push(tn);
    }
  }
  get innerHTML() {
    return this.childNodes.map((c) => c._serialize()).join('');
  }
  set innerHTML(fragment) {
    this.childNodes = [];
    if (fragment) {
      const parsed = parseXml('<g>' + String(fragment) + '</g>');
      for (const ch of parsed.childNodes) this.appendChild(ch);
    }
  }
  setAttribute(k, v) {
    const key = String(k);
    const val = String(v);
    this._attrs.set(key, val);
    if (key === 'style' && this._style) this._style._parse(val);
  }
  setAttributeNS(_ns, k, v) { this.setAttribute(k, v); }
  getAttribute(k) { return this._attrs.has(String(k)) ? this._attrs.get(String(k)) : null; }
  hasAttribute(k) { return this._attrs.has(String(k)); }
  removeAttribute(k) { this._attrs.delete(String(k)); }
  appendChild(ch) {
    if (ch.parentNode) ch.parentNode.removeChild(ch);
    ch.parentNode = this;
    this.childNodes.push(ch);
    return ch;
  }
  removeChild(ch) {
    const i = this.childNodes.indexOf(ch);
    if (i >= 0) { this.childNodes.splice(i, 1); ch.parentNode = null; }
    return ch;
  }
  get classList() {
    const self = this;
    const setOf = () => new Set((self.getAttribute('class') || '').split(/\s+/).filter(Boolean));
    return {
      add: (...cls) => { const s = setOf(); cls.forEach((c) => s.add(c)); self.setAttribute('class', [...s].join(' ')); },
      remove: (...cls) => { const s = setOf(); cls.forEach((c) => s.delete(c)); self.setAttribute('class', [...s].join(' ')); },
      contains: (c) => setOf().has(c),
      toggle: (c) => { const s = setOf(); if (s.has(c)) { s.delete(c); self.setAttribute('class', [...s].join(' ')); return false; } s.add(c); self.setAttribute('class', [...s].join(' ')); return true; }
    };
  }
  _matchesSimple(sel) {
    if (this.nodeType !== 1) return false;
    if (sel === '*') return true;
    const m = /^(?:([a-zA-Z0-9_:-]+))?(?:#([a-zA-Z0-9_:-]+))?(?:\.([a-zA-Z0-9_.:-]+))?$/.exec(sel.trim());
    if (!m) return false;
    const [, tag, id, clsChain] = m;
    if (tag && this.tagName.toLowerCase() !== tag.toLowerCase()) return false;
    if (id && this.id !== id) return false;
    if (clsChain) {
      const classes = new Set((this.getAttribute('class') || '').split(/\s+/).filter(Boolean));
      for (const c of clsChain.split('.')) if (c && !classes.has(c)) return false;
    }
    return Boolean(tag || id || clsChain);
  }
  _matches(parts) {
    if (!this._matchesSimple(parts[parts.length - 1])) return false;
    let cur = this.parentNode;
    for (let i = parts.length - 2; i >= 0; i--) {
      while (cur && !cur._matchesSimple(parts[i])) cur = cur.parentNode;
      if (!cur) return false;
      cur = cur.parentNode;
    }
    return true;
  }
  querySelectorAll(selector) {
    const groups = String(selector).split(',').map((s) => s.trim().split(/\s+/).filter(Boolean)).filter((g) => g.length > 0);
    const out = [];
    const walk = (node) => {
      for (const ch of node.childNodes) {
        if (ch.nodeType === 1) {
          if (groups.some((g) => ch._matches(g))) out.push(ch);
          walk(ch);
        }
      }
    };
    walk(this);
    return out;
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  _serialize() {
    if (this.nodeType === 3) return escXml(this._text);
    let attrs = '';
    for (const [k, v] of this._attrs.entries()) attrs += ` ${k}="${escAttr(v)}"`;
    if (this.childNodes.length === 0) return `<${this.tagName}${attrs}/>`;
    const inner = this.childNodes.map((c) => c._serialize()).join('');
    return `<${this.tagName}${attrs}>${inner}</${this.tagName}>`;
  }
}

function parseXml(xml) {
  const tokenRe = /<(\/?)([a-zA-Z0-9_:-]+)((?:\s+[a-zA-Z0-9_:-]+(?:\s*=\s*(?:"[^"]*"|'[^']*'|[^\s"'=<>`]+))?)*)\s*(\/?)>|([^<]+)/g;
  const attrRe = /([a-zA-Z0-9_:-]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'=<>`]+)))?/g;
  const rootWrap = new MiniNode('#root');
  const stack = [rootWrap];
  let m;
  while ((m = tokenRe.exec(xml)) !== null) {
    const [, closing, tag, attrStr, selfClose, textChunk] = m;
    const top = stack[stack.length - 1];
    if (textChunk !== undefined) {
      if (textChunk.trim() !== '' || top.tagName === 'text' || top.tagName === 'tspan') {
        const tn = new MiniNode('#text');
        tn._text = unescXml(textChunk);
        top.appendChild(tn);
      }
      continue;
    }
    if (closing) {
      if (stack.length > 1) stack.pop();
      continue;
    }
    const el = new MiniNode(tag);
    if (attrStr) {
      let am;
      attrRe.lastIndex = 0;
      while ((am = attrRe.exec(attrStr)) !== null) {
        const val = am[2] !== undefined ? am[2] : (am[3] !== undefined ? am[3] : (am[4] !== undefined ? am[4] : ''));
        el.setAttribute(am[1], unescXml(val));
      }
    }
    top.appendChild(el);
    if (!selfClose) stack.push(el);
  }
  return rootWrap.children[0];
}

const svgRoot = parseXml(cfg.svg);
const doc = {
  documentElement: svgRoot,
  body: svgRoot,
  getElementById: (id) => (svgRoot.id === id ? svgRoot : svgRoot.querySelector('#' + id)),
  querySelector: (s) => (svgRoot._matchesSimple(s) ? svgRoot : svgRoot.querySelector(s)),
  querySelectorAll: (s) => {
    const res = svgRoot.querySelectorAll(s);
    if (svgRoot._matchesSimple(s)) res.unshift(svgRoot);
    return res;
  },
  createElementNS: (_ns, tag) => new MiniNode(tag),
  createElement: (tag) => new MiniNode(tag),
  getAnimations: () => [],
};
const Module = require('module');
Module._initPaths();
const win = { document: doc, require, Math, Number, String, Array, Object, JSON, console };
global.window = win;
global.document = doc;

const fn = new Function('window', 'document', 'require', cfg.js + '\n;return window.renderFrame;');
const renderFrame = fn(win, doc, require);

(async () => {
  const hdr = Buffer.allocUnsafe(4);
  for (let f = 0; f < cfg.totalFrames; f++) {
    const t = f / cfg.fps;
    if (typeof renderFrame === 'function') {
      await renderFrame(f, t, cfg.durationSec, cfg.fps);
    }
    const xmlBuf = Buffer.from(svgRoot._serialize(), 'utf8');
    hdr.writeUInt32BE(xmlBuf.length, 0);
    process.stdout.write(hdr);
    process.stdout.write(xmlBuf);
  }
})().catch((err) => {
  process.stderr.write(String(err && err.stack ? err.stack : err));
  process.exit(1);
});
"""


async def _try_render_native_v8_resvg(
    *,
    svg_markup: str,
    js_code: str,
    dst: Path,
    width: int,
    height: int,
    fps: float,
    duration_sec: float,
    total_frames: int,
    is_static: bool,
    codec_norm: str,
    ffmpeg: str | None,
) -> dict[str, Any] | None:
    try:
        import resvg_py  # type: ignore[import-untyped]
    except ImportError:
        return None

    sized_svg = _ensure_svg_root_dimensions(svg_markup, width, height)

    # Zero-JS static or constant SVG fast path (no Node subprocess needed!)
    if not js_code.strip():
        png_bytes = await asyncio.to_thread(
            resvg_py.svg_to_bytes, svg_string=sized_svg, width=width, height=height
        )
        if is_static:
            dst.write_bytes(png_bytes)
            return {
                "path": str(dst),
                "bytes": dst.stat().st_size,
                "width": width,
                "height": height,
                "fps": fps,
                "frames": 1,
                "duration_sec": 0.0,
                "format": "png_rgba",
                "backend": "native_v8_resvg",
                "workers": 1,
                "clip": None,
            }
        svg_frames = [sized_svg] * total_frames
        png_frames = [png_bytes] * total_frames
    else:
        node_exe = shutil.which("node")
        if not node_exe:
            return None
        tmp_dir = Path(tempfile.mkdtemp(prefix="davag_v8svg_"))
        try:
            runner_js = tmp_dir / "runner.js"
            cfg_json = tmp_dir / "cfg.json"
            runner_js.write_text(_V8_SVG_BATCH_SCRIPT, encoding="utf-8")
            import json

            cfg_json.write_text(
                json.dumps(
                    {
                        "svg": sized_svg,
                        "js": js_code,
                        "totalFrames": total_frames,
                        "fps": fps,
                        "durationSec": duration_sec,
                    }
                ),
                encoding="utf-8",
            )
            proc = await asyncio.create_subprocess_exec(
                node_exe,
                str(runner_js),
                str(cfg_json),
                env=_build_node_env(),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out_bytes, _err_bytes = await asyncio.wait_for(proc.communicate(), timeout=60.0)
            if proc.returncode != 0:
                return None

            svg_frames = []
            offset = 0
            buf_len = len(out_bytes)
            while offset + 4 <= buf_len:
                flen = struct.unpack_from(">I", out_bytes, offset)[0]
                offset += 4
                if offset + flen > buf_len:
                    return None
                svg_frames.append(out_bytes[offset : offset + flen].decode("utf-8"))
                offset += flen
            if len(svg_frames) != total_frames:
                return None
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

        if is_static:
            png_bytes = await asyncio.to_thread(
                resvg_py.svg_to_bytes, svg_string=svg_frames[0], width=width, height=height
            )
            dst.write_bytes(png_bytes)
            return {
                "path": str(dst),
                "bytes": dst.stat().st_size,
                "width": width,
                "height": height,
                "fps": fps,
                "frames": 1,
                "duration_sec": 0.0,
                "format": "png_rgba",
                "backend": "native_v8_resvg",
                "workers": 1,
                "clip": None,
            }

        workers = min(max(1, os.cpu_count() or 4), 8, total_frames)

        def _raster_all() -> list[bytes]:
            def _one(s: str) -> bytes:
                return bytes(resvg_py.svg_to_bytes(svg_string=s, width=width, height=height))

            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
                return list(pool.map(_one, svg_frames))

        png_frames = await asyncio.to_thread(_raster_all)

    assert ffmpeg is not None
    ff_cmd = _build_ffmpeg_encode_args(
        ffmpeg=ffmpeg,
        dst=dst,
        fps=fps,
        codec_norm=codec_norm,
        width=width,
        height=height,
        raw_rgba_input=False,
        clip_rect=None,
    )
    ff_proc = await asyncio.create_subprocess_exec(
        *ff_cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    assert ff_proc.stdin is not None
    for pb in png_frames:
        ff_proc.stdin.write(pb)
    await ff_proc.stdin.drain()
    ff_proc.stdin.close()
    _, ff_err = await asyncio.wait_for(ff_proc.communicate(), timeout=120.0)
    if ff_proc.returncode != 0:
        raise RuntimeError(f"FFmpeg mux failed in native_v8_resvg: {ff_err.decode('utf-8', errors='replace').strip()}")
    if codec_norm in ("png", "qtrle"):
        await asyncio.to_thread(_ensure_mov_png_rgba_depth32, dst)

    return {
        "path": str(dst),
        "bytes": dst.stat().st_size,
        "width": width,
        "height": height,
        "fps": fps,
        "frames": total_frames,
        "duration_sec": round(total_frames / fps, 3),
        "format": (
            "prores_4444_alpha"
            if codec_norm in ("prores", "prores_4444")
            else ("mov_qtrle_rgba" if codec_norm == "qtrle" else "mov_png_rgba")
        ),
        "backend": "native_v8_resvg",
        "workers": min(max(1, os.cpu_count() or 4), 8, total_frames),
        "clip": None,
    }


def _probe_node_canvas_module() -> tuple[str, str] | None:
    global _NODE_CANVAS_MODULE_CACHE
    if _NODE_CANVAS_MODULE_CACHE is not False:
        return _NODE_CANVAS_MODULE_CACHE  # type: ignore[return-value]
    node_exe = shutil.which("node")
    if not node_exe:
        _NODE_CANVAS_MODULE_CACHE = None
        return None
    node_env = _build_node_env()
    for mod in ("@napi-rs/canvas", "canvas"):
        res = subprocess.run(
            [
                node_exe,
                "-e",
                f"const Module = require('module'); Module._initPaths(); process.stdout.write(require.resolve('{mod}'));",
            ],
            env=node_env,
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        if res.returncode == 0 and res.stdout.strip():
            _NODE_CANVAS_MODULE_CACHE = (mod, res.stdout.strip())
            return _NODE_CANVAS_MODULE_CACHE
    _NODE_CANVAS_MODULE_CACHE = None
    return None


async def _try_render_native_node_canvas(
    *,
    canvas_markup: str,
    js_code: str,
    dst: Path,
    width: int,
    height: int,
    fps: float,
    duration_sec: float,
    total_frames: int,
    is_static: bool,
    codec_norm: str,
    ffmpeg: str | None,
) -> dict[str, Any] | None:
    mod_hit = await asyncio.to_thread(_probe_node_canvas_module)
    node_exe = shutil.which("node")
    if not mod_hit or not node_exe:
        return None
    _mod_name, mod_path = mod_hit

    id_match = re.search(r'\bid\s*=\s*(?:"([^"]+)"|\'([^\']+)\')', canvas_markup, re.IGNORECASE)
    canvas_id = (id_match.group(1) or id_match.group(2)) if id_match else "canvas"

    runner_src = """
const fs = require('fs');
const Module = require('module');
Module._initPaths();
const cfg = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const { createCanvas } = require(cfg.modulePath);
const canvas = createCanvas(cfg.width, cfg.height);
try { canvas.id = cfg.canvasId; } catch {}
try { canvas.style = {}; } catch {}
const ctx = canvas.getContext('2d');
const doc = {
  body: canvas,
  documentElement: canvas,
  getElementById: (id) => (id === cfg.canvasId || !cfg.canvasId ? canvas : null),
  querySelector: (s) => (s === 'canvas' || s === '#' + cfg.canvasId ? canvas : null),
  querySelectorAll: (s) => (s === 'canvas' || s === '#' + cfg.canvasId ? [canvas] : []),
  getAnimations: () => [],
};
const win = { document: doc, require, devicePixelRatio: 1 };
global.window = win;
global.document = doc;
const fn = new Function('window', 'document', 'canvas', 'ctx', 'require', cfg.js + '\\n;return window.renderFrame;');
const renderFrame = fn(win, doc, canvas, ctx, require);
(async () => {
  if (cfg.isStatic) {
    if (typeof renderFrame === 'function') await renderFrame(0, 0, cfg.durationSec, cfg.fps);
    process.stdout.write(canvas.toBuffer('image/png'));
    return;
  }
  for (let f = 0; f < cfg.totalFrames; f++) {
    const t = f / cfg.fps;
    if (typeof renderFrame === 'function') await renderFrame(f, t, cfg.durationSec, cfg.fps);
    const idata = ctx.getImageData(0, 0, cfg.width, cfg.height).data;
    const rgba = Buffer.from(idata.buffer, idata.byteOffset, idata.byteLength);
    if (!process.stdout.write(rgba)) {
      await new Promise((r) => process.stdout.once('drain', r));
    }
  }
})().catch((e) => { process.stderr.write(String(e && e.stack ? e.stack : e)); process.exit(1); });
"""

    import json

    node_env = _build_node_env()
    tmp_dir = Path(tempfile.mkdtemp(prefix="davag_ncanvas_"))
    try:
        r_file = tmp_dir / "canvas_runner.js"
        c_file = tmp_dir / "cfg.json"
        r_file.write_text(runner_src, encoding="utf-8")
        c_file.write_text(
            json.dumps(
                {
                    "modulePath": mod_path,
                    "width": width,
                    "height": height,
                    "fps": fps,
                    "durationSec": duration_sec,
                    "totalFrames": total_frames,
                    "isStatic": is_static,
                    "canvasId": canvas_id,
                    "js": js_code,
                }
            ),
            encoding="utf-8",
        )

        if is_static:
            proc = await asyncio.create_subprocess_exec(
                node_exe,
                str(r_file),
                str(c_file),
                env=node_env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out_png, _ = await asyncio.wait_for(proc.communicate(), timeout=30.0)
            if proc.returncode != 0 or not out_png.startswith(b"\x89PNG"):
                return None
            dst.write_bytes(out_png)
            return {
                "path": str(dst),
                "bytes": dst.stat().st_size,
                "width": width,
                "height": height,
                "fps": fps,
                "frames": 1,
                "duration_sec": 0.0,
                "format": "png_rgba",
                "backend": "native_node_canvas",
                "workers": 1,
                "clip": None,
            }

        assert ffmpeg is not None
        ff_cmd = _build_ffmpeg_encode_args(
            ffmpeg=ffmpeg,
            dst=dst,
            fps=fps,
            codec_norm=codec_norm,
            width=width,
            height=height,
            raw_rgba_input=True,
            clip_rect=None,
        )
        node_proc = await asyncio.create_subprocess_exec(
            node_exe,
            str(r_file),
            str(c_file),
            env=node_env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        ff_proc = await asyncio.create_subprocess_exec(
            *ff_cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        assert node_proc.stdout is not None and ff_proc.stdin is not None
        while True:
            chunk = await node_proc.stdout.read(256 * 1024)
            if not chunk:
                break
            ff_proc.stdin.write(chunk)
            await ff_proc.stdin.drain()
        ff_proc.stdin.close()
        await asyncio.wait_for(node_proc.wait(), timeout=60.0)
        _, ff_err = await asyncio.wait_for(ff_proc.communicate(), timeout=120.0)
        if node_proc.returncode != 0 or ff_proc.returncode != 0:
            if node_proc.returncode != 0:
                return None
            raise RuntimeError(f"FFmpeg encode failed in native_node_canvas: {ff_err.decode('utf-8', errors='replace').strip()}")
        if codec_norm in ("png", "qtrle"):
            await asyncio.to_thread(_ensure_mov_png_rgba_depth32, dst)

        return {
            "path": str(dst),
            "bytes": dst.stat().st_size,
            "width": width,
            "height": height,
            "fps": fps,
            "frames": total_frames,
            "duration_sec": round(total_frames / fps, 3),
            "format": (
                "prores_4444_alpha"
                if codec_norm in ("prores", "prores_4444")
                else ("mov_qtrle_rgba" if codec_norm == "qtrle" else "mov_png_rgba")
            ),
            "backend": "native_node_canvas",
            "workers": 1,
            "clip": None,
        }
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


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
            elif msg.type in (
                aiohttp.WSMsgType.CLOSED,
                aiohttp.WSMsgType.CLOSING,
                aiohttp.WSMsgType.CLOSE,
                aiohttp.WSMsgType.ERROR,
            ):
                raise RuntimeError(
                    f"Headless browser WebSocket closed during {method}: "
                    f"type={msg.type} data={msg.data!r} close_code={self.ws.close_code}"
                )


def _build_auto_clip_probe_js(width: int, height: int, duration_sec: float, fps: float, total_frames: int) -> str:
    return f"""(async () => {{
        const W = {width}, H = {height}, dur = {duration_sec:.6f}, fps = {fps:.4f}, N = {total_frames};
        const bodyStyle = getComputedStyle(document.body);
        const bg = bodyStyle.backgroundColor || '';
        if (bg && bg !== 'rgba(0, 0, 0, 0)' && bg !== 'transparent') return null;
        if (bodyStyle.backgroundImage && bodyStyle.backgroundImage !== 'none') return null;

        const fracs = [0, 0.12, 0.28, 0.5, 0.72, 0.88, 1.0];
        let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
        const skipTags = new Set(['SCRIPT', 'STYLE', 'HEAD', 'META', 'LINK', 'TITLE', 'DEFS', 'CLIPPATH', 'MASK', 'LINEARGRADIENT', 'RADIALGRADIENT', 'STOP', 'FILTER']);

        function extraMargin(cs) {{
            let pad = 6;
            const bs = cs.boxShadow;
            if (bs && bs !== 'none') {{
                const nums = (bs.match(/-?\\d+(?:\\.\\d+)?px/g) || []).map((s) => Math.abs(parseFloat(s)));
                if (nums.length) pad = Math.max(pad, nums.reduce((a, b) => a + b, 0));
            }}
            const flt = cs.filter;
            if (flt && flt !== 'none') {{
                const nums = (flt.match(/-?\\d+(?:\\.\\d+)?px/g) || []).map((s) => Math.abs(parseFloat(s)));
                if (nums.length) pad = Math.max(pad, nums.reduce((a, b) => a + b, 0) * 2 + 8);
            }}
            const sw = parseFloat(cs.strokeWidth || '0') || 0;
            return pad + sw;
        }}

        for (const q of fracs) {{
            const f = Math.min(N - 1, Math.max(0, Math.round(q * (N - 1))));
            const t = f / fps;
            document.getAnimations({{ subtree: true }}).forEach((a) => {{ a.pause(); a.currentTime = t * 1000; }});
            document.querySelectorAll('svg').forEach((s) => {{
                if (typeof s.pauseAnimations === 'function') {{ s.pauseAnimations(); s.setCurrentTime(t); }}
            }});
            if (typeof window.renderFrame === 'function') {{
                await window.renderFrame(f, t, dur, fps);
            }}
            const els = document.body.querySelectorAll('*');
            for (let i = 0; i < els.length; i++) {{
                const el = els[i];
                const tn = el.tagName ? el.tagName.toUpperCase() : '';
                if (skipTags.has(tn)) continue;
                if (tn === 'SVG') continue;
                const cs = getComputedStyle(el);
                if (cs.display === 'none' || cs.visibility === 'hidden' || parseFloat(cs.opacity || '1') <= 0.001) continue;
                const r = el.getBoundingClientRect();
                if (r.width <= 0 && r.height <= 0) continue;
                if (r.width >= W * 0.96 && r.height >= H * 0.96 && (cs.backgroundColor === 'rgba(0, 0, 0, 0)' || cs.backgroundColor === 'transparent') && cs.borderStyle === 'none') continue;
                const m = extraMargin(cs);
                minX = Math.min(minX, r.left - m);
                minY = Math.min(minY, r.top - m);
                maxX = Math.max(maxX, r.right + m);
                maxY = Math.max(maxY, r.bottom + m);
            }}
        }}
        if (!isFinite(minX) || !isFinite(minY) || maxX <= minX || maxY <= minY) return null;
        const padPx = 24;
        let x = Math.max(0, Math.floor(minX - padPx)) & ~1;
        let y = Math.max(0, Math.floor(minY - padPx)) & ~1;
        let right = Math.min(W, Math.ceil(maxX + padPx));
        let bottom = Math.min(H, Math.ceil(maxY + padPx));
        let w = Math.max(16, (right - x + 1) & ~1);
        let h = Math.max(16, (bottom - y + 1) & ~1);
        if (x + w > W) w = (W - x) & ~1;
        if (y + h > H) h = (H - y) & ~1;
        if (w < 16 || h < 16 || (w * h) >= (W * H * 0.72)) return null;
        return {{ x, y, width: w, height: h }};
    }})()"""


async def _open_prepared_cdp_tab(
    http: aiohttp.ClientSession,
    http_base: str,
    full_doc: str,
    width: int,
    height: int,
) -> tuple[str, aiohttp.ClientWebSocketResponse, _CDPSession, str]:
    async with http.put(f"{http_base}/json/new?about:blank") as r_new:
        new_t = await r_new.json()
        target_id = str(new_t["id"])
        page_ws_url = str(new_t["webSocketDebuggerUrl"])

    p_ws = await http.ws_connect(page_ws_url, max_msg_size=64 * 1024 * 1024)
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
    frame_id = str(ft["frameTree"]["frame"]["id"])
    await cdp.call("Page.setDocumentContent", {"frameId": frame_id, "html": full_doc})
    return target_id, p_ws, cdp, frame_id


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
    backend: str = "auto",
    auto_clip: bool = True,
    max_workers: int = 4,
) -> dict[str, Any]:
    """Deterministically render HTML/CSS/SVG/Canvas/JS into a Straight-Alpha `.mov` (`png`/`qtrle`/`prores`) or `.png`."""
    codec_norm = (codec or "png").strip().lower()
    if codec_norm not in SUPPORTED_CODECS:
        raise ValueError(
            f"Unsupported overlay codec '{codec}' (expected 'png', 'qtrle', or 'prores')."
        )
    backend_norm = (backend or "auto").strip().lower()
    if backend_norm not in SUPPORTED_BACKENDS:
        raise ValueError(
            f"Unsupported overlay backend '{backend}' (expected 'auto', 'native', or 'cdp')."
        )

    dst = Path(output_path).resolve()
    is_static = static or duration_sec <= 0 or dst.suffix.lower() == ".png"
    if is_static and dst.suffix.lower() != ".png":
        dst = dst.with_suffix(".png")
    elif not is_static and dst.suffix.lower() != ".mov":
        dst = dst.with_suffix(".mov")
    dst.parent.mkdir(parents=True, exist_ok=True)

    ffmpeg = shutil.which("ffmpeg")
    if not is_static and not ffmpeg:
        raise RuntimeError("ffmpeg is required to mux/encode transparent overlay video.")

    total_frames = 1 if is_static else max(1, round(duration_sec * fps))

    # --- Tier 1: Native V8 + Rust `resvg-py` / `@napi-rs/canvas` fast paths ---
    if backend_norm in ("auto", "native"):
        src_kind, clean_markup, js_code = _classify_overlay_source(html_or_svg)
        if src_kind == "svg":
            native_res = await _try_render_native_v8_resvg(
                svg_markup=clean_markup,
                js_code=js_code,
                dst=dst,
                width=width,
                height=height,
                fps=fps,
                duration_sec=duration_sec,
                total_frames=total_frames,
                is_static=is_static,
                codec_norm=codec_norm,
                ffmpeg=ffmpeg,
            )
            if native_res is not None:
                return native_res
        elif src_kind == "canvas":
            native_res = await _try_render_native_node_canvas(
                canvas_markup=clean_markup,
                js_code=js_code,
                dst=dst,
                width=width,
                height=height,
                fps=fps,
                duration_sec=duration_sec,
                total_frames=total_frames,
                is_static=is_static,
                codec_norm=codec_norm,
                ffmpeg=ffmpeg,
            )
            if native_res is not None:
                return native_res

        if backend_norm == "native":
            raise RuntimeError(
                "backend='native' requested, but overlay is either full HTML/CSS DOM or missing "
                "`resvg-py` / `@napi-rs/canvas` + `node` (use backend='auto' or install `pip install -e '.[motion]'`)."
            )

    # --- Tier 2: Warm Persistent Headless Chrome/Edge + Dynamic `auto_clip` + Multi-Tab `cdp_parallel` ---
    browser_exe = find_headless_browser()
    http_base = await asyncio.to_thread(_ensure_warm_browser_sync, browser_exe)
    full_doc = _wrap_svg_or_html(html_or_svg, width, height)

    eff_workers = (
        1
        if is_static or total_frames < 8 or max_workers <= 1
        else min(max_workers, max(1, total_frames // 6))
    )
    clip_rect: dict[str, int] | None = None
    ff_proc: asyncio.subprocess.Process | None = None
    opened_tabs: list[tuple[str, aiohttp.ClientWebSocketResponse]] = []

    async with aiohttp.ClientSession() as http:
        try:
            t0_id, ws0, cdp0, frame0_id = await _open_prepared_cdp_tab(
                http, http_base, full_doc, width, height
            )
            opened_tabs.append((t0_id, ws0))

            if is_static:
                await cdp0.call(
                    "Runtime.evaluate",
                    {
                        "expression": f"""(() => {{
                            if (typeof window.renderFrame === 'function') return window.renderFrame(0, 0, {duration_sec}, {fps});
                        }})()""",
                        "awaitPromise": True,
                    },
                )
                shot = await cdp0.call("Page.captureScreenshot", {"format": "png", "omitBackground": True})
                dst.write_bytes(base64.b64decode(shot["data"]))
            else:
                if auto_clip and total_frames >= 2:
                    with contextlib.suppress(Exception):
                        probe_res = await cdp0.call(
                            "Runtime.evaluate",
                            {
                                "expression": _build_auto_clip_probe_js(
                                    width, height, duration_sec, fps, total_frames
                                ),
                                "returnByValue": True,
                                "awaitPromise": True,
                            },
                        )
                        cand_clip = (probe_res.get("result") or {}).get("value")
                        if (
                            isinstance(cand_clip, dict)
                            and cand_clip.get("width", 0) >= 16
                            and cand_clip.get("height", 0) >= 16
                        ):
                            clip_rect = {
                                "x": int(cand_clip["x"]),
                                "y": int(cand_clip["y"]),
                                "width": int(cand_clip["width"]),
                                "height": int(cand_clip["height"]),
                            }
                    # Reset tab 0 document cleanly after probe so stateful JS starts fresh at frame 0
                    await cdp0.call("Page.setDocumentContent", {"frameId": frame0_id, "html": full_doc})

                worker_cdps: list[_CDPSession] = [cdp0]
                if eff_workers > 1:
                    extra_tabs = await asyncio.gather(
                        *[
                            _open_prepared_cdp_tab(http, http_base, full_doc, width, height)
                            for _ in range(eff_workers - 1)
                        ]
                    )
                    for tid, tws, tcdp, _fid in extra_tabs:
                        opened_tabs.append((tid, tws))
                        worker_cdps.append(tcdp)

                assert ffmpeg is not None
                ff_cmd = _build_ffmpeg_encode_args(
                    ffmpeg=ffmpeg,
                    dst=dst,
                    fps=fps,
                    codec_norm=codec_norm,
                    width=width,
                    height=height,
                    raw_rgba_input=False,
                    clip_rect=clip_rect,
                )
                ff_proc = await asyncio.create_subprocess_exec(
                    *ff_cmd,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                )
                assert ff_proc.stdin is not None

                loop = asyncio.get_running_loop()
                frame_futs: list[asyncio.Future[bytes]] = [
                    loop.create_future() for _ in range(total_frames)
                ]
                in_flight = asyncio.Semaphore(max(4, eff_workers * 3))
                shot_params: dict[str, Any] = {"format": "png", "omitBackground": True}
                if clip_rect is not None:
                    shot_params["clip"] = {**clip_rect, "scale": 1}

                async def _render_worker(w_idx: int, cdp_s: _CDPSession) -> None:
                    chunk = (total_frames + eff_workers - 1) // eff_workers
                    f_start = w_idx * chunk
                    f_end = min(total_frames, f_start + chunk)
                    try:
                        for f_idx in range(f_start, f_end):
                            await in_flight.acquire()
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
                            await cdp_s.call(
                                "Runtime.evaluate", {"expression": step_js, "awaitPromise": True}
                            )
                            shot = await cdp_s.call("Page.captureScreenshot", shot_params)
                            if not frame_futs[f_idx].done():
                                frame_futs[f_idx].set_result(base64.b64decode(shot["data"]))
                    except Exception as w_err:
                        for rem in range(f_start, f_end):
                            if not frame_futs[rem].done():
                                frame_futs[rem].set_exception(w_err)
                        raise

                async def _pipe_to_ffmpeg() -> None:
                    assert ff_proc is not None and ff_proc.stdin is not None
                    for fut in frame_futs:
                        png_b = await fut
                        in_flight.release()
                        ff_proc.stdin.write(png_b)
                        await ff_proc.stdin.drain()
                    ff_proc.stdin.close()

                await asyncio.gather(
                    _pipe_to_ffmpeg(),
                    *[_render_worker(idx, cdp_w) for idx, cdp_w in enumerate(worker_cdps)],
                )
                _, ff_err_bytes = await asyncio.wait_for(ff_proc.communicate(), timeout=120.0)
                rc = ff_proc.returncode
                ff_proc = None
                if rc != 0:
                    raise RuntimeError(
                        f"FFmpeg ({codec_norm}) encode failed: {ff_err_bytes.decode('utf-8', errors='replace').strip()}"
                    )
                if codec_norm in ("png", "qtrle"):
                    await asyncio.to_thread(_ensure_mov_png_rgba_depth32, dst)
        finally:
            if ff_proc is not None:
                with contextlib.suppress(Exception):
                    if ff_proc.stdin:
                        ff_proc.stdin.close()
                    ff_proc.kill()
            for tid, tws in opened_tabs:
                with contextlib.suppress(Exception):
                    await tws.close()
                with contextlib.suppress(Exception):
                    async with http.get(f"{http_base}/json/close/{tid}") as _:
                        pass

    return {
        "path": str(dst),
        "bytes": dst.stat().st_size,
        "width": width,
        "height": height,
        "fps": fps,
        "frames": total_frames,
        "duration_sec": 0.0 if is_static else round(total_frames / fps, 3),
        "format": (
            "png_rgba"
            if is_static
            else (
                "prores_4444_alpha"
                if codec_norm in ("prores", "prores_4444")
                else ("mov_qtrle_rgba" if codec_norm == "qtrle" else "mov_png_rgba")
            )
        ),
        "backend": "cdp_parallel",
        "workers": eff_workers,
        "clip": clip_rect,
    }