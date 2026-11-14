# Code-Based Motion Overlays (JS / CSS / SVG / Canvas -> DaVinci Resolve)

`davag` lets the AI agent (or you) author **rich, resolution-independent data visualizations, callouts, lower-thirds, token counters, and animated diagrams** in standard HTML, CSS, SVG, and JavaScript, and drop them straight onto **`Video 2`** over your DaVinci Resolve timeline with **10-bit straight alpha transparency**.

## Why Deterministic CDP -> ProRes 4444 Alpha?

1. **Zero dropped frames:** Unlike screen-recording a browser in real time, `motion.render_overlay` steps a virtual clock frame-by-frame (`f = 0 .. N-1`) over local Headless Chrome/Edge DevTools Protocol (`aiohttp` WebSocket — zero extra dependencies).
2. **Full Web Graphics Stack:** Every CSS3 glassmorphism filter, custom typography, inline `<svg>` path morph, HTML5 `<canvas>` chart, Web Animations API timeline, or JS function works identically.
3. **Broadcast Codec Native to Resolve:** Frames pipe directly into FFmpeg `prores_ks -profile:v 4444 -pix_fmt yuva444p10le` (`.mov`), preserving crisp anti-aliased RGBA alpha edges, automatically matched to your open Resolve project's resolution (e.g. `3840×2160` 4K) and frame rate (`30fps`).
4. **Native Lottie `.json` Option:** DaVinci Resolve 21.1+ also imports vector Lottie `.json` animations natively via `mediapool.import` + `timeline.append_clips`.

## Authoring Contract (`motion.render_overlay`)

You can animate using **any** of three mechanisms (they can even be combined):

1. **`window.renderFrame(frame, timeSec, durationSec, fps)`** — Deterministic JS hook called before every frame capture (supports `async` promises too!).
2. **CSS `@keyframes` / Web Animations API** — `davag` automatically pauses and scrubs `document.getAnimations()` to `timeSec * 1000` ms on every frame!
3. **SVG SMIL (`<animate>`, `<animateTransform>`)** — `davag` scrubs `svg.setCurrentTime(timeSec)` on every frame!
4. **Static Callout Cards (`"static": true`)** — Renders a single 32-bit RGBA `.png` in `<0.4s` when you don't need motion.

### Example `dr_ops` Invocation

```json
{"ops": [
  {
    "op": "motion.render_overlay",
    "name": "credit_burn_callout",
    "duration_sec": 4.0,
    "record_tc": "01:05:37:28",
    "track_index": 2,
    "html": "@overlay1"
  }
]}
```

With payload `--davag:overlay1--`:

```html
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
```