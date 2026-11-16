# `davag` — DaVinci Resolve AI Editing Agent & Harness Plan

> **Historical Architecture Spec:** This document records the initial Phase 1–5 design plan. For current production invariants, exact `dr_ops` / slash-command catalogs, FastAPI + Unified `🎬 timeline` Studio architecture, QuickTime PNG overlays, and Resolve 21.2 parity, see **`DAVAG.md`**, **`README.md`**, and **`src/davag/docs/`**.

## 1. Vision & Scope

`davag` ("DaVinci Agent") is a Python 3.11+ agentic CLI REPL + Web GUI sidecar modeled directly on `davai`'s architecture (`D:\davag\davai`), purpose-built for **DaVinci Resolve** (Studio & Free).

### Core Architectural Pillars
1. **`davai`-grade Harness UX (TUI REPL + Web Sidecar GUI):**
   * Clean pip-installable CLI entrypoint (`python -m venv .venv` + `pip install -e .` → `davag`, `davag --web`, `davag --host 0.0.0.0`, `davag "rough cut interview"`, `davag --resume`).
   * **Single-owner state, dual renderers:** The harness owns all session state (`Agent`, `Ledger`, `SessionBus`, `Dispatcher`). Both the terminal TUI (`prompt_toolkit` + `rich`) and the browser GUI (`--web` / `--host` / `--port`, SSE downstream `/api/events` + `Last-Event-ID` replay, POST upstream, bearer token auth) attach as views over the exact same `SessionBus` and `Dispatcher`—including raced approval resolution, live steering, `/context` segment table, `/tps` throughput telemetry, `/model`, `/keys`, and `/env` profile switching.
2. **Protocol-First Execution (`dr_ops` fenced batches, no brittle framework):**
   * Like `davai`'s `da_ops`, the LLM emits at most one fenced ```` ```dr_ops ```` block per turn with `@NAME` out-of-band verbatim payloads (`--davag:NAME-- ... --davag:end--`), plus native tool-call salvage if a model emits tool calls anyway.
   * **Two-phase Plan → Apply execution** with **timeline checkpointing** (`Timeline.DuplicateTimeline` snapshots instead of file copies, since Resolve mutations happen in Resolve's project DB) and segregated human approval for `resolve.eval` / `shell` / destructive deletes.
3. **Dual DaVinci Resolve Transport (External Direct + Internal Bridge):**
   * **Direct Mode (Studio):** External process loads `fusionscript.dll` / `DaVinciResolveScript` directly over Blackmagic's IPC. Zero clicks inside Resolve.
   * **Bridge Mode (Free & Studio):** Lightweight zero-dependency Python stdlib script (`Workspace → Scripts → davag_bridge`) opens a tiny `UIManager` keep-alive window inside Resolve and dispatches JSON-RPC calls over `127.0.0.1:18765` onto Resolve's main script thread.
   * **100% shared Resolve logic:** `src/davag/resolve_ops/` uses **Python stdlib only** and runs identically in-process (Direct) or inside Resolve's `fuscript.exe` (Bridge).
4. **First-Class Multi-Provider + Video/Vision Grounding:**
   * **Anthropic** (`claude-opus-5`, `claude-opus-5-5`, `claude-sonnet-5`) — primary reasoning/editing brain with adaptive thinking, `output_config.effort`, and ephemeral prompt prefix caching.
   * **Google Gemini** (`gemini-3-pro-preview`, `gemini-2.5-flash`) — native **video** (`inlineData` <=18MB **plus Gemini Files API upload-and-poll** for larger proxy renders/clips, and YouTube `fileData.fileUri`) + audio + vision so the agent can literally *watch* raw footage or a rendered timeline cut.
   * **OpenAI** (`gpt-5.1`, `gpt-5-mini` via Responses API by default for reasoning summaries, plus Chat Completions fallback `DAVAG_OPENAI_SURFACE=chat` for any OpenAI-compatible local/cloud endpoint).
   * **OpenRouter** (`OPENROUTER_API_KEY`, dynamic `/api/v1/models` catalog + pricing merge, `reasoning: {effort}`, `HTTP-Referer`/`X-Title`).

---

## 2. Locked Design Decisions

| Area | Decision | Rationale |
|---|---|---|
| **Language & Packaging** | Python 3.11+, `venv` + `pip install -e ".[dev]"`, `pyproject.toml` (`[project.scripts] davag = "davag.cli:main"`) | Matches Resolve's Python 3 ecosystem + ML/media tooling while giving a zero-friction `davag` CLI entrypoint. |
| **Action Protocol** | Fenced `dr_ops` JSON envelope + `--davag:NAME--` payload blocks (mirrors `da_ops`) | Provider-agnostic across Anthropic, Gemini, OpenAI Responses, OpenRouter, and local models; preserves verbatim Python/Lua/FCPXML/OTIO payloads without JSON-escaping bugs; supports atomic multi-op batching. |
| **Resolve Boundary** | Serializable domain ops (`dict` → `dict`) in stdlib-only `davag.resolve_ops` | `PyRemoteObject` C++ proxies cannot cross sockets. Sharing `resolve_ops` gives 100% Free/Studio parity and eliminates chatty per-item IPC overhead. |
| **Transport Auto-Order** | `auto` probes `BridgeTransport` (`127.0.0.1:18765`) first, then `DirectTransport` (`fusionscript.dll`), configurable via `DAVAG_TRANSPORT=auto\|direct\|bridge` | If the user explicitly started the internal bridge, honor it immediately (also faster for 1,000-clip loops); otherwise connect zero-click via Studio IPC; if Studio returns `None` (Resolve Free), print exact 1-click Bridge instructions. |
| **Undo / Rollback Model** | Project/Timeline Checkpoints (`Timeline.DuplicateTimeline`) before mutating batches + filesystem `Journal` for disk files | Resolve's C++ scripting API bypasses or fragments Ctrl+Z undo history. Duplicating the active timeline to `.davag-snap-<turn>` before mutating ops gives instant `/undo` in Resolve. |
| **Large Video Input** | Inline `inlineData` (<18MB) + **FFmpeg low-bitrate proxy transcode & Gemini Files API** (>18MB) | Raw camera clips in Resolve are gigabytes, not <18MB. `davag` will auto-render a lightweight 480p/1fps or 720p AAC proxy clip (or timeline range) and upload via Gemini Files API (`client.files.upload` + active poll) when needed. |
| **Web Sidecar Security** | Same rules as `davai`: `--host` or `--port` implies `--web`; bearer token required for `/api/*`; `--host 0.0.0.0` + `--yolo` is **refused** | Prevents exposing an unauthenticated `shell` / `resolve.eval` RCE surface on the LAN. |

---

## 3. System Architecture

```text
┌──────────────────────────────────────────────────────────────────────────────┐
│ External Process: `davag` (.venv, Python 3.11+, asyncio)                     │
│                                                                              │
│  ┌─────────────────────┐        ┌─────────────────────────────────────────┐  │
│  │ Terminal TUI REPL   │        │ HTTP + SSE Web Sidecar (`--web`/`--host`)│  │
│  │ (prompt_toolkit +   │        │  • GET /api/events (SSE + Last-Event-ID)│  │
│  │  rich live panes)   │        │  • Viewer still preview + video player  │  │
│  └──────────┬──────────┘        │  • uPlot perf, context, models, keys    │  │
│             │                   └────────────────────┬────────────────────┘  │
│             └───────────────────┬────────────────────┘                       │
│                                 ▼                                            │
│                   Dispatcher & SessionBus (single state owner)               │
│                                 │                                            │
│                                 ▼                                            │
│            Agent Loop (dr_ops parser, steers, brakes, compaction)            │
│               ├── Providers: Anthropic | Gemini | OpenAI | OpenRouter        │
│               ├── Local Ops: read/write/replace, FFmpeg, Whisper, OTIO       │
│               └── Resolve Ops Executor (plan -> checkpoint -> apply)         │
│                                 │                                            │
│                         ResolveTransport                                     │
│                    ┌────────────┴────────────┐                               │
│                    ▼                         ▼                               │
│             DirectTransport            BridgeTransport                       │
│         (fusionscript.dll IPC)     (TCP 127.0.0.1:18765 + token)             │
└────────────────────┼─────────────────────────┼───────────────────────────────┘
                     │                         │
                     ▼ (Studio only)           ▼ (Free & Studio)
┌──────────────────────────────────┐  ┌────────────────────────────────────────┐
│ DaVinci Resolve.exe              │  │ fuscript.exe (Workspace -> Scripts)    │
│ C++ Scripting Host               │◄─┤ davag_bridge.py (stdlib only)          │
│                                  │  │  • UIManager status dialog (keepalive) │
│                                  │  │  • Main-thread queue drain via timer   │
│                                  │  │  • Imports `davag.resolve_ops`         │
└──────────────────────────────────┘  └────────────────────────────────────────┘
```

---

## 4. Package & Directory Structure

```text
D:\davag\
├── pyproject.toml                   # Hatchling/setuptools editable build, `davag` script
├── README.md                        # Operator documentation & quickstart
├── DAVAG.md                         # Project standing instructions & architectural invariants
├── plan.md                          # This implementation plan
├── .daignore / .gitignore
├── scripts\
│   ├── install_bridge.py            # Installs launcher into Resolve's Scripts\Utility dir
│   └── davag_bridge.py              # Standalone launcher placed in Resolve Scripts menu
├── src\
│   └── davag\
│       ├── __init__.py              # __version__
│       ├── cli.py                   # parse_args(), UsageError, headless vs TUI + Web startup
│       ├── headless.py              # --print, --json, piped stdin execution
│       ├── config\
│       │   ├── env.py               # Precedence: settings.json < ~/.davag/.env < ./.env < env < CLI
│       │   ├── envfile.py           # Atomic comment-preserving .env editor (CRLF-safe)
│       │   ├── profiles.py          # `#---# name` ... `#-end-provider-#` block switcher
│       │   ├── models.py            # Seed table (Opus 5, Gemini 3 Pro, GPT-5.1, OpenRouter) + dynamic catalog
│       │   └── resolve_paths.py     # OS-aware discovery for fusionscript.dll, Scripting API, Scripts dir
│       ├── providers\
│       │   ├── base.py              # Canonical StreamEvent (`text`, `thinking`, `usage`, `stop`), cost math
│       │   ├── index.py             # Factory, key masking, auth & surface diagnostics (`sk-or-` detection)
│       │   ├── anthropic.py         # AsyncAnthropic, adaptive thinking, output_config.effort, ephemeral cache
│       │   ├── gemini.py            # google-genai async, inlineData + Files API upload/poll + YouTube fileUri
│       │   ├── openai_chat.py       # Chat Completions (OpenRouter, custom base_url, Grok, local vLLM/Ollama)
│       │   └── openai_responses.py  # OpenAI Responses API (reasoning summaries, store=False)
│       ├── context\
│       │   ├── ledger.py            # Typed addressable segments (`grounding`, `user`, `op-result`, `media`, etc.)
│       │   ├── tokens.py            # Heuristic + provider countTokens calibration + video duration billing
│       │   ├── compact.py           # 75% auto-compaction (evict stale state dumps/stills first, then summarize)
│       │   ├── media.py             # @image, @video, /youtube, Resolve still-frame & proxy-clip attachments
│       │   ├── pastes.py            # [[paste#N: K lines]] folding & expansion
│       │   └── artifacts.py         # Captures emitted scripts/EDLs/OTIO/Fuses as saveable artifacts
│       ├── resolve_ops\             # *** ZERO THIRD-PARTY DEPS (runs in .venv AND Resolve fuscript) ***
│       │   ├── __init__.py
│       │   ├── schema.py            # @resolve_op decorator, param schemas, read/mutate/destructive tiering
│       │   ├── helpers.py           # Lua {1:x} dict->list normalizer, timecode<->frame math, Rational FPS
│       │   ├── session_ops.py       # ping, get_info, open_page, project load/save/list
│       │   ├── mediapool_ops.py     # bin tree, clip metadata, import media, create/move bins, clip markers
│       │   ├── timeline_ops.py      # get_state, playhead get/set, create/duplicate, append subclips, properties
│       │   ├── marker_ops.py        # timeline/clip markers (add, filter by color, delete, customData)
│       │   ├── color_fusion_ops.py  # grab_still, LUT/CDL apply, node list, add Fusion comp/text+/generator
│       │   ├── render_ops.py        # presets, render range (for Gemini video review!), job queue status
│       │   └── eval_op.py           # `resolve.eval` gated escape hatch (executes Python against resolve/fusion)
│       ├── bridge\                  # *** ZERO THIRD-PARTY DEPS (runs inside Resolve) ***
│       │   ├── protocol.py          # Newline-delimited JSON-RPC framing + shared secret handshake
│       │   └── server.py            # Non-blocking localhost socket + queue + UIManager window & timer pump
│       ├── transport\
│       │   ├── base.py              # ResolveTransport ABC, auto-detection, connection state & reconnect
│       │   ├── direct.py            # Loads fusionscript.dll + DaVinciResolveScript in threadpool executor
│       │   └── bridge_client.py     # Async TCP client to bridge/server.py
│       ├── media_intel\             # External-only media analysis tools (.venv)
│       │   ├── ffmpeg.py            # FFprobe metadata, silence/scene detection, 480p proxy clip extraction
│       │   ├── transcribe.py        # faster-whisper / OpenAI / Gemini audio transcription w/ word timestamps
│       │   └── otio_builder.py      # FCPXML / OTIO timeline assembly for complex ripple/razor edits
│       ├── agent\
│       │   ├── parser.py            # `dr_ops` fenced block + `--davag:NAME--` payload parser + XML-leak recovery
│       │   ├── prompt.py            # 6 layered prompts (built-in < ~/.davag/prompts < ./.prompts) + fork hash check
│       │   ├── grounding.py         # Static cacheable prefix + live Resolve project/timeline snapshot segment
│       │   ├── executor.py          # Plan -> Checkpoint (DuplicateTimeline / file Journal) -> Apply -> Gate
│       │   ├── journal.py           # File undo snapshots + Resolve checkpoint registry (`/undo`)
│       │   └── loop.py              # Turn streaming, repetition brake, effort demotion, steers, EVENTS list
│       ├── session\
│       │   ├── bootstrap.py         # Wires Config, Transport, Ledger, Agent, SessionBus
│       │   ├── bus.py               # Sequenced replay ring, live turn projection, raced approval broker
│       │   ├── dispatch.py          # Unified submit path for TUI & Web (steers, /commands, @media, pastes)
│       │   ├── log.py               # ~/.davag/sessions/<ts>-<id>/{meta.json, transcript.jsonl, journal.jsonl}
│       │   ├── resume.py            # Reconstructs ledger from transcript.jsonl
│       │   └── perf.py              # tok/s buckets, TTFT, contextIn vs rate telemetry
│       ├── server\
│       │   ├── auth.py              # Bearer token generation, constant-time check, assert_safe_bind()
│       │   └── http.py              # aiohttp / asyncio HTTP + SSE server (`/api/events`, `/api/resolve/*`, media)
│       └── ui\
│           ├── commands.py          # Single source of truth HELP -> command_list() for TUI + Web completion
│           ├── tui\                 # Terminal UI (prompt_toolkit input + rich transcript, status bar, panes)
│           │   ├── app.py
│           │   └── panes.py         # Approval, Model, Keys, Env, Context, Perf, Resolve Status
│           └── web\                 # Zero-build static SPA (ported & extended from davai/src/ui/web/)
│               ├── index.html       # Tabs: chat | resolve | perf | context | models | artifacts
│               ├── app.js           # SSE client, approval modal, viewer frame preview, uPlot charts
│               └── app.css
└── tests\
    ├── fake_resolve.py              # In-memory mock of Resolve/Project/MediaPool/Timeline/UIManager API
    ├── test_cli.py
    ├── test_config_profiles.py
    ├── test_parser.py
    ├── test_providers.py
    ├── test_resolve_ops.py          # Runs full resolve_ops suite against FakeResolve (both Direct & Bridge TCP!)
    ├── test_bridge_rpc.py
    ├── test_executor_checkpoints.py
    ├── test_events_contract.py      # Static check: every emitted event in EVENTS is consumed by TUI & Headless & Web
    └── test_server_sse.py
```

---

## 5. Detailed Subsystem Specifications

### 5.1 CLI Entrypoint & Configuration (`davag`)
Installed via `pip install -e .` into `.venv\Scripts\davag.exe`:

```bash
davag                                   # Interactive TUI REPL (connects to Resolve if open)
davag "cut dead air and add markers"    # One-shot prompt in TUI (or headless if piped)
davag --web                             # TUI REPL + Web GUI on http://127.0.0.1:7879
davag --host 0.0.0.0 --port 7879        # Implies --web; binds LAN interface w/ bearer token
davag -p "list all timelines"           # Headless plain text (--print)
davag --json "inspect current timeline" # Headless NDJSON event stream
davag --transport bridge                # Force bridge | direct | auto
davag --install-bridge                  # Installs davag_bridge.py into Resolve's Scripts menu
davag --doctor                          # Checks Resolve install, fusionscript.dll, FFmpeg, API keys
davag --models [provider]               # Queries live provider model list
davag --sessions / --resume [id]        # Session listing & transcript-based resume
davag --yolo                            # Auto-approve shell/eval/destructive ops (refused with non-loopback --host)
```

**Home Directory (`$DAVAG_HOME`, default `~/.davag/`) & `.env` Precedence:**
* Separate `~/.davag/` home directory (`settings.json`, `.env`, `history.jsonl`, `bridge.json`, `prompts/`, `cache/`, `sessions/<timestamp>-<id>/{meta.json, transcript.jsonl, journal.jsonl, snaps/}`) so sessions and undo snapshots never collide with `davai`.
* **Precedence (lowest → highest):**
  `~/.davag/settings.json` → `~/.davai/.env` *(read-only key fallback so existing `davai` keys work out of the box, with origin tracked in `cfg.key_origins`)* → `~/.davag/.env` → `./.env` *(with `HOME_ONLY_VARS` blocking `DAVAG_BASE_URL` / `OPENAI_BASE_URL` in project `.env`)* → OS environment → CLI flags → live session overrides.
* All writes from `/keys` and `/env` target `~/.davag/.env` exclusively via an atomic, comment-preserving, CRLF-safe editor.
* Supports `#---# profile_name` ... `#-end-provider-#` comment-toggled blocks in `.env` (`##` prose comments preserved, CRLF round-trip exact on Windows).
* API keys resolve `DAVAG_<PROVIDER>_API_KEY` first, then standard `ANTHROPIC_API_KEY`, `GEMINI_API_KEY` / `GOOGLE_API_KEY`, `OPENAI_API_KEY`, `OPENROUTER_API_KEY`, `XAI_API_KEY`, then `DAVAI_<PROVIDER>_API_KEY`.

---

### 5.2 Multi-Provider Layer & Video/Multimodal Pipeline

| Provider | Adapter | Key Capabilities & Implementation Details |
|---|---|---|
| **Anthropic** | `anthropic` SDK (`AsyncAnthropic`) | Seed models: `claude-opus-5-5`, `claude-opus-5` (default reasoning/editing model), `claude-sonnet-5`, `claude-haiku-4-5`. Uses `thinking={"type": "adaptive", "display": "summarized"}`, `output_config={"effort": effort}`, and `cache_control={"type": "ephemeral"}` on system prompt + rolling history breakpoints. Pre-flight `messages.count_tokens`. Rejects raw `video` parts cleanly with actionable message ("switch `/model gemini-3-pro-preview` or extract keyframes/transcript"). |
| **Gemini** | `google-genai` SDK (`genai.Client().aio`) | Seed models: `gemini-3-pro-preview`, `gemini-2.5-flash`. `video=True`, `vision=True`. Supports `thinking_config={"include_thoughts": True}`. **Three video ingestion paths:**<br>1. `inlineData` for clips <= 18 MB.<br>2. **Gemini Files API** (`aio.files.upload` + poll until ` state == "ACTIVE"`, auto-cleanup on session close or TTL) for clips/renders > 18 MB up to 2 GB.<br>3. **YouTube URLs** via `fileData(fileUri=url)` (no upload, `/youtube <url> [minutes]`). |
| **OpenAI** | `openai` SDK (`AsyncOpenAI`) | Seed models: `gpt-5.1`, `gpt-5-mini`. Defaults to **Responses API** (`client.responses.create`, `store=False`, `reasoning={"effort": ..., "summary": "auto"}`) so reasoning summaries stream to the Thinking pane. `DAVAG_OPENAI_SURFACE=chat` switches to Chat Completions for third-party OpenAI-compatible servers (vLLM, Ollama, LM Studio, Mistral, Grok). |
| **OpenRouter** | `openai` SDK (`AsyncOpenAI` w/ `https://openrouter.ai/api/v1`) | Dynamic catalog & pricing fetch from `GET /api/v1/models` merged over curated seeds. Sends `reasoning={"effort": ...}` (never top-level `reasoning_effort`), strips on 400 with dual-error detail, sets `HTTP-Referer` & `X-Title: davag`. Forces `video=False` (Chat Completions wire format has no video slot). |

**Canonical Usage Normalization:**
Every provider emits `{t: "usage", in_fresh, out, cache_read, cache_write, context_in}` so cached prompt tokens are never double-billed and `/tps` plots against true `context_in`.

---

### 5.3 The `dr_ops` Protocol & Operation Catalog

The model emits at most one `dr_ops` fenced block per turn:

````markdown
```dr_ops
{"ops": [
  {"op": "timeline.get_state", "include_clips": true},
  {"op": "timeline.grab_still", "attach_to_context": true}
]}
```
````

For multi-line scripts, FCPXML/OTIO payloads, Fusion Lua expressions, or file edits, string values use `@NAME` payload references:

````markdown
```dr_ops
{"ops": [
  {"op": "resolve.eval", "description": "Add Text+ title on V2", "code": "@1"}
]}
--davag:1--
tl = project.GetCurrentTimeline()
item = tl.InsertFusionTitleIntoTimeline("Text+")
print(item.GetName())
--davag:end--
```
````

#### Complete `dr_ops` Surface

**1. Session & Navigation (Read / Safe)**
* `resolve.status` — connection mode (`direct` vs `bridge`), Resolve version, edition (`Studio` vs `Free`), current page, current project & timeline summary.
* `resolve.open_page` — switch UI to `"media" | "cut" | "edit" | "fusion" | "color" | "fairlight" | "deliver"`.
* `project.list` / `project.open` / `project.save` — list or switch Resolve projects.

**2. Media Pool & Bins (Read / Mutate)**
* `mediapool.tree` — recursive folder/bin hierarchy with clip counts.
* `mediapool.clips` — clip properties (`clip_id` = `GetUniqueId()`, FPS, start/end TC, frames, resolution, codec, audio channels, file path, usage count).
* `mediapool.import` — import file/folder paths into target bin.
* `mediapool.create_bin` / `mediapool.move_clips` — organize bins.

**3. Timeline Inspection & Visual/Video Grounding (Read / Safe)**
* `timeline.list` / `timeline.select` — list timelines or switch active timeline.
* `timeline.get_state` — tracks (`video`, `audio`, `subtitle`), track lock/mute/solo state, and every `TimelineItem` (`item_id` = `GetUniqueId()`, name, `media_pool_id`, `record_start`, `record_end`, `source_start`, `source_end`, `duration`, `enabled`, color, markers, Fusion comp count).
* `timeline.playhead` — get or set current timecode/frame (`GetCurrentTimecode` / `SetCurrentTimecode`).
* `timeline.grab_still` — calls `Project.ExportCurrentFrameAsStill(temp_png)` (optionally moves playhead first, optionally downscales to <=1568px, and attaches image directly into the `Ledger` so vision models see the viewer).
* `timeline.watch_range` — *(Gemini superpower)* renders a specified `[in_tc, out_tc]` timeline range (or extracts source clip segment via FFmpeg) at 480p low-bitrate MP4 and attaches it to the `Ledger` via inlineData or Gemini Files API so Gemini can watch the cut with audio and pacing intact.

**4. Timeline Editing, Markers & Effects (Mutate — Auto-Checkpointed)**
* `timeline.create` / `timeline.duplicate` — create empty timeline or duplicate active timeline.
* `timeline.append_clips` — appends an ordered array of subclips `[{media_pool_id, start_frame, end_frame, track_index, record_frame, media_type}]` via `MediaPool.AppendToTimeline`. Primary workhorse for rough cuts and transcript-based assembly.
* `timeline.insert_generator_or_title` — inserts Resolve generators, titles, `Text+`, or Fusion compositions.
* `timeline.set_clip_props` — sets `TimelineItem.SetProperty` keys (`Pan`, `Tilt`, `ZoomX`, `ZoomY`, `CropLeft/Right/Top/Bottom`, `Opacity`, `Pitch`, `Yaw`, `FlipX/Y`, `CompositeMode`, `DynamicZoomEase`, `RetimeProcess`).
* `timeline.set_clip_enabled` / `timeline.set_clip_color` — toggle clip enable state or clip color tag.
* `timeline.delete_clips` — deletes items by `item_id` (`Timeline.DeleteClips(items, ripple=bool)`), gated or checkpointed.
* `timeline.import_otio` — imports a generated `.otio` or `.fcpxml` timeline via `MediaPool.ImportTimelineFromFile` for complex multi-track ripple/razor surgeries that Resolve's in-place API cannot express directly.
* `marker.list` / `marker.add` / `marker.delete` — timeline or clip markers with frame, color, name, note, duration, and `custom_data` namespace (`"davag"`).

**5. Color, Fusion & Deliver**
* `color.apply_lut` / `color.set_cdl` / `color.reset_grades` — node LUT and ASC-CDL grading on clips.
* `fusion.inspect_comp` / `fusion.set_tool_input` — inspect nodes in a `TimelineItem`'s Fusion comp (e.g., changing `StyledText` on a `Text+` title, which is the #1 way to programmatically set title text in Resolve!).
* `render.presets` / `render.queue_add` / `render.start` / `render.status` — Deliver page automation.

**6. Local Media Intelligence & Escape Hatches**
* `media.probe` — FFprobe stream/codec/loudness/duration inspection on source files on disk.
* `media.transcribe` — runs word-timestamped transcription (`faster-whisper` locally if installed, or Gemini/OpenAI audio API) cached by file hash in `~/.davag/cache/transcripts/`, mapping words directly to **Resolve source frame numbers** so the LLM never hallucinates frame math.
* `media.detect_silence` — FFmpeg `silencedetect` mapped to `[keep_start_frame, keep_end_frame]` speech segments ready for `timeline.append_clips`.
* `fs.read` / `fs.write` / `fs.replace` / `fs.list` — workspace file ops (sandboxed to `cwd`, journaled).
* `resolve.eval` — executes arbitrary Python inside the Resolve context (`resolve`, `fusion`, `bmd`, `project`, `timeline`, `mediapool`) with captured `stdout`/`result`. **Segregated & requires operator approval** unless `--yolo`.
* `shell` — local shell command, **segregated & requires operator approval** unless `--yolo`.

---

### 5.4 Dual Transport & Resolve Threading Mechanics

#### A. `DirectTransport` (External IPC — Studio)
1. `resolve_paths.py` locates:
   * Windows: `RESOLVE_SCRIPT_LIB` = `C:\Program Files\Blackmagic Design\DaVinci Resolve\fusionscript.dll`, `RESOLVE_SCRIPT_API` = `C:\ProgramData\Blackmagic Design\DaVinci Resolve\Support\Developer\Scripting`
   * macOS / Linux equivalents included for portability.
2. Injects environment variables into `os.environ` and appends `%RESOLVE_SCRIPT_API%\Modules` to `sys.path` in-process (no global Windows System Properties editing required!).
3. Calls `dvr_script.scriptapp("Resolve")`. Because `fusionscript.dll` calls are synchronous C++ IPC, `DirectTransport` runs `resolve_ops` batches inside a dedicated **single-thread `ThreadPoolExecutor(max_workers=1)`** so the `asyncio` TUI/Web loop never stutters while `fusionscript` stays pinned to one OS thread.

#### B. `BridgeTransport` + `davag_bridge.py` (Internal Server — Free & Studio)
1. `davag --install-bridge` writes a 25-line bootstrap script `davag_bridge.py` into:
   `%APPDATA%\Blackmagic Design\DaVinci Resolve\Support\Fusion\Scripts\Utility\davag_bridge.py`.
2. When clicked via `Workspace → Scripts → davag_bridge` inside Resolve:
   * Prepends `D:\davag\src` to `sys.path` and imports `davag.bridge.server` (100% stdlib).
   * Reads or creates `~/.davag/bridge.json` (`{"host": "127.0.0.1", "port": 18765, "token": "<random-hex>", "pid": ...}`).
   * Starts a background `threading.Thread` running a `selectors`-based TCP server on `127.0.0.1:18765`. Incoming authenticated JSON-RPC requests are placed onto `request_queue: queue.Queue` with a per-request `threading.Event` response slot.
   * **Crucial Resolve Main-Thread Pump:** Opens a compact `fusion.UIManager` window (`280x110`, title `"davag Bridge"`, showing port, connected status, op counter, and a `"Stop Bridge"` button) and pumps `disp = bmd.UIDispatcher(ui)` using a non-blocking timer / short loop so `request_queue` is drained **on `fuscript`'s main thread**. Every `resolve_ops` call executes safely without cross-thread COM/Qt crashes.

---

### 5.5 Prompt Caching vs. Live Resolve State

In `davai`, the system prompt (`preamble + protocol + grounding`) is byte-stable for the entire session so Anthropic/OpenAI/Gemini prefix caching hits at 90%+ discount.

In DaVinci Resolve, however, the user is also clicking around in Resolve's UI between turns (moving the playhead, switching timelines).
* **Static Cached System Prompt:** `preamble.md` + `protocol.md` (`dr_ops` spec) + `DAVAG.md` + workspace info. Never mutated mid-session.
* **Lightweight Per-Turn State Header (User Message Prefix):** Right before sending a user turn, `davag` runs a fast (<15ms) `resolve.quick_context()` poll (active project, active timeline name, FPS, resolution, current playhead TC, selected clip count) and prepends a compact 3-line `[Resolve Live State: Project="...", Timeline="...", Playhead=01:00:12:04, Page="edit"]` tag if changed since last turn—preserving 100% of the conversation prefix cache!

---

### 5.6 TUI REPL & Web GUI Sidecar (`--web` / `--host`)

#### Terminal REPL (`prompt_toolkit` + `rich`)
* **Live Composer while busy:** Typing during a streaming turn queues a **steer** (`steer-queued` → injected at next step boundary). Slash commands (`/context`, `/tps`, `/status`, `/yolo`) execute immediately mid-turn. `Esc` cancels the active turn.
* **Modal Approval Prompt:** When `resolve.eval`, `shell`, or un-checkpointed `timeline.delete_clips` runs, pauses with `[y] allow · [n] deny · [d] duplicate timeline first & allow`. Raced cleanly through `SessionBus` against any open browser tabs.
* **Status Bar (80-col safe):**
  `Project/Timeline · claude-opus-5/high · [direct] · ████░░░░░░ 12% · $0.08 · 48 tok/s ▃▅█▆`
* **Slash Commands:**
  `/model`, `/keys`, `/env`, `/context` (`ctrl+g`), `/tps` (`ctrl+t`), `/artifacts` (`ctrl+a`), `/resolve` (connection status, reconnect, switch direct/bridge), `/grab` (attach current Resolve viewer frame), `/watch [in out]` (render & attach timeline clip for Gemini), `/undo` (restore previous `.davag-snap` timeline), `/compact`, `/clear`, `/sessions`, `/resume`, `/history`, `/replay`, `/yolo`, `/help`, `/exit`.

#### Web GUI Sidecar (`davag --web` or `davag --host 0.0.0.0`)
Adapts `davai/src/ui/web/` (zero-build vanilla HTML/CSS/JS + uPlot, dark theme `#0b0e14`, token login gate, SSE `Last-Event-ID` reconnect):
1. **Chat + Live Viewer Strip:** Transcript with filter chips (`messages`, `thinking`, `ops`, `notices`, `errors`), approval banner, composer with `/` command autocomplete, **plus a Resolve Monitor card** showing active Project/Timeline/Playhead TC, a `[Grab Viewer Frame]` button, and inline thumbnails whenever `timeline.grab_still` or `timeline.watch_range` runs.
2. **Resolve Tab (New):** Live tree view of Media Pool bins/clips and visual track overview of the active Timeline, transport badge (`Direct IPC` vs `Bridge :18765`), and one-click timeline checkpoint restore.
3. **Perf Tab:** uPlot tok/s turn curve, TTFT, and tok/s vs `contextIn`.
4. **Context Tab:** Segment table with token bars, pin/drop checkboxes, `summarize selected`, and `compact now`.
5. **Models / Keys / .env Panels:** Browse providers (Anthropic, Gemini, OpenAI, OpenRouter), sort OpenRouter catalog by price/context, verify & save masked API keys, switch `#---#` `.env` profiles.
6. **Artifacts Tab:** View/copy/download generated Python scripts, `.otio`/`.fcpxml` timelines, `.srt` subtitles, or Fusion `.setting` snippets.

---

## 6. Phased Implementation Roadmap & Verification Checkpoints

Each phase ends with a concrete, runnable checkpoint before moving to the next.

### Phase 1: Project Skeleton, Config, `.env` Profiles & Resolve Discovery
* **Deliverables:**
  * `pyproject.toml`, `.venv` setup (`pip install -e ".[dev]"`), `DAVAG.md`, `.gitignore`.
  * `davag.cli` (`parse_args`, `UsageError`, `--help`, `--version`, `--config`, `--doctor`).
  * `davag.config` (`env.py`, `envfile.py` CRLF-safe atomic editor, `profiles.py` `#---#` switcher, `models.py` seed registry, `resolve_paths.py` Windows/macOS/Linux Resolve & FFmpeg locator).
* **Checkpoint 1 (Automated + Manual):**
  * `pytest tests/test_cli.py tests/test_config_profiles.py` passes (verifies byte-for-byte CRLF `.env` profile round-trips and CLI arg edge cases).
  * Running `davag --doctor` from `.venv` detects local DaVinci Resolve installation paths (`fusionscript.dll`, `DaVinciResolveScript.py`, `Scripts\Utility` folder, Python arch, FFmpeg availability) and prints a clear status table.

### Phase 2: Dual Transport (`DirectTransport` + `BridgeTransport`) & `FakeResolve` Harness
* **Deliverables:**
  * `davag.resolve_ops.schema` & `helpers.py` (Lua `{1: x}` table normalization, SMPTE timecode ↔ frame math including drop-frame 29.97/59.94).
  * `davag.resolve_ops.session_ops` (`resolve.status`, `resolve.open_page`, `project.list/open/save`).
  * `davag.bridge.protocol` & `davag.bridge.server` (`UIManager` keep-alive dialog + main-thread queue pump) + `davag --install-bridge`.
  * `davag.transport` (`DirectTransport`, `BridgeTransport`, `auto` negotiation).
  * `tests/fake_resolve.py` (in-memory Resolve mock so all `resolve_ops` and TCP bridge RPCs run in CI without Resolve open).
* **Checkpoint 2 (Automated + Live Resolve Handshake):**
  * `pytest tests/test_bridge_rpc.py` spins up `bridge/server.py` against `FakeResolve` on an ephemeral port and verifies auth rejection, concurrent requests, and parity with `DirectTransport`.
  * **Live check:** Run `davag --doctor` / `davag -p "check resolve status"` with Resolve open to verify live connection via Direct IPC (if Studio) and via `Workspace → Scripts → davag_bridge`.

### Phase 3: Full `resolve_ops` Suite, Checkpoints & Media Intelligence
* **Deliverables:**
  * `mediapool_ops.py`, `timeline_ops.py`, `marker_ops.py`, `color_fusion_ops.py` (including `Text+` `StyledText` editing & `grab_still`), `render_ops.py`, and `eval_op.py`.
  * `agent/journal.py` & `agent/executor.py` implementing two-phase Plan → Checkpoint (`DuplicateTimeline` + file snapshot) → Apply → Approval segregation.
  * `media_intel/` (`ffmpeg.py` silence detection & 480p proxy range export, `transcribe.py` word-to-source-frame mapper, `otio_builder.py` FCPXML/OTIO generator).
* **Checkpoint 3:**
  * `pytest tests/test_resolve_ops.py tests/test_executor_checkpoints.py` passes 100% of op schemas, Lua-dict normalizations, timecode edge cases, rollback on mid-batch failure, and `Text+` inspection.

### Phase 4: Multi-Provider Adapters, `dr_ops` Loop, Video/Files API & Sessions
* **Deliverables:**
  * `providers/` (`anthropic.py`, `gemini.py` with inline + **Gemini Files API** + YouTube `fileUri`, `openai_responses.py`, `openai_chat.py` + OpenRouter catalog fetch, `index.py` auth/surface diagnostics).
  * `context/` (`ledger.py`, `tokens.py`, `compact.py`, `media.py`, `pastes.py`, `artifacts.py`).
  * `agent/` (`parser.py` with `--davag:NAME--` & XML-leak recovery, `prompt.py` with 6 overridable markdown prompts, `loop.py` with steers, repetition brake, effort demotion, and `EVENTS` registry).
  * `session/` (`bootstrap.py`, `bus.py`, `dispatch.py`, `log.py`, `resume.py`, `perf.py`) + `headless.py`.
* **Checkpoint 4:**
  * `pytest tests/test_parser.py tests/test_providers.py` passes recorded stream translations for all 4 providers (Anthropic thinking+cache, Gemini thought+Files API+video, OpenAI Responses summaries, OpenRouter reasoning).
  * Headless end-to-end works: `davag -p "inspect current timeline and add a blue marker at the playhead"` executes `dr_ops`, logs to `~/.davag/sessions/`, and supports `davag --resume`.

### Phase 5: Interactive TUI REPL & Web GUI Sidecar (`--web` / `--host`)
* **Deliverables:**
  * `ui/commands.py` (single-source `HELP` → `command_list()`).
  * `ui/tui/` (`prompt_toolkit` async composer with live steering, `@path` & `/command` tab completion, `[[paste#N]]` folding, `rich` streaming markdown/thinking/ops display, modal approval pane, 80-col status bar with tok/s sparkline).
  * `server/` (`auth.py` with `assert_safe_bind` blocking `0.0.0.0` + `--yolo`, `http.py` SSE `/api/events` + `Last-Event-ID` + REST endpoints + static `/api/media/<still>` preview route).
  * `ui/web/` (`index.html`, `app.js`, `app.css` with Chat, Resolve Inspector, Perf uPlot charts, Context manager, Model/Key/.env switchers, Artifacts downloader, and raced approval banner).
  * `tests/test_events_contract.py` & `tests/test_server_sse.py` ensuring zero unretired events across TUI, Headless, and Web.
* **Checkpoint 5 (Final Acceptance):**
  * Full test suite passes (`pytest`).
  * Launch `davag --host 0.0.0.0 --port 7879` alongside DaVinci Resolve:
    1. Connect from browser using terminal bearer token; verify TUI and browser stay in lockstep on prompts, steers, and raced `resolve.eval` approvals.
    2. Switch live between **Claude Opus 5** (for complex timeline assembly), **Gemini 3 Pro** (run `/watch` or `timeline.watch_range` to watch a timeline cut and critique pacing/visuals), **OpenAI**, and **OpenRouter**.
    3. Verify `/undo` cleanly restores the pre-edit Resolve timeline snapshot.

---

## 7. Dependencies (`pyproject.toml`)

 Kept deliberately lean—no LangChain/LlamaIndex/agent frameworks:

* **Core runtime:**
  * `anthropic>=0.49` — Claude Opus 5 / Sonnet 5 streaming, adaptive thinking, prompt caching
  * `google-genai>=1.5` — Gemini streaming, inline video, Files API upload, YouTube `fileUri`
  * `openai>=1.68` — OpenAI Responses API, Chat Completions, and OpenRouter
  * `aiohttp>=3.11` — Async HTTP + SSE web sidecar server (clean streaming + zero framework bloat)
  * `prompt-toolkit>=3.0.50` — Async terminal composer, keybindings, tab completion, bracketed paste
  * `rich>=13.9` — Terminal markdown, spinners, tables, syntax highlighting
  * `opentimelineio>=0.17` — Programmatic FCPXML / OTIO timeline generation for complex ripple cuts
* **Optional extras:**
  * `davag[whisper]`: `faster-whisper>=1.1` for local GPU/CPU word-level transcription
  * `davag[dev]`: `pytest>=8.3`, `pytest-asyncio>=0.25`, `ruff>=0.9`
* **Inside Resolve (`davag.resolve_ops` & `davag.bridge`):**
  * **Zero third-party packages** (Python 3 stdlib only).