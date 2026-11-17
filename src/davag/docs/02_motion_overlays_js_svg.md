# Code-Based Motion Overlays (Dual-Path V8/Rust & Parallel CDP -> DaVinci Resolve)

`davag` lets the AI agent (or you) author **rich, resolution-independent data visualizations, callouts, lower-thirds, token counters, QR cards, and animated diagrams** in standard HTML, CSS, SVG, Canvas, and JavaScript, and drop them straight onto an unobstructed **Overlay track (`Video 2+`)** over your DaVinci Resolve timeline with **lossless Straight Alpha transparency**.

## Dual-Path Rendering Engine (`backend: "auto" | "native" | "cdp"`)

`motion.render_overlay` and `motion.qr_callout` automatically select the fastest deterministic renderer for each overlay (`backend: "auto"`):

1. **`native_v8_resvg` (Pure `<svg>` + optional JS `window.renderFrame`):**
   - Evaluates all frames in a single lightweight Node.js V8 batch (or zero-subprocess direct path for static `<svg>`) and rasterizes SVG XML across all CPU cores using Rust `resvg` SIMD (`pip install -e ".[motion]"` -> `resvg-py`).
2. **`native_node_canvas` (Pure `<canvas>` + JS `window.renderFrame`):**
   - Evaluates 2D Canvas animations in Node.js V8 via `@napi-rs/canvas` and streams raw RGBA buffers straight into FFmpeg with zero PNG encode/decode intermediate overhead.
3. **`cdp_parallel` (Full HTML / CSS3 / SVG / SMIL / Web Animations fallback — zero extra pip dependencies):**
   - **Persistent Warm Chromium Daemon:** Keeps a headless Chrome/Edge daemon warm across renders so subsequent overlays skip the 600ms browser cold-start penalty.
   - **`<4ms` Dynamic Bounding-Box `auto_clip` + FFmpeg `pad`:** Probes key animation timestamps (`box-shadow`, `filter: drop-shadow/blur`, SVG strokes, CSS transforms) to compute the union active bounding rect (`clip`), reading back up to **10×–40× fewer pixels** from Chromium while FFmpeg `pad` restores the exact transparent `width × height` timeline canvas.
   - **Multi-Tab Worker Pool (`eff_workers`):** Steps frame chunks concurrently across multiple CDP tabs and pipes ordered frames directly into FFmpeg.

## Supported Transparent Codecs (`codec: "png" | "qtrle" | "prores"`) & Straight Alpha

- **`"png"` (Default — QuickTime PNG RGBA `.mov`):** Lossless DEFLATE-compressed 32-bit RGBA `.mov` (~10×–25× smaller than ProRes on sparse UI/callouts). `davag` patches the QuickTime `stsd` sample entry to `depth = 32` (`_ensure_mov_png_rgba_depth32`) and sets `SetClipProperty("Alpha mode", "Straight")` via `mediapool.import` so DaVinci Resolve Free & Studio composite transparency cleanly with zero dark fringes.
- **`"qtrle"` (QuickTime Animation RLE RGBA `.mov`):** Ultra-fast lossless run-length-encoded 32-bit ARGB `.mov` (`depth = 32`), ideal for flat-color motion graphics and lower-thirds.
- **`"prores"` (Apple ProRes 4444 10-bit Alpha `.mov`):** `prores_ks -profile:v 4444 -pix_fmt yuva444p10le`, ideal for heavy gradients, film grain, or external grading pipelines.
- **Static Callout Cards (`"static": true`):** Renders a single 32-bit RGBA `.png`.

## Authoring Contract (`motion.render_overlay`)

Always pass multi-line HTML/SVG/JS in a `--davag:NAME--` payload block (`"html": "@overlay1"`), never as an inline `\n`-escaped JSON string! You can animate using any of:

1. **`window.renderFrame(frame, timeSec, durationSec, fps)`** — Deterministic JS hook called before every frame capture (supports `async` promises).
2. **CSS `@keyframes` / Web Animations API** — `davag` pauses and scrubs `document.getAnimations()` to `timeSec * 1000` ms on every frame (`cdp_parallel`).
3. **SVG SMIL (`<animate>`, `<animateTransform>`)** — `davag` scrubs `svg.setCurrentTime(timeSec)` on every frame (`cdp_parallel`).

### Example `dr_ops` Invocation

```dr_ops
{"ops": [
  {
    "op": "motion.render_overlay",
    "name": "credit_burn_callout",
    "duration_sec": 4.0,
    "codec": "png",
    "record_tc": "01:05:37:28",
    "track_index": 2,
    "html": "@overlay1"
  }
]}
--davag:overlay1--
<div id="badge" style="
  position: absolute; right: 96px; top: 96px;
  padding: 28px 40px; border-radius: 18px;
  background: rgba(14, 18, 28, 0.90);
  border: 2px solid #ff4d6d;
  box-shadow: 0 20px 50px rgba(0,0,0,0.55);
  color: #f8f9fa;
">
  <div style="font-size: 22px; color: #ff8fa3; letter-spacing: 0.08em; text-transform: uppercase;">
    API Credit Burned
  </div>
  <div id="amt" style="font-size: 68px; font-weight: 800; font-variant-numeric: tabular-nums;">
    $0.00
  </div>
</div>
<script>
  window.renderFrame = (frame, t, dur) => {
    const enter = Math.min(1, t / 0.35);
    const ease = 1 - Math.pow(1 - enter, 3);
    const badge = document.getElementById('badge');
    badge.style.transform = `translateY(${(1 - ease) * -40}px) scale(${0.92 + 0.08 * ease})`;
    badge.style.opacity = String(ease);

    const countP = Math.min(1, Math.max(0, (t - 0.2) / 1.8));
    document.getElementById('amt').textContent = `$${(countP * 26.65).toFixed(2)}`;
  };
</script>
--davag:end--
```