# davag

An agentic CLI REPL + Web Studio GUI for **DaVinci Resolve 21+** (Studio & Free), built on `davai`'s single-owner `SessionBus` + atomic `dr_ops` batch architecture.

## Highlights

- **Dual DaVinci Resolve Transport:** Zero-click Direct IPC (`fusionscript.dll` on Studio) **and** zero-dependency in-process TCP Bridge (`Workspace → Scripts → Utility → davag_bridge` on Free & Studio) executing the same **41 stdlib-only `davag.resolve_ops`**.
- **Token-Efficient Visual & Audio Intelligence:**
  - **Mosaic Contact Sheets (`media.contact_sheet` / `/sheet`):** `4×4`–`6×6` storyboards with burned-in `HH:MM:SS:FF fNNNN` (~2,050 tokens for 16 frames — **100×–1,000× cheaper than raw video**).
  - **Scout Model Indexer (`media.analyze_clip` / `/index`):** Fast multimodal Scout (`gemini-3.8-flash`) indexes long recordings into persistent `~/.davag/cache/digests/` summaries.
  - **Hardware GPU Proxies (`/proxy` & Resolve Studio Tab):** Intel Arc QuickSync (`av1_qsv`, `h264_qsv`) & NVIDIA NVENC proxies at `360p` / `540p` / `720p` / `1080p`, auto-linked into Resolve and playable right inside the Web GUI.
- **Synced Voiceover Teleprompter & Fairlight ADR (`/vo` & `🎙 prompter` Tab):**
  - Converts timeline markers into a **Fairlight ADR Cue Sheet `.csv`**, an **on-screen Resolve Subtitle track (`ST1: VO Teleprompter`)**, and a **live playhead-synced Web Teleprompter** (works on desktop or phone over LAN!).
- **Deterministic JS / CSS / SVG / Canvas Motion Overlays (`motion.render_overlay`):**
  - Renders code-authored HTML/SVG/Canvas animations via headless Chrome/Edge CDP (`window.renderFrame(f, t, dur, fps)`) into **10-bit ProRes 4444 Straight-Alpha `.mov`** clips and drops them directly onto `Video 2` over your timeline!
- **5-Tier Role Surfaces (`--role`, `/role`):** `reviewer` (read-only) → `logger` → `editor` → `finisher` → `master`.
- **YouTube Delivery & OpenRouter Flux Thumbnails (`/thumbnail`, `/youtube-stage`, `youtube.upload`):**
  - Multimodal 16:9 thumbnail generation (`black-forest-labs/flux.2-pro` / `flux-3` via OpenRouter with live Resolve viewer reference frames), auto-formatted `00:00` YouTube Chapters from markers, `.srt` captions, Deliver queue, and gated `private`-draft YouTube Data API v3 upload.

## Quickstart

```powershell
python -m venv .venv
.venv\Scripts\pip install -e ".[dev]"

# Verify Resolve 21+, Chrome/Edge motion renderer, QSV/NVENC encoders, and API keys:
.venv\Scripts\davag --doctor

# Launch TUI REPL + LAN Web Studio GUI (Ctrl+Click tokenized URL in banner):
.venv\Scripts\davag --host 0.0.0.0 --port 7879
```

## Key Commands

| Command | Description |
|---|---|
| `/resolve [direct\|bridge]` | Check or switch live DaVinci Resolve connection |
| `/role [name]` | Switch capability surface (`reviewer`, `logger`, `editor`, `finisher`, `master`) |
| `/sheet <clip> [start dur]` | Build burned-in-TC mosaic PNG & attach to vision context |
| `/index <clip> [--force]` | Run Scout LLM (`gemini-3.8-flash`) & cache Clip Digest |
| `/proxy <clip> [360\|540\|720\|1080]` | Build Intel Arc QSV / NVENC proxy & link in Resolve |
| `/transcribe <clip> [s dur]` | Extract frame-mapped speech transcript & attach |
| `/vo [prefix]` | Build Fairlight ADR `.csv` + import on-screen `.srt` VO Teleprompter onto `ST1` |
| `/thumbnail <prompt>` | Generate 16:9 YouTube thumbnail via OpenRouter Flux (+ current viewer frame) |
| `/youtube-stage <title>` | Build `youtube_staging/<slug>/` (chapters, `.srt`, `DESCRIPTION.md`, render job) |
| `/undo` | Restore active Resolve timeline from previous `.davag-snap-*` checkpoint |
| `/model <id> [--save]` | Switch live between `gemini-4-carbon`, `opus`, `sonnet`, `flash`, `gpt`, or OpenRouter |