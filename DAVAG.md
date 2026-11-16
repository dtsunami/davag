# davag — Project Grounding & Architectural Decisions

DaVinci Resolve AI editing agent & custom harness (TUI REPL + Web Sidecar GUI).

## Architectural Invariants

1. **Zero third-party dependencies inside Resolve:**
   `src/davag/resolve_ops/` and `src/davag/bridge/` MUST import Python standard library modules only. They execute both inside the external `.venv` (`DirectTransport`) and inside DaVinci Resolve's embedded `fuscript` Python host (`BridgeTransport`).
2. **Single-owner state, dual views:**
   `agent/`, `context/`, `providers/`, `session/`, and `server/` remain TUI-free. Both the terminal REPL and the `--web` / `--host` SSE browser GUI drive the session via `session/dispatch.py` and subscribe via `session/bus.py`.
3. **5-Tier Capability Roles (`--role`, `DAVAG_ROLE`, `/role`):**
   `reviewer` (strictly read-only) → `logger` (markers, VO scripts, proxies) → `editor` (cuts, transitions, JS/SVG motion overlays, audio master) → `finisher` (Color, Fusion, Deliver, YouTube staging & Flux thumbnails) → `master` (adds approval-gated `resolve.eval`, `shell`, `youtube.upload`, and `destructive` ops). Enforced in both `protocol.md` and `executor.plan_batch`.
4. **Deterministic Code-Based Motion Overlays (`motion.render_overlay`, `motion.qr_callout`):**
   Self-contained HTML/CSS/SVG/Canvas/JS is rendered frame-deterministically via local Headless Chrome/Edge CDP (`window.renderFrame(f, t, dur, fps)` + Web Animations / SMIL scrubbing) with transparent RGBA background into lightweight **QuickTime PNG Straight-Alpha `.mov`** (`-c:v copy`, zero re-encode, ~10–25× smaller on sparse UI; optional `codec: "prores"` for 10-bit ProRes 4444; or RGBA `.png` for static callouts), imported into Resolve's `Overlays` bin, and placed onto an unobstructed Overlay track (`Video 2+`) at `record_tc`.
   - *Planned Future Optimization (Bounding-Box Auto-Crop):* Compute tight DOM/shadow bounding box (`x, y, width, height`, padded & rounded to even px), pass `clip` to `Page.captureScreenshot`, and position the cropped `.mov`/`.png` on Resolve's canvas via `timeline.set_clip_props` (`Scaling`, `Pan`, `Tilt`) for another ~5–10× size and render-speed win.
5. **Recorded Resolve 21.2 API Capability & Boundary Decisions (Nov 2026):**
   - **Covered natively (`46` structured Resolve ops):** In-place trim/slip/move/gap-delete/silence-ripple (`21.1.2+`), transitions & fades (`21.1+`), constant speed/stabilize/SmartReframe, Color LUT/CDL/`GetCDL`/`ApplyAutoColor`/`ApplyGradeFromDRX` (`21.2`), Fairlight LUFS normalization/VoiceIsolation/DialogueLeveler/`GenerateSpeech`, Fusion `Text+` inputs, Subtitle neural generation + `.srt` teleprompter + Fairlight ADR `.csv` cues.
   - **Accepted Blackmagic Scripting API Gaps (by design):**
     1. *Interactive Color Wheels / Curves / Qualifiers:* Not exposed by Resolve's scripting API. **Decision:** Author or apply looks via `.cube` LUTs (`color.apply_lut`), ASC-CDL (`color.set_cdl`), Gallery `.drx` PowerGrade stills (`color.grade`), or Resolve 21.2 `ApplyAutoColor`.
     2. *Nonlinear Speed-Ramp Bezier Curves:* Resolve exposes constant `SetSpeed` (`timeline.set_speed`), not keyframed variable speed curves. **Decision:** Apply constant retimes via `timeline.set_speed` or build motion-graphic timecurves in `motion.render_overlay`.
     3. *Granular Fairlight Parametric EQ Curves:* Not individually scriptable per band. **Decision:** Use `media.master_vo` (FFmpeg 80Hz HPF + `afftdn` denoise + compressor + `-14 LUFS` EBU R128 WAV), `audio.enhance` (`VoiceIsolation`, `DialogueLeveler`, `-14 LKFS` normalization), or `Project.ApplyFairlightPresetToCurrentTimeline`.