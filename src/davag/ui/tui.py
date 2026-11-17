"""Interactive Terminal TUI REPL (`prompt_toolkit` + `rich`).

Uses `prompt_toolkit.shortcuts.print_formatted_text(ANSI(...))` rather than `patch_stdout`
so Rich panels, colors, and streamed lines render natively through prompt_toolkit's Win32/VT
renderer with:
- ZERO `?[90m` raw-escape artifacts
- ZERO clipped left margins on wrapped lines
- Live 10fps animated spinner + elapsed timer + live partial-line stream preview!
"""

from __future__ import annotations

import asyncio
import html
import io
import json
import shutil
import textwrap
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import CompleteEvent, Completer, Completion
from prompt_toolkit.document import Document
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import ANSI, HTML
from prompt_toolkit.history import History
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.shortcuts import print_formatted_text
from rich.console import Console, Group
from rich.panel import Panel
from rich.syntax import Syntax
from rich.text import Text

_APPROVAL_FOOTER = (
    "Press [bold green]y[/] allow · [bold cyan]c[/] checkpoint timeline & allow · "
    "[bold red]n[/] deny  [dim](single key — no Enter needed)[/]"
)


def _format_approval_body(op_dict: dict[str, Any]) -> Group:
    """Format gated ops cleanly — dedicated line-numbered Python view for `resolve.eval`, bash for `shell`."""
    op_name = str(op_dict.get("op", ""))
    parts: list[Any] = []

    if op_name == "resolve.eval":
        header = Text()
        header.append("resolve.eval", style="bold bright_cyan")
        desc = str(op_dict.get("description") or "").strip()
        if desc:
            header.append(f" — {desc}", style="bold bright_white")
        parts.append(header)

        extra = {k: v for k, v in op_dict.items() if k not in ("op", "code", "description")}
        if extra:
            parts.append(
                Syntax(
                    json.dumps(extra, ensure_ascii=False),
                    "json",
                    theme="monokai",
                    background_color="default",
                    word_wrap=True,
                )
            )

        raw_code = str(op_dict.get("code") or "")
        clean_code = textwrap.dedent(raw_code).strip("\n") or "# (empty)"
        parts.append(
            Panel(
                Syntax(
                    clean_code,
                    "python",
                    theme="monokai",
                    background_color="default",
                    line_numbers=True,
                    word_wrap=True,
                    indent_guides=True,
                    padding=(0, 1),
                ),
                title="[dim cyan]python · live resolve / fusion[/]",
                title_align="left",
                border_style="bright_black",
                padding=(0, 0),
            )
        )
        parts.append(Text.from_markup(_APPROVAL_FOOTER))
        return Group(*parts)

    if op_name == "shell":
        header = Text()
        header.append("shell", style="bold bright_cyan")
        if op_dict.get("cwd"):
            header.append(f"  (cwd: {op_dict['cwd']})", style="dim")
        parts.append(header)
        clean_cmd = textwrap.dedent(str(op_dict.get("cmd") or "")).strip("\n")
        parts.append(
            Panel(
                Syntax(
                    clean_cmd,
                    "bash",
                    theme="monokai",
                    background_color="default",
                    line_numbers=True,
                    word_wrap=True,
                    padding=(0, 1),
                ),
                title="[dim cyan]command[/]",
                title_align="left",
                border_style="bright_black",
                padding=(0, 0),
            )
        )
        parts.append(Text.from_markup(_APPROVAL_FOOTER))
        return Group(*parts)

    envelope: dict[str, Any] = {}
    payload_blocks: list[tuple[str, str]] = []
    for k, v in op_dict.items():
        if isinstance(v, str) and ("\n" in v or (k in ("code", "cmd", "html", "text") and len(v) > 96)):
            envelope[k] = f"@{k}"
            payload_blocks.append((k, v))
        else:
            envelope[k] = v

    parts.append(
        Syntax(
            json.dumps(envelope, indent=2, ensure_ascii=False),
            "json",
            theme="monokai",
            background_color="default",
            word_wrap=True,
        )
    )

    for field_name, raw_body in payload_blocks:
        lexer = "python" if field_name == "code" else ("bash" if field_name == "cmd" else ("html" if field_name == "html" else "text"))
        clean_body = textwrap.dedent(raw_body).strip("\n")
        parts.append(Text(f"--davag:{field_name}--", style="bold cyan"))
        parts.append(
            Syntax(
                clean_body,
                lexer,
                theme="monokai",
                background_color="default",
                word_wrap=True,
                line_numbers=True,
            )
        )
        parts.append(Text("--davag:end--", style="bold cyan"))

    parts.append(Text(""))
    parts.append(Text.from_markup(_APPROVAL_FOOTER))
    return Group(*parts)

from davag import __version__
from davag.context.utf8 import sanitize_utf8_str
from davag.server.http import RunningWebServer
from davag.session.bootstrap import Session
from davag.session.dispatch import create_dispatcher
from davag.ui.clipboard import copy_to_clipboard, open_in_browser
from davag.ui.commands import command_list


class DavagCompleter(Completer):
    def __init__(self, cwd: Path) -> None:
        self.cwd = cwd
        self._cmds = command_list()

    def get_completions(
        self, document: Document, complete_event: CompleteEvent
    ) -> Iterable[Completion]:
        _ = complete_event
        text = document.text_before_cursor

        if text.startswith("/"):
            first = text.split()[0] if text.strip() else "/"
            if " " not in text:
                for c in self._cmds:
                    if c["name"].startswith(first.lower()):
                        yield Completion(
                            c["name"],
                            start_position=-len(first),
                            display_meta=c["desc"][:52],
                        )
            return

        at_idx = text.rfind("@")
        if at_idx >= 0 and (at_idx == 0 or text[at_idx - 1].isspace()):
            frag = text[at_idx + 1 :]
            if " " not in frag:
                parent_rel = Path(frag).parent if "/" in frag or "\\" in frag else Path(".")
                prefix = Path(frag).name.lower()
                search_dir = (self.cwd / parent_rel).resolve()
                if search_dir.is_dir():
                    try:
                        for child in sorted(search_dir.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))[:40]:
                            if child.name.startswith("."):
                                continue
                            if child.name.lower().startswith(prefix):
                                rel_str = str(child.relative_to(self.cwd)).replace("\\", "/")
                                if child.is_dir():
                                    rel_str += "/"
                                yield Completion(rel_str, start_position=-len(frag))
                    except OSError:
                        return


class DavagPtHistory(History):
    """Bridges `~/.davag/history.jsonl` into prompt_toolkit's Up/Down arrow history."""

    def __init__(self, store: Any) -> None:
        super().__init__()
        self._store = store

    def load_history_strings(self) -> Iterable[str]:
        return list(reversed(self._store.strings()))

    def store_string(self, string: str) -> None:
        self._store.add(string)


def _context_bar(fraction: float, width: int = 8) -> str:
    filled = min(width, max(0, round(fraction * width)))
    return "█" * filled + "░" * (width - filled)


def _pt_print(renderable: Any = "") -> None:
    """Render any Rich markup string, Text, or Panel into ANSI and print natively via prompt_toolkit."""
    cols = max(60, shutil.get_terminal_size((100, 24)).columns - 1)
    buf = io.StringIO()
    c = Console(
        file=buf,
        force_terminal=True,
        color_system="truecolor",
        highlight=False,
        width=cols,
        legacy_windows=False,
    )
    c.print(renderable)
    out = buf.getvalue()
    out = out.removesuffix("\n")
    print_formatted_text(ANSI(out))


async def run_tui_loop(
    *,
    session: Session,
    web_server: RunningWebServer | None = None,
    initial_input: str = "",
) -> int:
    spinner_frames = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")
    should_exit = False
    pending_approvals: list[dict[str, Any]] = []
    in_thinking = False
    streamed_text_this_turn = False
    think_buf = ""
    text_buf = ""
    toolbar_flash: str = ""
    activity_label: str = ""
    activity_started: float = 0.0
    slash_busy: bool = False
    pt_history = DavagPtHistory(session.history)

    def _set_activity(label: str) -> None:
        nonlocal activity_label, activity_started
        activity_label = label
        if label:
            activity_started = time.monotonic()

    def _spin_glyph() -> str:
        idx = int(time.monotonic() * 10) % len(spinner_frames)
        return spinner_frames[idx]

    if web_server is not None:
        session.web_info = {  # type: ignore[attr-defined]
            "url": web_server.url,
            "auth_url": web_server.auth_url,
            "lan_auth_url": web_server.lan_auth_url,
            "token": web_server.token,
        }

    def _flash_notice(msg: str) -> None:
        nonlocal toolbar_flash
        toolbar_flash = msg

        async def _clear_later() -> None:
            nonlocal toolbar_flash
            await asyncio.sleep(3.0)
            if toolbar_flash == msg:
                toolbar_flash = ""

        asyncio.create_task(_clear_later())

    def _request_exit() -> None:
        nonlocal should_exit
        should_exit = True
        session.agent.cancel()

    def _flush_think_buf(*, force: bool = False) -> None:
        nonlocal think_buf
        while "\n" in think_buf:
            line, think_buf = think_buf.split("\n", 1)
            _pt_print(Text(f"  💭 {line}" if line else "", style="dim italic"))
        if force and think_buf:
            _pt_print(Text(f"  💭 {think_buf}", style="dim italic"))
            think_buf = ""

    def _flush_text_buf(*, force: bool = False) -> None:
        nonlocal text_buf
        while "\n" in text_buf:
            line, text_buf = text_buf.split("\n", 1)
            _pt_print(Text(line))
        if force and text_buf:
            _pt_print(Text(text_buf))
            text_buf = ""

    def _render_entry(entry: dict[str, Any], *, from_web: bool = False) -> None:
        etype = entry.get("type", "info")
        if etype == "user":
            txt = str(entry.get("text", ""))
            if from_web:
                _pt_print(f"\n[bold bright_white]❯ [web][/] {txt}")
            elif txt.startswith("[steer]"):
                _pt_print(f"[magenta]  ↳ {txt}[/]")
        elif etype in ("notice", "info"):
            msg = entry.get("message") or entry.get("text") or ""
            _pt_print(f"[cyan]  · {msg}[/]")
        elif etype == "warning":
            _pt_print(f"[yellow]  ! {entry.get('message', '')}[/]")
        elif etype == "error":
            _pt_print(f"[bold red]  ✗ {entry.get('message', '')}[/]")
        elif etype == "shell":
            code = entry.get("exit_code", 0)
            color = "green" if code == 0 else "red"
            _pt_print(f"[{color}]  $ {entry.get('cmd')} (exit {code})[/]")
            if entry.get("output"):
                _pt_print(Text(str(entry["output"]), style="dim"))

    submit = create_dispatcher(
        session=session,
        local_push=lambda entry: _render_entry(entry, from_web=False) if entry.get("origin") != "web" else None,
        on_exit=_request_exit,
    )

    def _on_bus_event(ev: dict[str, Any]) -> None:
        if ev["type"] == "transcript" and isinstance(ev["data"], dict) and ev["data"].get("origin") == "web":
            _render_entry(ev["data"], from_web=True)
            if ev["data"].get("type") == "user" and ev["data"].get("text"):
                pt_history.append_string(str(ev["data"]["text"]))
        elif ev["type"] == "approval-resolved":
            ap_id = ev["data"].get("id")
            before = len(pending_approvals)
            pending_approvals[:] = [p for p in pending_approvals if p.get("id") != ap_id]
            if len(pending_approvals) < before:
                dec = ev["data"].get("decision") or {}
                allow = bool(dec.get("allow"))
                verdict = "allowed" if allow else "denied"
                _pt_print(f"[dim]  ✓ approval {ap_id} {verdict} (web)[/]")
                _set_activity(
                    "waiting for approval [y/c/n]…"
                    if pending_approvals
                    else ("running approved op…" if allow else "processing denial…")
                )

    session.bus.subscribe(_on_bus_event)

    def _on_turn_start(_p: Any) -> None:
        nonlocal in_thinking, streamed_text_this_turn, think_buf, text_buf
        in_thinking = False
        streamed_text_this_turn = False
        think_buf = ""
        text_buf = ""
        _set_activity(f"thinking ({session.cfg.model.id})…")

    def _on_thinking(delta: str) -> None:
        nonlocal in_thinking, activity_label, think_buf
        if activity_label != "thinking…":
            activity_label = "thinking…"
        if not session.cfg.thinking_visible or not delta:
            return
        in_thinking = True
        think_buf += delta
        if "\n" in think_buf:
            _flush_think_buf(force=False)

    def _on_text(delta: str) -> None:
        nonlocal in_thinking, streamed_text_this_turn, activity_label, text_buf
        activity_label = "streaming response…"
        if in_thinking:
            _flush_think_buf(force=True)
            in_thinking = False
        if not streamed_text_this_turn:
            streamed_text_this_turn = True
        text_buf += delta
        if "\n" in text_buf:
            _flush_text_buf(force=False)

    def _on_turn_end(p: dict[str, Any]) -> None:
        nonlocal in_thinking
        if in_thinking:
            _flush_think_buf(force=True)
            in_thinking = False
        _flush_text_buf(force=True)
        _set_activity("processing turn…")
        tps = session.perf.live_tps
        ttft = p.get("ttft")
        ttft_s = f" · ttft {ttft:.2f}s" if ttft is not None else ""
        if tps > 0:
            _pt_print(f"[dim]{tps:.1f} tok/s{ttft_s}[/]")

    def _on_ops_parsed(ops: list[dict[str, Any]]) -> None:
        names = ", ".join(o["op"] for o in ops[:2])
        if len(ops) > 2:
            names += f" (+{len(ops) - 2})"
        _set_activity(f"running {names}…")
        for o in ops:
            _pt_print(f"[bright_cyan]  · {o['op']}[/]")

    def _on_ops_result(res: dict[str, Any]) -> None:
        _set_activity("preparing next turn…")
        if res.get("checkpoint"):
            _pt_print(f"[dim]  ✓ checkpoint: {res['checkpoint']} (/undo to restore)[/]")
        for err in res.get("errors") or []:
            _pt_print(f"[red]  ✗ [{err.get('index')}] {err.get('op')}: {err.get('message')}[/]")

    def _on_artifact(art: dict[str, Any]) -> None:
        _pt_print(f"[dim]  📄 artifact #{art['id']} [{art['lang']}] ({art['lines']} lines) — /artifacts[/]")

    def _on_approval_request(req: dict[str, Any]) -> None:
        pending_approvals.append(req)
        _set_activity("waiting for approval [y/c/n]…")
        _pt_print(
            Panel(
                _format_approval_body(req["op"]),
                title=f"▲ Approval Needed ({req.get('id', 'ap')})",
                border_style="yellow",
            )
        )

    session.agent.on("turn-start", _on_turn_start)
    session.agent.on("thinking", _on_thinking)
    session.agent.on("text", _on_text)
    session.agent.on("turn-end", _on_turn_end)
    session.agent.on("ops-parsed", _on_ops_parsed)
    session.agent.on("ops-result", _on_ops_result)
    session.agent.on("artifact", _on_artifact)
    session.agent.on("approval-request", _on_approval_request)
    session.agent.on("usage", lambda _u: None)
    session.agent.on(
        "compact-start",
        lambda _p: (_set_activity("compacting context…"), _pt_print("[dim]  ⟳ compacting context…[/]")),
    )
    session.agent.on(
        "compact-done",
        lambda p: _pt_print(f"[dim]  ✓ compacted (freed {p.get('freed_tokens', 0):,} tok)[/]"),
    )
    session.agent.on("nudge", lambda _p: _pt_print("[dim]  ↻ nudging model for dr_ops…[/]"))
    session.agent.on(
        "retry",
        lambda p: (
            _set_activity(f"retrying provider (#{p.get('attempt')})…"),
            _pt_print(f"[yellow]  ↻ retrying provider (#{p.get('attempt')})…[/]"),
        ),
    )
    session.agent.on("steer-queued", lambda p: _pt_print(f"[magenta]  ↳ steer queued ({p.get('queued')})[/]"))
    session.agent.on("steered", lambda _p: _pt_print("[magenta]  ↳ steer applied[/]"))
    session.agent.on(
        "prompts-applied",
        lambda p: _pt_print(f"[cyan]  ✓ prompts rebuilt ({p.get('after_tokens', 0):,} tok; cache reset)[/]"),
    )
    session.agent.on(
        "repair",
        lambda p: _pt_print(f"[yellow]  ⚙ repairing dr_ops syntax (#{p.get('attempt')})…[/]"),
    )
    session.agent.on("warning", lambda p: _pt_print(f"[yellow]  ! {p.get('message', p)}[/]"))
    session.agent.on(
        "cancelled",
        lambda _p: (_set_activity(""), _pt_print("[yellow]  ■ turn cancelled[/]")),
    )
    session.agent.on(
        "error",
        lambda p: (_set_activity(""), _pt_print(f"[bold red]  ✗ {p.get('message', p)}[/]")),
    )
    session.agent.on("done", lambda _p: _set_activity(""))

    # Startup banner with Ctrl+Click tokenized URLs
    res_badge = (
        f"[green]connected ({session.transport.mode})[/]"
        if session.transport is not None
        else f"[yellow]disconnected ({session.resolve_connect_note or 'run /resolve'})[/]"
    )
    ro_note = (
        f" [dim](ro: {', '.join(str(p) for p in session.cfg.ro_dirs)})[/]"
        if session.cfg.ro_dirs
        else ""
    )
    resumed_from = session.log.meta.get("resumed_from")
    banner_lines = [
        f"[bold]davag {__version__}[/] · [cyan]{session.cfg.provider}/{session.cfg.model.id}[/] (effort={session.cfg.effort})",
        f"Workspace: [bold]{session.cfg.cwd}[/]{ro_note}"
        + (f" · [dim]resumed {resumed_from}[/]" if resumed_from else ""),
        f"DaVinci Resolve: {res_badge}",
    ]
    if web_server is not None:
        banner_lines.append(f"Web GUI (Ctrl+Click): [bold cyan]{web_server.auth_url}[/]")
        if web_server.lan_auth_url:
            banner_lines.append(f"LAN URL (Ctrl+Click): [bold cyan]{web_server.lan_auth_url}[/]")
        banner_lines.append(
            "[dim]Web shortcuts: `Alt+O` open browser · `Alt+C` copy URL · `Alt+T` copy token · `/web`[/]"
        )

        if session.cfg.web_clipboard:
            target_u = web_server.lan_auth_url or web_server.auth_url
            if copy_to_clipboard(target_u):
                banner_lines.append("[green]  ✓ Copied tokenized Web URL to clipboard (DAVAG_WEB_CLIPBOARD=true)[/]")
        if session.cfg.web_open and open_in_browser(web_server.auth_url):
            banner_lines.append("[green]  ✓ Opened Web GUI in default browser (DAVAG_WEB_OPEN=true)[/]")

    banner_lines.append("[dim]Type `/help` for commands, `@file` to attach media, `Esc` to cancel running turn.[/]")
    _pt_print(Panel("\n".join(banner_lines), border_style="bright_black", expand=False))

    kb = KeyBindings()

    @kb.add("escape")
    def _esc_cancel(_event: Any) -> None:
        if session.agent.busy:
            session.agent.cancel()

    @kb.add("escape", "c")  # Alt+C
    def _alt_c_copy_url(event: Any) -> None:
        if web_server is not None:
            u = web_server.lan_auth_url or web_server.auth_url
            if copy_to_clipboard(u):
                _flash_notice("✓ copied Web URL!")
                event.app.invalidate()

    @kb.add("escape", "t")  # Alt+T
    def _alt_t_copy_tok(event: Any) -> None:
        if web_server is not None and copy_to_clipboard(web_server.token):
            _flash_notice("✓ copied token!")
            event.app.invalidate()

    @kb.add("escape", "o")  # Alt+O
    def _alt_o_open(event: Any) -> None:
        if web_server is not None and open_in_browser(web_server.auth_url):
            _flash_notice("✓ opened browser!")
            event.app.invalidate()

    def _is_working() -> bool:
        return bool(session.agent.busy or slash_busy or activity_label)

    def _toolbar() -> HTML:
        cfg = session.cfg
        frac = session.ledger.usage_fraction
        pct = round(frac * 100)
        bar = _context_bar(frac)
        st = session.agent.stats
        res_mode = session.transport.mode if session.transport else "offline"
        spark = session.perf.sparkline(5)
        tps = session.perf.live_tps
        tps_part = f" · {tps:.0f} tok/s {spark}" if tps > 0 else ""
        yolo_part = ' · <style bg="ansired" fg="ansiwhite"> YOLO </style>' if cfg.yolo else ""
        web_part = " · [Alt+O open · Alt+C copy URL]" if web_server is not None and not _is_working() else ""
        flash_part = f' · <style fg="ansigreen"><b>{html.escape(toolbar_flash)}</b></style>' if toolbar_flash else ""
        busy_part = ""
        if _is_working():
            elapsed = max(0.0, time.monotonic() - activity_started) if activity_started else 0.0
            lbl = html.escape(activity_label or "working…")
            busy_part = (
                f' · <style fg="ansiyellow"><b>{_spin_glyph()} {lbl} ({elapsed:.1f}s)</b></style>'
                ' <style fg="ansibrightblack">[type to steer · Esc cancel]</style>'
            )
        return HTML(
            f" <b>{html.escape(cfg.model.id)}</b>/{cfg.effort} · [{res_mode}] · {bar} {pct}% "
            f"· {st['turns']}t {st['ops']}ops · ${st['cost']:.3f}{tps_part}{yolo_part}{web_part}{busy_part}{flash_part}"
        )

    def _prompt_message() -> HTML:
        cols = max(40, shutil.get_terminal_size((100, 24)).columns - 12)
        preview = ""
        if text_buf:
            tail = html.escape(text_buf[-cols:])
            preview = f"{tail}\n"
        elif think_buf and session.cfg.thinking_visible:
            tail = html.escape(think_buf[-cols:])
            preview = f'<style fg="ansibrightblack"><i>  💭 {tail}</i></style>\n'

        if pending_approvals:
            return HTML(f'{preview}<style fg="ansiyellow"><b>▲ approve [y/c/n] ❯ </b></style>')
        if _is_working():
            return HTML(f'{preview}<style fg="ansicyan"><b>{_spin_glyph()}</b></style> <b>❯ </b>')
        return HTML("<b>❯ </b>")

    from davag.server.http import install_win32_proactor_reset_guard, is_benign_socket_disconnect

    install_win32_proactor_reset_guard()

    prompt_session: PromptSession[str] = PromptSession(
        history=pt_history,
        completer=DavagCompleter(session.cfg.cwd),
        key_bindings=kb,
        bottom_toolbar=_toolbar,
        refresh_interval=0.12,
    )

    def _resolve_next_approval(choice: str) -> None:
        if not pending_approvals:
            return
        ap = pending_approvals.pop(0)
        ap_id = ap.get("id")
        label = ap_id or "ap"
        allow = choice in ("y", "yes", "c", "checkpoint")
        cp_first = choice in ("c", "checkpoint")
        if cp_first:
            _pt_print(f"[cyan]  ✓ approval {label}: checkpointing timeline & allowing[/]")
        elif allow:
            _pt_print(f"[green]  ✓ approval {label}: allowed[/]")
        else:
            _pt_print(f"[red]  ✗ approval {label}: denied[/]")
        _set_activity(
            "waiting for approval [y/c/n]…"
            if pending_approvals
            else ("running approved op…" if allow else "processing denial…")
        )
        if ap_id:
            session.bus.resolve_approval(ap_id, {"allow": allow, "checkpoint_first": cp_first})
        else:
            ap["respond"]({"allow": allow, "checkpoint_first": cp_first})

    _approval_hotkey = Condition(
        lambda: bool(pending_approvals) and not prompt_session.default_buffer.text
    )
    for _key_ch in ("y", "Y", "c", "C", "n", "N"):

        @kb.add(_key_ch, filter=_approval_hotkey)
        def _on_instant_approval(event: Any, ch: str = _key_ch.lower()) -> None:
            _resolve_next_approval(ch)
            event.app.invalidate()

    # Never let prompt_toolkit's default `Application._handle_exception` freeze the CLI/Web event loop
    # with `Press ENTER to continue...` when a browser aborts a TCP socket or a background callback errs!
    def _nonblocking_loop_exception_handler(_loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
        exc = context.get("exception")
        if is_benign_socket_disconnect(exc):
            return
        msg = str(exc or context.get("message") or "event loop error")
        _pt_print(f"[dim yellow]  ⚠ async loop warning: {html.escape(msg)}[/]")

    prompt_session.app._handle_exception = _nonblocking_loop_exception_handler  # type: ignore[method-assign]

    active_tasks: set[asyncio.Task[Any]] = set()

    async def _safe_submit(line: str) -> None:
        nonlocal slash_busy
        is_cmd = line.strip().startswith(("/", "sh ")) or line.strip() == "sh"
        if is_cmd:
            slash_busy = True
            _set_activity(f"running {line.strip().split()[0]}…")
        elif not session.agent.busy:
            _set_activity("sending prompt…")
        try:
            await submit(line, "tui")
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001
            _pt_print(f"[bold red]  ✗ {type(err).__name__}: {err}[/]")
        finally:
            if is_cmd:
                slash_busy = False
                if not session.agent.busy:
                    _set_activity("")

    def _spawn_submit(line: str) -> None:
        t = asyncio.create_task(_safe_submit(line))
        active_tasks.add(t)
        t.add_done_callback(active_tasks.discard)

    if initial_input.strip():
        _pt_print(f"\n[bold bright_white]❯[/] {initial_input.strip()}")
        _spawn_submit(initial_input.strip())

    while not should_exit:
        try:
            line = sanitize_utf8_str(await prompt_session.prompt_async(_prompt_message))
        except KeyboardInterrupt:
            if session.agent.busy:
                session.agent.cancel()
                continue
            break
        except (EOFError, asyncio.CancelledError):
            break

        trimmed = line.strip()
        if not trimmed:
            continue

        if pending_approvals and trimmed.lower() in ("y", "yes", "n", "no", "c", "checkpoint"):
            _resolve_next_approval(trimmed.lower())
            continue

        if line.count("\n") >= 4:
            token_str, item = session.pastes.add(line)
            _pt_print(f"[dim]  folded {item.lines}-line paste as {token_str}[/]")
            _spawn_submit(token_str)
        else:
            _spawn_submit(trimmed)

    if active_tasks:
        for t in list(active_tasks):
            t.cancel()
        await asyncio.gather(*active_tasks, return_exceptions=True)

    return 0