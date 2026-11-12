# davag

An agentic CLI REPL + Web GUI harness for **DaVinci Resolve** (Studio & Free).

Built on `davai`'s single-owner `SessionBus` + protocol-batch design (`dr_ops`), with:
- **Dual DaVinci Resolve Transport:** Zero-click external IPC (`fusionscript.dll` on Studio) **and** a zero-dependency internal TCP bridge (`Workspace → Scripts → Utility → davag_bridge` on Free & Studio) running the exact same `davag.resolve_ops` suite.
- **Multi-Provider + Native Video:**
  - **Anthropic** (`claude-opus-5`, `claude-opus-5-5`, `claude-sonnet-5`) — adaptive thinking, effort, ephemeral prefix caching.
  - **Google Gemini** (`gemini-3-pro-preview`, `gemini-2.5-flash`) — watches video natively via inline bytes (`<=18 MB`), **Gemini Files API** (`>18 MB` up to `2 GB`), and YouTube URLs.
  - **OpenAI** (`gpt-5.1` on Responses API w/ reasoning summaries + `DAVAG_OPENAI_SURFACE=chat` for local/OpenAI-compatible endpoints) & **OpenRouter** (live catalog & pricing).
- **Hardware-Accelerated Media Intelligence:** Auto-detects **Intel Arc QuickSync (`av1_qsv`, `h264_qsv`)** and **NVIDIA NVENC** for instant proxy transcodes (`/watch` / `media.proxy_clip`), frame-mapped silence removal (`media.detect_silence`), cached word-to-frame transcription (`media.transcribe`), and pure-Python `.otio` / `.fcpxml` timeline generation (`otio.build`).
- **Safe Editing & Instant `/undo`:** Mutating timeline batches automatically snapshot the active timeline (`.davag-snap-*`) before applying edits.

## Quickstart

```powershell
python -m venv .venv
.venv\Scripts\pip install -e ".[dev]"

# Verify Resolve connection, GPU hardware encoders, FFmpeg, and API keys:
.venv\Scripts\davag --doctor

# Optional (for DaVinci Resolve Free, or in-process Bridge mode):
.venv\Scripts\davag --install-bridge
```

## Usage

```powershell
davag                                    # Interactive TUI REPL
davag --web                              # TUI REPL + Web GUI on http://127.0.0.1:7879
davag --host 0.0.0.0 --port 7879         # TUI REPL + LAN Web GUI (prints bearer token)
davag -p "inspect project and timelines" # One-shot headless plain text
davag --json "list media pool clips"     # One-shot NDJSON event stream
davag --resume                           # Resume most recent session
```

## Key REPL & Web Commands

| Command | Description |
|---|---|
| `/resolve [direct\|bridge]` | Show live Resolve connection status or switch transport |
| `/grab [tc]` | Export current Resolve viewer frame & attach PNG to context |
| `/watch <file> [start dur]` | Hardware-transcode proxy clip & attach for Gemini video review |
| `/undo` | Restore active Resolve timeline from previous `.davag-snap` checkpoint |
| `/model <id> [--save]` | Switch live between `opus`, `sonnet`, `gemini`, `flash`, `gpt`, or OpenRouter slugs |
| `/keys` / `/env <block>` | Manage API keys & `#---#` `.env` provider blocks (`~/.davag/.env`, with automatic read-only key fallback to `~/.davai/.env`) |
| `/context` / `/compact` | Inspect addressable context segments or compact |
| `/tps` / `/artifacts` | View tok/s throughput or save emitted `.py`/`.otio`/`.fcpxml` artifacts |