"""davag CLI entry point.

Modeled on davai/bin/davai.js:
- parse_args(argv) raises UsageError on bad input (exits 2 in run()) so tests can assert cleanly.
- --host or --port implies --web.
- Binding off-loopback while --yolo is enabled is refused.
"""

from __future__ import annotations

import ipaddress
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from . import __version__
from .config import discover_resolve, load_config, load_settings, resolve_home, settings_to_defaults

HELP = f"""davag {__version__} — DaVinci Resolve AI editing agent

USAGE
  davag                          start the interactive TUI REPL
  davag "rough cut interview"    one-shot prompt, then exit (or start REPL w/ prompt)
  echo "list timelines" | davag  read the request from stdin
  davag --host 0.0.0.0           TUI REPL + Web GUI bound on LAN
  davag --resume [id]            reopen a past session (default: most recent)

OPTIONS
  -p, --print                one-shot, stream plain text to stdout (no TUI)
      --json                 one-shot, emit newline-delimited JSON events
  -y, --yes                  auto-approve gated ops (headless only)
      --yolo                 auto-approve gated ops in the REPL too (/yolo toggles)
  -m, --model <id>           override the model for this run
      --provider <name>      anthropic | gemini | openai | grok | openrouter
      --effort <level>       low | medium | high | xhigh | max
      --role <role>          reviewer | logger | editor | finisher | master
      --transport <mode>     auto | direct | bridge
  -C, --cwd <dir>            run against a different working directory
      --web                  also serve the session over HTTP (default 127.0.0.1:7879)
      --open                 open the tokenized Web GUI URL in your browser on launch (implies --web)
      --port <n>             port for --web (implies --web; 0 picks a free port)
      --host <addr>          bind address for --web (implies --web; refuses --yolo off-loopback)
      --doctor               inspect DaVinci Resolve paths, FFmpeg, motion backends, and API keys
      --install-bridge       install davag_bridge.py into Resolve's Workspace -> Scripts menu
      --models [provider]    list models the provider serves
      --sessions             list recent sessions
      --resume [id]          rebuild context from a past session; id may be a prefix
      --config               show resolved configuration (keys redacted)
      --accept-prompts       start even if a prompt override was forked from an older davag
  -h, --help                 this text
  -v, --version

CONFIG
  Reads $DAVAG_HOME/settings.json, ~/.davai/.env (keys fallback), $DAVAG_HOME/.env,
  ./.env, then OS environment. DAVAG_HOME defaults to ~/.davag
"""


class UsageError(Exception):
    """Raised on invalid CLI invocation; maps to exit code 2."""


@dataclass
class ParsedArgs:
    help: bool = False
    version: bool = False
    print_mode: bool = False
    json_mode: bool = False
    yes: bool = False
    accept_stale_prompts: bool = False
    cwd: str | None = None
    web: bool = False
    port: int | None = None
    host: str | None = None
    doctor: bool = False
    install_bridge: bool = False
    list_models: bool = False
    list_sessions: bool = False
    resume: str | bool = False
    show_config: bool = False
    overrides: dict[str, str] = field(default_factory=dict)
    words: list[str] = field(default_factory=list)
    input_text: str = ""


def is_loopback_host(host: str) -> bool:
    h = host.strip().lower()
    if h == "localhost":
        return True
    if h.startswith("[") and h.endswith("]"):
        h = h[1:-1]
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


def assert_safe_bind(*, host: str, yolo: bool) -> None:
    if yolo and not is_loopback_host(host):
        raise UsageError(
            f'Refusing to combine --host "{host}" with --yolo: binding off-loopback while '
            "auto-approving shell/eval ops exposes unauthenticated remote execution."
        )


def parse_args(argv: list[str]) -> ParsedArgs:
    out = ParsedArgs()
    i = 0
    n = len(argv)

    def next_val(flag: str) -> str:
        nonlocal i
        i += 1
        if i >= n:
            raise UsageError(f"{flag} requires a value")
        return argv[i]

    while i < n:
        a = argv[i]
        if a in ("-h", "--help"):
            out.help = True
        elif a in ("-v", "--version"):
            out.version = True
        elif a in ("-p", "--print"):
            out.print_mode = True
        elif a == "--json":
            out.json_mode = True
            out.print_mode = True
        elif a in ("-y", "--yes"):
            out.yes = True
        elif a == "--accept-prompts":
            out.accept_stale_prompts = True
        elif a == "--yolo":
            out.overrides["DAVAG_YOLO"] = "true"
            out.yes = True
        elif a in ("-m", "--model"):
            out.overrides["DAVAG_MODEL"] = next_val(a)
        elif a == "--provider":
            out.overrides["DAVAG_PROVIDER"] = next_val(a)
        elif a == "--effort":
            out.overrides["DAVAG_EFFORT"] = next_val(a)
        elif a == "--role":
            out.overrides["DAVAG_ROLE"] = next_val(a)
        elif a == "--transport":
            out.overrides["DAVAG_TRANSPORT"] = next_val(a)
        elif a in ("-C", "--cwd"):
            out.cwd = next_val(a)
        elif a == "--web":
            if i + 1 < n and argv[i + 1].isdigit():
                raise UsageError(f"--web takes no port argument. Use: --web --port {argv[i + 1]}")
            out.web = True
            out.overrides["DAVAG_WEB"] = "true"
        elif a == "--open":
            out.web = True
            out.overrides["DAVAG_WEB"] = "true"
            out.overrides["DAVAG_WEB_OPEN"] = "true"
        elif a == "--port":
            v = next_val(a)
            if not v.isdigit() or int(v) > 65535:
                raise UsageError(f'--port must be a number between 0 and 65535 (got "{v}")')
            out.port = int(v)
            out.web = True
            out.overrides["DAVAG_WEB"] = "true"
            out.overrides["DAVAG_WEB_PORT"] = v
        elif a == "--host":
            h = next_val(a)
            out.host = h
            out.web = True
            out.overrides["DAVAG_WEB"] = "true"
            out.overrides["DAVAG_WEB_HOST"] = h
        elif a == "--web-host":
            raise UsageError("--web-host is --host")
        elif a == "--doctor":
            out.doctor = True
        elif a == "--install-bridge":
            out.install_bridge = True
        elif a == "--models":
            out.list_models = True
            if i + 1 < n and not argv[i + 1].startswith("-"):
                out.overrides["DAVAG_PROVIDER"] = next_val(a)
        elif a == "--sessions":
            out.list_sessions = True
        elif a == "--resume":
            if i + 1 < n and not argv[i + 1].startswith("-"):
                out.resume = next_val(a)
            else:
                out.resume = True
        elif a == "--config":
            out.show_config = True
        elif a.startswith("-"):
            raise UsageError(f"unknown option: {a}\n\n{HELP}")
        else:
            out.words.append(a)
        i += 1

    out.input_text = " ".join(out.words)
    return out


def _build_config(args: ParsedArgs):
    home = resolve_home()
    defaults = settings_to_defaults(load_settings(home))
    cfg = load_config(cwd=args.cwd, defaults=defaults, overrides=args.overrides, home=home)
    if cfg.web_enabled:
        assert_safe_bind(host=cfg.web_host, yolo=cfg.yolo)
    return cfg


async def _probe_live_resolve(cfg) -> dict[str, object]:
    from .transport import TransportConnectionError, connect_resolve

    try:
        tr = await connect_resolve(
            mode=cfg.transport,
            home=cfg.home,
            bridge_host=cfg.bridge_host,
            bridge_port=cfg.bridge_port,
        )
    except TransportConnectionError as err:
        return {"connected": False, "error": str(err)}

    try:
        status = await tr.call("resolve.status", timeout=5.0)
        return {"connected": True, "status": status}
    except Exception as err:  # noqa: BLE001
        return {"connected": False, "error": str(err)}
    finally:
        await tr.close()


def _print_doctor(cfg) -> int:
    import asyncio

    from .media_intel.hwaccel import detect_gpu_capabilities
    from .media_intel.motion import probe_motion_capabilities

    inst = discover_resolve()
    gpu = detect_gpu_capabilities(inst.ffmpeg_path)
    motion = probe_motion_capabilities()
    bridge_script = Path(inst.user_scripts_utility_dir) / "davag_bridge.py"
    live = asyncio.run(_probe_live_resolve(cfg)) if inst.resolve_running else {"connected": False}

    gpu_label = ", ".join(gpu.gpu_names) if gpu.gpu_names else "none detected"
    qsv_str = f"Intel QSV ({', '.join(gpu.qsv_encoders)})" if gpu.qsv_encoders else ""
    nvenc_str = f"NVENC ({', '.join(gpu.nvenc_encoders)})" if gpu.nvenc_encoders else ""
    hw_encoders = " · ".join(x for x in (qsv_str, nvenc_str) if x) or "software only"
    cdp_label = (
        f"ready ({motion['browser_path']}, warm daemon + <4ms auto_clip)"
        if motion["cdp_ready"]
        else "missing (install Chrome or Microsoft Edge)"
    )

    lines = [
        f"davag {__version__} — environment & DaVinci Resolve check",
        "",
        "Python, GPU & Media Tools",
        f"  python              {inst.python_version} ({inst.python_arch_bits}-bit) on {inst.platform}",
        f"  gpu hardware        {gpu_label}",
        f"  hw media encoders   {hw_encoders} (preferred proxy: {gpu.preferred_proxy_encoder})",
        f"  ffmpeg              {inst.ffmpeg_path or 'missing (install ffmpeg for proxy video & silence detection)'}",
        f"  ffprobe             {inst.ffprobe_path or 'missing'}",
        "",
        "Motion Overlay Engine",
        f"  svg fast path       {motion['svg_status']}",
        f"  canvas fast path    {motion['canvas_status']}",
        f"  html/css cdp pool   {cdp_label}",
        f"  node runtime        {motion['node_path'] or 'not found'}",
        f"  alpha codecs        {', '.join(motion['codecs']) if inst.ffmpeg_path else 'requires ffmpeg'}",
        "",
        "DaVinci Resolve",
        f"  resolve executable  {inst.resolve_exe_path or 'not found in default path'}",
        f"  resolve process     {'RUNNING' if inst.resolve_running else 'not running'}",
        f"  fusionscript lib    {inst.script_lib_path or 'not found'}",
        f"  scripting api dir   {inst.script_api_dir or 'not found'}",
        f"  dvr python module   {inst.dvr_module_file or 'not found'}",
        f"  direct ipc ready    {'yes (Studio external scripting)' if inst.direct_ready else 'no (use Bridge mode or set RESOLVE_SCRIPT_LIB)'}",
        f"  bridge launcher     {'installed (' + str(bridge_script) + ')' if bridge_script.is_file() else 'not installed (run `davag --install-bridge`)'}",
    ]

    if live.get("connected"):
        st = live["status"]  # type: ignore[index]
        assert isinstance(st, dict)
        proj = st.get("project") or {}
        tl = st.get("timeline") or {}
        proj_str = proj.get("name") or "(none)"
        tl_str = (
            f"{tl.get('name')} @ {tl.get('playhead_tc')} ({proj.get('fps')} fps)"
            if tl
            else "(no active timeline)"
        )
        lines.extend(
            [
                f"  live connection     CONNECTED via [{st.get('mode')}] -> {st.get('product')} {st.get('version')}",
                f"  active page         {st.get('page') or 'unknown'}",
                f"  open project        {proj_str}",
                f"  active timeline     {tl_str}",
            ]
        )
    elif inst.resolve_running:
        err_first = str(live.get("error", "unreachable")).splitlines()[0]
        lines.append(f"  live connection     not connected ({err_first})")

    lines.extend(
        [
            "",
            "Providers & Keys",
            f"  active              {cfg.provider} / {cfg.model.id} (effort={cfg.effort}, video={'yes' if cfg.model.video else 'no'})",
        ]
    )
    for prov, key_val in cfg.keys.items():
        if key_val:
            origin = cfg.key_origins.get(prov, {})
            src = origin.get("from") or "unknown"
            var = origin.get("name") or ""
            lines.append(f"  {prov:<18}  configured ({var} from {src})")
        else:
            lines.append(f"  {prov:<18}  —")
    if cfg.warnings:
        lines.append("")
        lines.append("Warnings")
        for w in cfg.warnings:
            lines.append(f"  ! {w}")
    sys.stdout.write("\n".join(lines) + "\n")
    return 0


def _ensure_utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconf = getattr(stream, "reconfigure", None)
        if callable(reconf):
            try:
                reconf(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                continue


def main(argv: list[str] | None = None) -> int:
    _ensure_utf8_stdio()
    args = parse_args(sys.argv[1:] if argv is None else argv)

    if args.help:
        sys.stdout.write(HELP)
        return 0
    if args.version:
        sys.stdout.write(f"{__version__}\n")
        return 0

    cfg = _build_config(args)

    if args.show_config:
        sys.stdout.write(json.dumps(cfg.to_public_dict(), indent=2) + "\n")
        return 0

    if args.doctor:
        return _print_doctor(cfg)

    if args.list_models:
        import asyncio

        from .config.models import MODELS
        from .providers import list_remote_models

        try:
            ids = asyncio.run(list_remote_models(cfg))
        except Exception:  # noqa: BLE001 — fall back to curated seeds when offline
            ids = [m.id for m in MODELS if m.provider == cfg.provider]
        sys.stdout.write("\n".join(ids) + ("\n" if ids else ""))
        return 0

    if args.list_sessions:
        from .session.log import list_sessions

        rows = list_sessions(Path(cfg.home))
        if not rows:
            sys.stdout.write("no sessions yet\n")
            return 0
        for s in rows:
            t = s.get("totals") or {}
            sys.stdout.write(
                f"{s.get('id')}  {s.get('started_at')}  {s.get('model')}  "
                f"{t.get('turns', 0)} turns  ${float(t.get('cost', 0.0)):.4f}  {s.get('cwd')}\n"
            )
        return 0

    if args.install_bridge:
        from .bridge.installer import install_bridge_script

        installed = install_bridge_script(port=cfg.bridge_port)
        sys.stdout.write(
            f"Installed davag Resolve bridge script:\n  {installed}\n\n"
            "To start it inside DaVinci Resolve (Free or Studio):\n"
            "  Workspace -> Scripts -> Utility -> davag_bridge\n"
        )
        return 0

    from .headless import read_piped_stdin, run_headless_async

    piped = read_piped_stdin() if not args.input_text else ""
    combined_input = "\n".join(x for x in (args.input_text, piped) if x)

    interactive = (
        not args.print_mode
        and sys.stdout is not None
        and sys.stdin is not None
        and sys.stdout.isatty()
        and sys.stdin.isatty()
    )

    if not interactive:
        if not combined_input:
            sys.stderr.write("nothing to do: pass a request or pipe one in\n")
            return 2
        import asyncio

        return asyncio.run(
            run_headless_async(
                combined_input,
                json_mode=args.json_mode,
                yes=args.yes,
                cwd=args.cwd,
                overrides=args.overrides,
                resume=args.resume,
                accept_stale_prompts=args.accept_stale_prompts,
            )
        )

    # Interactive TUI + optional Web Sidecar (Phase 5 wires start_tui; if invoked before Phase 5, run headless if prompt passed)
    try:
        from .ui.start import start_tui
    except ImportError:
        if combined_input:
            import asyncio

            return asyncio.run(
                run_headless_async(
                    combined_input,
                    json_mode=args.json_mode,
                    yes=args.yes,
                    cwd=args.cwd,
                    overrides=args.overrides,
                    resume=args.resume,
                    accept_stale_prompts=args.accept_stale_prompts,
                )
            )
        sys.stdout.write(f"davag {__version__} ready (interactive TUI launching in Phase 5).\n")
        return 0

    return start_tui(
        initial_input=combined_input,
        cwd=args.cwd,
        overrides=args.overrides,
        resume=args.resume,
        accept_stale_prompts=args.accept_stale_prompts,
    )


def run() -> None:
    try:
        code = main()
        raise SystemExit(code)
    except UsageError as err:
        sys.stderr.write(f"{err}\n")
        raise SystemExit(2) from None
    except Exception as err:  # noqa: BLE001 — top-level CLI error boundary
        sys.stderr.write(f"\n{err}\n")
        if os.environ.get("DAVAG_DEBUG"):
            import traceback

            traceback.print_exc()
        raise SystemExit(1) from None


if __name__ == "__main__":
    run()