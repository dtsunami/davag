"""Single-source-of-truth REPL & Web slash commands (`davag.ui.commands`).

Parsed directly from `HELP_TEXT` -> `command_list()` so TUI tab completion and Web
command completion can never drift!
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from davag.context.compact import compact_ledger
from davag.context.media import load_image_part, load_video_part, youtube_ref
from davag.media_intel.ffmpeg import extract_proxy_clip
from davag.session.bootstrap import Session
from davag.session.keys_ctl import key_state, set_provider_key
from davag.session.log import list_sessions
from davag.session.models_ctl import apply_model_change
from davag.session.profiles_ctl import profile_state, switch_profile

HELP_TEXT = """Commands
  sh <cmd>                  run a local shell command; output is added to context
  /model [id] [--save]      switch model or effort (e.g. `/model gemini-3-pro-preview`, `/model opus`)
  /effort <level>           set reasoning effort: low | medium | high | xhigh | max
  /resolve [mode]           show DaVinci Resolve status or reconnect (`auto` | `direct` | `bridge`)
  /grab [tc]                  grab current Resolve viewer frame & attach image to context
  /sheet <clip|path> [s dur]  build burned-in-TC contact sheet PNG & attach to vision context
  /index [clip|path]          run fast Scout LLM (`gemini-3.8-flash`) to index clip & cache Digest
  /proxy <clip|path> [h kbps] build QSV editing proxy (`360|540|720|1080`, optional kbps) & link in Resolve
  /transcribe <clip> [s dur]  extract frame-mapped transcript (Gemini Audio / Whisper) & attach to context
  /vo [prefix]                convert timeline markers to Fairlight ADR `.csv` + on-screen `.srt` teleprompter
  /role [name]                view or switch capability surface (`reviewer|logger|editor|finisher|master`)
  /thumbnail <prompt>         generate 16:9 YouTube thumbnail via OpenRouter Flux (+ current viewer frame)
  /youtube-stage <title>      stage YouTube release bundle (chapters, `.srt`, `DESCRIPTION.md`, render queue)
  /youtube-auth [json_path]   run one-time Google OAuth browser flow & save `~/.davag/youtube_token.json`
  /watch <clip|path> [s dur]  transcode QSV/NVENC review video & attach for Gemini review
  /undo                       restore active Resolve timeline from most recent `.davag-snap` checkpoint
  /image <path>               attach a workspace image to context
  /video <path>               attach a workspace video to context (Gemini inline <=18MB / Files API >18MB)
  /youtube <url> [minutes]    attach a YouTube video by reference (Gemini only)
  /context                    show context segment breakdown & token budget
  /tps                        show tok/s throughput and TTFT per turn
  /artifacts [save <id> <p>]  list emitted code/OTIO/FCPXML artifacts or save one
  /compact                  compact context ledger now
  /clear                    clear conversation history while keeping system grounding
  /prompts [sub]            inspect/edit prompts & tool protocol: export | diff | accept | reset | apply
  /keys [prov] [key]        inspect configured API keys or set one (`--save` persists to ~/.davag/.env)
  /env [block|off]          list `#---#` .env provider blocks or switch active block
  /config                   display resolved configuration
  /sessions                 list recent sessions on disk
  /history [n]              show recent prompts across sessions
  /replay <n>               re-run prompt #n from `/history` (e.g. `/replay -1`)
  /web [open|copy|token]    show tokenized Web URL, open in browser, or copy URL/token to clipboard
  /yolo [on|off]            toggle auto-approval for gated ops in this session
  /events                   show harness events emitted vs retired this session
  /help                     show this command reference
  /exit                     quit davag

Keys
  esc / ctrl+c              cancel running turn (ctrl+c when idle exits)
  alt+c / alt+t / alt+o     copy tokenized Web URL / copy token / open Web GUI in browser
  tab                       complete `/commands` and `@paths`"""

MODEL_SHORTCUTS: dict[str, tuple[str, str]] = {
    "opus": ("anthropic", "claude-opus-5-5"),
    "opus5": ("anthropic", "claude-opus-5"),
    "sonnet": ("anthropic", "claude-sonnet-5-5"),
    "haiku": ("anthropic", "claude-haiku-5-5"),
    "gemini": ("gemini", "gemini-3.8-flash"),
    "flash": ("gemini", "gemini-3.8-flash"),
    "carbon": ("gemini", "gemini-4-carbon"),
    "argon": ("gemini", "gemini-4-argon"),
    "gpt": ("openai", "gpt-5.1"),
    "mini": ("openai", "gpt-5-mini"),
    "grok": ("grok", "grok-4"),
}


def command_list() -> list[dict[str, str]]:
    """Parse `HELP_TEXT` Commands section so REPL and Web completions never drift."""
    out: list[dict[str, str]] = []
    for line in HELP_TEXT.splitlines():
        if line and not line.startswith(" ") and out:
            break
        m = re.match(r"^  (\S.*?)\s{2,}(\S.*)$", line)
        if not m:
            continue
        usage = m.group(1).strip()
        name = usage.split()[0]
        if not name.startswith("/") and name != "sh":
            continue
        out.append({"name": name, "usage": usage, "desc": m.group(2).strip()})
    return out


def _infer_provider_for_model(model_id: str, current_provider: str) -> str:
    low = model_id.lower()
    if "/" in model_id:
        return "openrouter"
    if low.startswith("claude-"):
        return "anthropic"
    if low.startswith("gemini-"):
        return "gemini"
    if low.startswith(("gpt-", "o1", "o3", "o4")):
        return "openai"
    if low.startswith("grok-"):
        return "grok"
    return current_provider


async def handle_command(
    raw_text: str,
    *,
    session: Session,
    push: Callable[[dict[str, Any]], None],
    exit_app: Callable[[], None],
) -> dict[str, Any]:
    """Execute a local REPL/Web command. Returns `{"handled": bool, "replay_input"?: str}`."""
    text = raw_text.strip()

    # --- `sh <cmd>` ---
    if text == "sh" or text.startswith("sh "):
        cmd_str = text[2:].strip()
        if not cmd_str:
            push({"type": "notice", "message": "usage: sh <command>"})
            return {"handled": True}
        push({"type": "user", "text": text})
        proc = await asyncio.to_thread(
            subprocess.run,
            cmd_str,
            shell=True,
            cwd=str(session.cfg.cwd),
            capture_output=True,
            text=True,
            timeout=session.cfg.shell_timeout,
            check=False,
        )
        combined = ((proc.stdout or "") + (proc.stderr or "")).strip()
        session.ledger.add(
            seg_type="shell",
            label=f"$ {cmd_str[:40]} (exit {proc.returncode})",
            role="user",
            text=f"$ {cmd_str}\n(exit {proc.returncode})\n{combined[:12_000]}",
        )
        push(
            {
                "type": "shell",
                "cmd": cmd_str,
                "exit_code": proc.returncode,
                "output": combined[:8000],
            }
        )
        return {"handled": True}

    if not text.startswith("/"):
        return {"handled": False}

    parts = text.split()
    cmd = parts[0].lower()
    args = parts[1:]

    if cmd in ("/help", "/?"):
        push({"type": "info", "text": HELP_TEXT})
        return {"handled": True}

    if cmd in ("/exit", "/quit", "/q"):
        exit_app()
        return {"handled": True}

    if cmd == "/clear":
        dropped = session.ledger.clear_conversation()
        session.bus.publish("cleared", {"dropped": dropped})
        push({"type": "notice", "message": f"Cleared {dropped} conversation segments (grounding kept)."})
        return {"handled": True}

    if cmd == "/yolo":
        if args and args[0].lower() in ("on", "true", "1"):
            want = True
        elif args and args[0].lower() in ("off", "false", "0"):
            want = False
        else:
            want = not session.cfg.yolo
        if want and session.cfg.web_enabled:
            from davag.cli import UsageError, assert_safe_bind

            try:
                assert_safe_bind(host=session.cfg.web_host, yolo=True)
            except UsageError as err:
                push({"type": "warning", "message": str(err)})
                return {"handled": True}
        session.cfg.yolo = want
        session.bus.publish("yolo-changed", {"yolo": want})
        push({"type": "notice", "message": f"yolo mode {'ENABLED' if want else 'disabled'} for this session."})
        return {"handled": True}

    if cmd == "/effort":
        if not args:
            push({"type": "info", "text": f"current effort: {session.cfg.effort} (low | medium | high | xhigh | max)"})
            return {"handled": True}
        try:
            res = apply_model_change(session, effort=args[0])
            push({"type": "notice", "message": f"effort -> {res['effort']} ({res['provider']}/{res['model']['id']})"})
        except ValueError as err:
            push({"type": "warning", "message": str(err)})
        return {"handled": True}

    if cmd == "/model":
        persist = "--save" in args
        clean_args = [a for a in args if a != "--save"]
        if not clean_args:
            m = session.cfg.model
            push(
                {
                    "type": "info",
                    "text": (
                        f"current: {session.cfg.provider} / {m.id} (effort={session.cfg.effort}, "
                        f"ctx={m.context:,}, video={'yes' if m.video else 'no'})\n"
                        "shortcuts: /model opus | /model sonnet | /model gemini | /model flash | /model gpt | /model <provider>/<slug>"
                    ),
                }
            )
            return {"handled": True}
        target = clean_args[0]
        if target.lower() in MODEL_SHORTCUTS:
            prov, mid = MODEL_SHORTCUTS[target.lower()]
        elif ":" in target and not target.startswith("http"):
            prov, mid = target.split(":", 1)
        else:
            prov = _infer_provider_for_model(target, session.cfg.provider)
            mid = target
        try:
            res = apply_model_change(session, provider=prov, model_id=mid, persist=persist)
            msg = f"switched -> {res['provider']} / {res['model']['id']} (video={'yes' if res['model']['video'] else 'no'})"
            if res.get("note"):
                msg += f"\n{res['note']}"
            push({"type": "notice", "message": msg})
        except ValueError as err:
            push({"type": "warning", "message": str(err)})
        return {"handled": True}

    if cmd == "/resolve":
        mode_arg = args[0].lower() if args else None
        try:
            if mode_arg or session.transport is None:
                st = await session.reconnect_resolve(mode_arg)
            else:
                st = await session.transport.call("resolve.status", timeout=4.0)
            proj = st.get("project") or {}
            tl = st.get("timeline") or {}
            push(
                {
                    "type": "info",
                    "text": (
                        f"Connected [{st.get('mode')}] -> {st.get('product')} {st.get('version')}\n"
                        f"  page:     {st.get('page')}\n"
                        f"  project:  {proj.get('name') or '(none)'} ({proj.get('fps')} fps)\n"
                        f"  timeline: {tl.get('name') or '(none)'} (playhead={tl.get('playhead_tc')})"
                    ),
                }
            )
        except Exception as err:  # noqa: BLE001
            push({"type": "warning", "message": str(err)})
        return {"handled": True}

    if cmd == "/grab":
        if session.transport is None:
            push({"type": "warning", "message": "DaVinci Resolve is not connected (`/resolve` first)."})
            return {"handled": True}
        try:
            params: dict[str, Any] = {}
            if args:
                params["tc"] = args[0]
            still = await session.transport.call("timeline.grab_still", params)
            img = load_image_part(still["path"])
            session.ledger.add(
                seg_type="media",
                label=f"viewer frame @ {still.get('playhead_tc')} ({img['bytes'] // 1024}kB)",
                role="user",
                image=img,
            )
            push(
                {
                    "type": "notice",
                    "message": f"Grabbed Resolve viewer frame at {still.get('playhead_tc')} -> {still['path']} (~{img['tokens']} tokens)",
                    "image_path": still["path"],
                }
            )
        except Exception as err:  # noqa: BLE001
            push({"type": "warning", "message": f"/grab failed: {err}"})
        return {"handled": True}

    async def _resolve_clip_or_path(target_str: str) -> tuple[Path, str | None]:
        if session.transport is not None:
            with contextlib.suppress(Exception):
                mp = await session.transport.call("mediapool.clips", {"recursive": True}, timeout=3.0)
                for c in mp.get("clips") or []:
                    if target_str in (c.get("clip_id"), c.get("name")) and c.get("file_path"):
                        return Path(str(c["file_path"])).resolve(), str(c.get("clip_id"))
        return session.sandbox.resolve_for_read(target_str), None

    if cmd == "/sheet":
        if not args:
            push({"type": "notice", "message": "usage: /sheet <clip_id|clip_name|path> [start_sec] [duration_sec]"})
            return {"handled": True}
        try:
            from davag.media_intel.ffmpeg import generate_contact_sheet

            src_path, _cid = await _resolve_clip_or_path(args[0])
            s_sec = float(args[1]) if len(args) >= 2 else None
            d_sec = float(args[2]) if len(args) >= 3 else None
            sheet = await asyncio.to_thread(
                generate_contact_sheet, src_path, start_sec=s_sec, duration_sec=d_sec
            )
            img = load_image_part(sheet["path"])
            session.ledger.add(
                seg_type="media",
                label=f"contact sheet {src_path.name} ({sheet['cols']}x{sheet['rows']})",
                role="user",
                image=img,
            )
            session.bus.publish("media-preview", {"kind": "image", "path": sheet["path"], "title": f"Contact Sheet: {src_path.name}"})
            push(
                {
                    "type": "notice",
                    "message": f"Built {sheet['cols']}x{sheet['rows']} timecoded contact sheet ({sheet['bytes'] // 1024} KB) & attached (~{img['tokens']} tokens)",
                }
            )
        except Exception as err:  # noqa: BLE001
            push({"type": "warning", "message": f"/sheet failed: {err}"})
        return {"handled": True}

    if cmd == "/index":
        try:
            from davag.media_intel.digest import (
                analyze_clip_with_scout,
                pick_scout_provider_and_model,
            )

            target_arg = args[0] if args else ""
            if not target_arg and session.transport is not None:
                mp = await session.transport.call("mediapool.clips", {"recursive": True}, timeout=3.0)
                first_with_path = next((c for c in (mp.get("clips") or []) if c.get("file_path")), None)
                if first_with_path:
                    target_arg = str(first_with_path["clip_id"])
            if not target_arg:
                push({"type": "notice", "message": "usage: /index <clip_id|clip_name|path>"})
                return {"handled": True}

            src_path, cid = await _resolve_clip_or_path(target_arg)
            sp, sm = pick_scout_provider_and_model(session.cfg)
            push({"type": "notice", "message": f"Indexing {src_path.name} with Scout model ({sp}/{sm})…"})
            digest = await analyze_clip_with_scout(
                src_path,
                cfg=session.cfg,
                cache_dir=session.cfg.home / "cache",
                clip_id=cid,
                force_refresh="--force" in args,
            )
            session.ledger.add(
                seg_type="op-result",
                label=f"Clip Digest: {src_path.name}",
                role="user",
                text=f"[Clip Digest for {src_path.name} (clip_id={cid})]\n{digest['summary']}",
            )
            if digest.get("contact_sheet_path"):
                session.bus.publish(
                    "media-preview",
                    {"kind": "image", "path": digest["contact_sheet_path"], "title": f"Storyboard: {src_path.name}"},
                )
            push({"type": "info", "text": f"Clip Digest ({digest.get('scout_model')}):\n\n{digest['summary']}"})
        except Exception as err:  # noqa: BLE001
            push({"type": "warning", "message": f"/index failed: {err}"})
        return {"handled": True}

    if cmd == "/proxy":
        if not args:
            push({"type": "notice", "message": "usage: /proxy <clip_id|clip_name|path> [height: 360|540|720|1080] [kbps]"})
            return {"handled": True}
        try:
            from davag.media_intel.ffmpeg import generate_editing_proxy

            src_path, cid = await _resolve_clip_or_path(args[0])
            height = int(args[1]) if len(args) >= 2 else 720
            default_kbps = {360: 700, 480: 1200, 540: 1800, 720: 3500, 1080: 6000}.get(height, 3500)
            kbps = int(args[2]) if len(args) >= 3 else default_kbps
            push({"type": "notice", "message": f"Transcoding {height}p ({kbps} kbps) QSV editing proxy for {src_path.name}…"})
            proxy_dir = session.cfg.home / "cache" / "resolve_proxies"
            proxy_dir.mkdir(parents=True, exist_ok=True)
            out_file = proxy_dir / f"{src_path.stem}_{height}p_proxy.mp4"
            res = await asyncio.to_thread(
                generate_editing_proxy, src_path, output_path=out_file, height=height, video_kbps=kbps
            )
            linked = False
            if cid and session.transport is not None and "--no-link" not in args:
                l_res = await session.transport.call("mediapool.link_proxy", {"clip_id": cid, "proxy_path": res["path"]})
                linked = bool(l_res.get("linked"))
            session.bus.publish("media-preview", {"kind": "video", "path": res["path"], "title": f"Proxy ({height}p): {src_path.name}"})
            push(
                {
                    "type": "notice",
                    "message": (
                        f"Generated {height}p proxy via {res['encoder']} ({res['bytes'] / (1024*1024):.1f} MB) -> {res['path']}"
                        + (" [Linked in DaVinci Resolve!]" if linked else "")
                    ),
                }
            )
        except Exception as err:  # noqa: BLE001
            push({"type": "warning", "message": f"/proxy failed: {err}"})
        return {"handled": True}

    if cmd == "/transcribe":
        if not args:
            push({"type": "notice", "message": "usage: /transcribe <clip_id|clip_name|path> [start_sec] [duration_sec]"})
            return {"handled": True}
        try:
            from davag.media_intel.transcribe import transcribe_media

            src_path, cid = await _resolve_clip_or_path(args[0])
            s_sec = float(args[1]) if len(args) >= 2 else None
            d_sec = float(args[2]) if len(args) >= 3 else None
            push({"type": "notice", "message": f"Transcribing {src_path.name}…"})
            tr_res = await transcribe_media(
                src_path,
                cache_dir=session.cfg.home / "cache" / "transcripts",
                gemini_api_key=session.cfg.keys.get("gemini"),
                openai_api_key=session.cfg.keys.get("openai"),
                start_sec=s_sec,
                duration_sec=d_sec,
            )
            lines_txt = "\n".join(
                f"[{s['start_tc']} f{s['start_frame']}..{s['end_frame']}] {s['text']}"
                for s in (tr_res.get("segments") or [])[:120]
            )
            session.ledger.add(
                seg_type="op-result",
                label=f"Transcript: {src_path.name} ({tr_res['segment_count']} segs)",
                role="user",
                text=f"[Transcript for {src_path.name} (clip_id={cid}, backend={tr_res['backend']})]\n{lines_txt}",
            )
            push(
                {
                    "type": "info",
                    "text": f"Transcript ({tr_res['backend']}, {tr_res['segment_count']} segments — attached to context):\n\n{lines_txt[:4000]}",
                }
            )
        except Exception as err:  # noqa: BLE001
            push({"type": "warning", "message": f"/transcribe failed: {err}"})
        return {"handled": True}

    if cmd == "/vo":
        if session.transport is None:
            push({"type": "warning", "message": "DaVinci Resolve is not connected (`/resolve` first)."})
            return {"handled": True}
        try:
            prefix = args[0] if args else "vo_teleprompter"
            out_dir = str(session.cfg.cwd / "vo_scripts")
            res = await session.transport.call(
                "timeline.vo_script",
                {"output_dir": out_dir, "prefix": prefix, "import_srt_track": "--no-srt" not in args},
            )
            cues_preview = "\n".join(
                f"  #{b['cue']:02d} [{b['in_tc']} -> {b['out_tc']} · {b['duration_sec']}s · ~{b['recommended_words']}w]\n      {b['script']}"
                for b in res["beats"]
            )
            msg = (
                f"Built Voiceover Teleprompter for '{res['timeline']}' ({res['beat_count']} beats):\n"
                f"  • On-Screen Subtitle Track: {'IMPORTED onto ST1 (VO Teleprompter)!' if res['imported_onto_timeline'] else res['resolve_srt']}\n"
                f"  • Fairlight ADR Cue List:   {res['fairlight_adr_csv']}\n"
                f"    (In Resolve: Fairlight -> ADR panel -> Setup -> Import Cue List -> select CSV for countdown streamer!)\n"
                f"  • Live Web Teleprompter:    open Web GUI -> click `🎬 timeline` tab!\n\n"
                f"{cues_preview}"
            )
            push({"type": "info", "text": msg})
        except Exception as err:  # noqa: BLE001
            push({"type": "warning", "message": f"/vo failed: {err}"})
        return {"handled": True}

    if cmd == "/role":
        from davag.agent.roles import ROLE_SPECS, ROLES
        from davag.session.promptedit import apply_staged_prompts

        if not args:
            lines = ["Capability Roles (`/role <name>`):"]
            for r_key in ROLES:
                rs = ROLE_SPECS[r_key]
                cur = "  <-- active" if session.cfg.role == r_key else ""
                lines.append(f"  • {r_key:<10} — {rs.title}: {rs.summary}{cur}")
            push({"type": "info", "text": "\n".join(lines)})
            return {"handled": True}

        target_role = args[0].strip().lower()
        if target_role not in ROLES:
            push({"type": "warning", "message": f"Unknown role '{target_role}'. Choose one of: {', '.join(ROLES)}"})
            return {"handled": True}
        session.cfg.role = target_role
        reb = apply_staged_prompts(session)
        session.bus.publish("config-changed", {"role": target_role})
        rs = ROLE_SPECS[target_role]
        push(
            {
                "type": "notice",
                "message": f"Switched active role -> {target_role} ({rs.title}; system prompt {reb['after_tokens']:,} tok)",
            }
        )
        return {"handled": True}

    if cmd == "/thumbnail":
        if not args:
            push({"type": "notice", "message": "usage: /thumbnail <prompt>  (uses current Resolve viewer frame as reference if connected!)"})
            return {"handled": True}
        try:
            from davag.youtube.studio import generate_youtube_thumbnail

            prompt_txt = " ".join(args)
            refs: list[Path] = []
            if session.transport is not None:
                with contextlib.suppress(Exception):
                    grab = await session.transport.call("timeline.grab_still", {"attach_to_context": False})
                    refs.append(Path(str(grab["path"])))
            out_png = session.cfg.cwd / "youtube_staging" / "thumbnail.png"
            push({"type": "notice", "message": f"Generating 16:9 thumbnail via OpenRouter ({session.cfg.image_model})…"})
            th = await generate_youtube_thumbnail(
                prompt=prompt_txt, output_path=out_png, cfg=session.cfg, reference_images=refs
            )
            if session.cfg.model.vision:
                session.ledger.add(seg_type="media", label="YouTube Thumbnail", role="user", image=load_image_part(th["path"]))
            session.bus.publish("media-preview", {"kind": "image", "path": th["path"], "title": f"Thumbnail ({th['model']})"})
            push({"type": "notice", "message": f"Saved thumbnail ({th['bytes']//1024} KB) -> {th['path']}"})
        except Exception as err:  # noqa: BLE001
            push({"type": "warning", "message": f"/thumbnail failed: {err}"})
        return {"handled": True}

    if cmd == "/youtube-stage":
        if session.transport is None:
            push({"type": "warning", "message": "DaVinci Resolve is not connected."})
            return {"handled": True}
        if not args:
            push({"type": "notice", "message": "usage: /youtube-stage <Video Title>"})
            return {"handled": True}
        try:
            from davag.youtube.studio import stage_youtube_package

            title = " ".join(args)
            pkg = await stage_youtube_package(
                transport=session.transport,
                staging_root=session.cfg.cwd / "youtube_staging",
                title=title,
            )
            push(
                {
                    "type": "info",
                    "text": (
                        f"Staged YouTube release '{pkg['title']}' in {pkg['staging_dir']}:\n"
                        f"  • Manifest:    {pkg['manifest_path']}\n"
                        f"  • Description: {pkg['description_path']} ({len(pkg['chapters'])} chapters)\n"
                        f"  • Captions:    {pkg['captions_srt']}\n"
                        f"  • Render Job:  {pkg['render_job_id']} -> {pkg['video_path']}"
                    ),
                }
            )
        except Exception as err:  # noqa: BLE001
            push({"type": "warning", "message": f"/youtube-stage failed: {err}"})
        return {"handled": True}

    if cmd == "/youtube-auth":
        try:
            from davag.youtube.studio import run_youtube_oauth_flow

            secret_arg = Path(" ".join(args).strip(' "\'')) if args else None
            push({"type": "notice", "message": "Opening browser for Google YouTube OAuth2 consent…"})
            saved = await run_youtube_oauth_flow(home=session.cfg.home, client_secret_path=secret_arg)
            push(
                {
                    "type": "notice",
                    "message": f"Saved YouTube OAuth refresh_token -> {saved['token_path']} (ready for youtube.upload!)",
                }
            )
        except Exception as err:  # noqa: BLE001
            push({"type": "warning", "message": f"/youtube-auth failed: {err}"})
        return {"handled": True}

    if cmd == "/watch":
        if not args:
            push({"type": "notice", "message": "usage: /watch <clip_id|clip_name|path> [start_sec] [duration_sec]"})
            return {"handled": True}
        if not session.cfg.model.video:
            push(
                {
                    "type": "warning",
                    "message": f"{session.cfg.model.id} does not support video input — switch `/model gemini` (or use `/sheet` / `/index`)!",
                }
            )
            return {"handled": True}
        try:
            src_path, _cid = await _resolve_clip_or_path(args[0])
            s_sec = float(args[1]) if len(args) >= 2 else None
            d_sec = float(args[2]) if len(args) >= 3 else None
            proxy = await asyncio.to_thread(
                extract_proxy_clip, src_path, start_sec=s_sec, duration_sec=d_sec
            )
            vid = load_video_part(proxy["path"])
            session.ledger.add(
                seg_type="media",
                label=f"watch {src_path.name} ({proxy['encoder']}, {vid['delivery']})",
                role="user",
                video=vid,
            )
            session.bus.publish("media-preview", {"kind": "video", "path": proxy["path"], "title": f"Watch: {src_path.name}"})
            push(
                {
                    "type": "notice",
                    "message": (
                        f"Transcoded proxy ({proxy['encoder']}, {proxy['bytes'] / 1024:.0f} KB) & "
                        f"attached via {vid['delivery']} (~{vid['tokens']:,} tokens)"
                    ),
                }
            )
        except Exception as err:  # noqa: BLE001
            push({"type": "warning", "message": f"/watch failed: {err}"})
        return {"handled": True}

    if cmd == "/undo":
        if session.transport is None:
            push({"type": "warning", "message": "DaVinci Resolve is not connected."})
            return {"handled": True}
        try:
            undone = await session.journal.undo_last_resolve(session.transport)
            push(
                {
                    "type": "notice",
                    "message": f"Restored timeline '{undone['restored_timeline']}' from checkpoint #{undone['checkpoint_seq']}.",
                }
            )
        except Exception as err:  # noqa: BLE001
            push({"type": "warning", "message": str(err)})
        return {"handled": True}

    if cmd == "/image":
        if not args:
            push({"type": "notice", "message": "usage: /image <path>"})
            return {"handled": True}
        try:
            img = load_image_part(args[0], session.sandbox)
            session.ledger.add(
                seg_type="media",
                label=f"image {img['name']}",
                role="user",
                image=img,
            )
            push({"type": "notice", "message": f"attached {img['name']} (~{img['tokens']} tokens)"})
        except Exception as err:  # noqa: BLE001
            push({"type": "warning", "message": str(err)})
        return {"handled": True}

    if cmd == "/video":
        if not args:
            push({"type": "notice", "message": "usage: /video <path>"})
            return {"handled": True}
        if not session.cfg.model.video:
            push({"type": "warning", "message": f"{session.cfg.model.id} does not accept video (switch `/model gemini`)."})
            return {"handled": True}
        try:
            vid = load_video_part(args[0], session.sandbox)
            session.ledger.add(
                seg_type="media",
                label=f"video {vid['name']} ({vid['delivery']})",
                role="user",
                video=vid,
            )
            push(
                {
                    "type": "notice",
                    "message": f"attached {vid['name']} via {vid['delivery']} (~{vid['tokens']:,} tokens)",
                }
            )
        except Exception as err:  # noqa: BLE001
            push({"type": "warning", "message": str(err)})
        return {"handled": True}

    if cmd == "/youtube":
        if not args:
            push({"type": "notice", "message": "usage: /youtube <url> [minutes]"})
            return {"handled": True}
        if not session.cfg.model.video:
            push({"type": "warning", "message": f"{session.cfg.model.id} does not accept video (switch `/model gemini`)."})
            return {"handled": True}
        try:
            mins = float(args[1]) if len(args) >= 2 else None
            yt = youtube_ref(args[0], minutes=mins)
            session.ledger.add(seg_type="media", label=f"youtube {yt['uri']}", role="user", video=yt)
            note = "assumed" if yt["assumed"] else "specified"
            push({"type": "notice", "message": f"attached {yt['uri']} (~{yt['tokens']:,} tokens, {yt['minutes']} min {note})"})
        except Exception as err:  # noqa: BLE001
            push({"type": "warning", "message": str(err)})
        return {"handled": True}

    if cmd == "/context":
        led = session.ledger
        pct = led.usage_fraction * 100
        rows = [
            f"Context: {led.total_tokens:,} / {led.limit:,} tokens ({pct:.1f}%) · system prefix: {led.system_tokens:,} tok",
        ]
        for s in led.segments:
            pin = "📌" if s.pinned else "  "
            rows.append(f"  [{s.id:>3}] {pin} {s.type:<10} {s.role:<9} {s.tokens:>7,} tok  {s.label}")
        push({"type": "info", "text": "\n".join(rows)})
        return {"handled": True}

    if cmd == "/tps":
        pf = session.perf
        if not pf.turns:
            push({"type": "info", "text": "No turns recorded yet."})
            return {"handled": True}
        rows = [f"Throughput ({pf.sparkline(10)}):"]
        for t in pf.turns[-15:]:
            ttft_s = f"{t.ttft:.2f}s" if t.ttft is not None else "—"
            rows.append(
                f"  turn #{t.turn:<2}  {t.model:<24}  {t.tps:>6.1f} tok/s  ttft {ttft_s:<6}  "
                f"out {t.out_tokens:>5}  ctx {t.context_in:>7,}"
            )
        push({"type": "info", "text": "\n".join(rows)})
        return {"handled": True}

    if cmd == "/artifacts":
        if len(args) >= 3 and args[0].lower() == "save":
            try:
                aid = int(args[1])
                dest = session.sandbox.resolve_for_write(args[2])
                saved = session.artifacts.save_to(aid, dest)
                push({"type": "notice", "message": f"Saved artifact #{aid} -> {saved}"})
            except Exception as err:  # noqa: BLE001
                push({"type": "warning", "message": str(err)})
            return {"handled": True}

        if not session.artifacts.items:
            push({"type": "info", "text": "No artifacts emitted in this session yet."})
            return {"handled": True}
        rows = ["Session Artifacts (`/artifacts save <id> <path>`):"]
        for a in session.artifacts.items:
            rows.append(f"  #{a.id:<2} [{a.lang}] {a.lines} lines — {a.title}")
        push({"type": "info", "text": "\n".join(rows)})
        return {"handled": True}

    if cmd == "/compact":
        res = await compact_ledger(session.ledger)
        push(
            {
                "type": "notice",
                "message": (
                    f"Compacted: {res['before_tokens']:,} -> {res['after_tokens']:,} tokens "
                    f"(freed {res['freed_tokens']:,}; evicted={res['evicted']}, summarized={res['summarized']})"
                ),
            }
        )
        return {"handled": True}

    if cmd == "/keys":
        save_flag = "--save" in args
        clean = [a for a in args if a != "--save"]
        if len(clean) >= 2:
            try:
                res = set_provider_key(session, clean[0], clean[1], save=save_flag)
                loc = f" (saved to {res['saved_to']})" if res["saved_to"] else " (session only; add --save to persist)"
                push({"type": "notice", "message": f"Key set for {res['provider']}: {res['masked']}{loc}"})
            except Exception as err:  # noqa: BLE001
                push({"type": "warning", "message": str(err)})
            return {"handled": True}

        rows = ["Configured Provider Keys (`/keys <provider> <key> [--save]`):"]
        for k in key_state(session):
            src = f" ({k['var']} from {k['from']})" if k["configured"] else ""
            rows.append(f"  {k['provider']:<12} {k['masked']}{src}")
        push({"type": "info", "text": "\n".join(rows)})
        return {"handled": True}

    if cmd == "/env":
        if args:
            target_blk = None if args[0].lower() in ("off", "none") else args[0]
            try:
                st = switch_profile(session, target_blk)
                push(
                    {
                        "type": "notice",
                        "message": f"Switched .env block -> {st['active'] or '(preamble only)'} "
                        f"({session.cfg.provider}/{session.cfg.model.id})",
                    }
                )
            except Exception as err:  # noqa: BLE001
                push({"type": "warning", "message": str(err)})
            return {"handled": True}

        st = profile_state(session)
        if not st["blocks"]:
            push({"type": "info", "text": f"No `#---#` provider blocks in {st['file']}"})
            return {"handled": True}
        rows = [f"Provider blocks in {st['file']} (`/env <name>` or `/env off`):"]
        for b in st["blocks"]:
            mark = "●" if b["enabled"] else "○"
            rows.append(f"  {mark} {b['name']} {b['note']}")
        push({"type": "info", "text": "\n".join(rows)})
        return {"handled": True}

    if cmd == "/prompts":
        from davag.agent.prompt import PROMPT_SPECS, builtin_prompts, prompt_dirs
        from davag.agent.promptfork import check_forks, hash_text, snapshot_prompt_file, stamp_fork
        from davag.session.promptedit import export_prompts_to_project, stage_prompt_apply

        dirs = prompt_dirs(session.cfg.home, session.cfg.cwd)
        forks = check_forks(sources=session.prompt_sources, layers=session.prompt_layers)
        sub = args[0].lower() if args else ""
        flags = args[1:]

        if not sub:
            def _drift_of(k: str) -> str:
                if any(s["key"] == k for s in forks["stale"]):
                    return "STALE"
                if any(u["key"] == k for u in forks["unstamped"]):
                    return "unstamped"
                return "ok"

            lines = ["Prompts (highest wins: built-in -> home -> project):"]
            for s in PROMPT_SPECS:
                lyr = session.prompt_layers.get(s.key, "default")
                where = "built-in" if lyr == "default" else session.prompt_sources.get(s.key, "")
                lines.append(f"  {s.key:<12} {lyr:<9} {_drift_of(s.key):<10} {where}")
            lines.extend(
                [
                    "",
                    f"  home     {dirs['home']}",
                    f"  project  {dirs['project']}",
                    "",
                    "  /prompts export [--force]   write resolved prompts (incl. protocol.md tool table) to ./.prompts/",
                    "  /prompts apply              re-read disk prompts & rebuild system prefix (resets prompt cache)",
                    "  /prompts diff <key>         compare your override against current built-in",
                    "  /prompts accept <key|all>   re-baseline override against today's built-in",
                    "  /prompts reset <key>        remove override and revert to built-in",
                ]
            )
            push({"type": "info", "text": "\n".join(lines)})
            return {"handled": True}

        if sub == "export":
            force = "--force" in flags
            try:
                exp = export_prompts_to_project(session, force=force)
                push(
                    {
                        "type": "notice",
                        "message": f"Exported {exp['count']} prompts to {exp['dir']}/ — edit any `.md` file then run `/prompts apply` (or relaunch).",
                    }
                )
            except Exception as err:  # noqa: BLE001
                push({"type": "warning", "message": str(err)})
            return {"handled": True}

        if sub == "apply":
            try:
                res = stage_prompt_apply(session)
                if res.get("applied"):
                    push(
                        {
                            "type": "notice",
                            "message": f"Rebuilt system prompt ({res['before_tokens']:,} -> {res['after_tokens']:,} tok; prompt cache reset).",
                        }
                    )
                else:
                    push({"type": "notice", "message": "Staged prompt rebuild for next step boundary."})
            except Exception as err:  # noqa: BLE001
                push({"type": "warning", "message": str(err)})
            return {"handled": True}

        if sub in ("diff", "accept", "reset"):
            known = [s.key for s in PROMPT_SPECS]
            key = flags[0] if flags else ""
            if sub == "accept" and key == "all":
                drifted = [*forks["stale"], *forks["unstamped"]]
                if not drifted:
                    push({"type": "notice", "message": "Every override is already current."})
                    return {"handled": True}
                by_dir: dict[str, list[str]] = {}
                for f in drifted:
                    by_dir.setdefault(f["dir"], []).append(f["key"])
                for d_str, ks in by_dir.items():
                    stamp_fork(Path(d_str), ks)
                push({"type": "notice", "message": f"Re-baselined {', '.join(f['key'] for f in drifted)}."})
                return {"handled": True}

            if key not in known:
                push({"type": "warning", "message": f"usage: /prompts {sub} <{'|'.join(known)}>"})
                return {"handled": True}
            if session.prompt_layers.get(key, "default") == "default":
                push({"type": "notice", "message": f"{key} is using the built-in default (not overridden)."})
                return {"handled": True}

            file_str = session.prompt_sources[key]
            defaults = builtin_prompts()
            if sub == "diff":
                push(
                    {
                        "type": "info",
                        "text": (
                            f"{key} ({file_str}, built-in hash {hash_text(defaults[key])})\n\n"
                            f"--- current built-in ---\n{defaults[key]}\n\n"
                            f"--- yours ---\n{session.prompts.get(key, '')}"
                        ),
                    }
                )
            elif sub == "accept":
                stamp_fork(Path(file_str).parent, [key])
                push({"type": "notice", "message": f"{key} re-baselined against current built-in."})
            elif sub == "reset":
                snap = snapshot_prompt_file(Path(file_str))
                Path(file_str).unlink(missing_ok=True)
                stage_prompt_apply(session)
                push({"type": "notice", "message": f"Reset {key} to built-in (backup: {snap})."})
            return {"handled": True}

        push({"type": "warning", "message": "usage: /prompts [export [--force] | apply | diff <key> | accept <key|all> | reset <key>]"})
        return {"handled": True}

    if cmd == "/config":
        import json

        push({"type": "info", "text": json.dumps(session.cfg.to_public_dict(), indent=2)})
        return {"handled": True}

    if cmd == "/sessions":
        rows_meta = list_sessions(Path(session.cfg.home), limit=15)
        if not rows_meta:
            push({"type": "info", "text": "No past sessions yet."})
            return {"handled": True}
        lines = ["Recent Sessions (`davag --resume <id>`):"]
        for s in rows_meta:
            t = s.get("totals") or {}
            lines.append(
                f"  {s.get('id')}  {s.get('started_at')}  {s.get('model')}  "
                f"{t.get('turns', 0)}t  ${float(t.get('cost', 0.0)):.4f}"
            )
        push({"type": "info", "text": "\n".join(lines)})
        return {"handled": True}

    if cmd == "/history":
        limit_n = int(args[0]) if args and args[0].isdigit() else 15
        entries = session.history.recent(limit_n)
        if not entries:
            push({"type": "info", "text": "No prompt history yet."})
            return {"handled": True}
        lines = ["Recent Prompts (`/replay <index>`):"]
        base_idx = len(session.history.entries) - len(entries)
        for offset, e in enumerate(entries):
            lines.append(f"  [{base_idx + offset}] {e.get('text', '')[:90]}")
        push({"type": "info", "text": "\n".join(lines)})
        return {"handled": True}

    if cmd == "/replay":
        if not args:
            push({"type": "notice", "message": "usage: /replay <index> (e.g. /replay -1)"})
            return {"handled": True}
        try:
            idx = int(args[0])
            prompt_str = session.history.entries[idx]["text"]
            return {"handled": False, "replay_input": str(prompt_str)}
        except (ValueError, IndexError):
            push({"type": "warning", "message": f"Invalid history index: {args[0]}"})
            return {"handled": True}

    if cmd == "/web":
        from davag.ui.clipboard import copy_to_clipboard, open_in_browser

        web_info = getattr(session, "web_info", None)
        if not web_info:
            push(
                {
                    "type": "notice",
                    "message": "Web GUI is not running in this session (relaunch with `davag --web` or set `DAVAG_WEB=true` in ~/.davag/.env).",
                }
            )
            return {"handled": True}

        sub = args[0].lower() if args else "status"
        if sub == "open":
            ok = open_in_browser(web_info["auth_url"])
            push({"type": "notice", "message": f"Opened {web_info['url']} in browser." if ok else f"URL: {web_info['auth_url']}"})
        elif sub in ("copy", "url"):
            target_url = web_info.get("lan_auth_url") or web_info["auth_url"]
            ok = copy_to_clipboard(target_url)
            push({"type": "notice", "message": "Copied tokenized Web URL to clipboard!" if ok else f"URL: {target_url}"})
        elif sub == "token":
            ok = copy_to_clipboard(web_info["token"])
            push({"type": "notice", "message": "Copied Web bearer token to clipboard!" if ok else f"Token: {web_info['token']}"})
        else:
            lines = [f"Web GUI (Ctrl+Click): {web_info['auth_url']}"]
            if web_info.get("lan_auth_url"):
                lines.append(f"LAN URL (Ctrl+Click): {web_info['lan_auth_url']}")
            lines.append("Commands: `/web open` (Alt+O) · `/web copy` (Alt+C) · `/web token` (Alt+T)")
            push({"type": "info", "text": "\n".join(lines)})
        return {"handled": True}

    if cmd == "/events":
        lines = ["Harness Events (emitted / unretired):"]
        for ev_name, count in sorted(session.agent.event_counts.items()):
            unret = session.agent.unretired_events.get(ev_name, 0)
            lines.append(f"  {ev_name:<18} emitted={count:<4} unretired={unret}")
        push({"type": "info", "text": "\n".join(lines)})
        return {"handled": True}

    push({"type": "warning", "message": f"Unknown command: {cmd} (type /help for commands)"})
    return {"handled": True}