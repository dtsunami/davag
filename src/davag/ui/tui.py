"""Interactive Terminal TUI REPL (`prompt_toolkit` + `rich`).

Features:
- Enters `patch_stdout(raw=True)` BEFORE constructing `rich.Console()` so every streamed delta,
  panel, and notice goes cleanly through prompt_toolkit's stdout proxy without clipping lines!
- Ctrl+Clickable tokenized Web GUI URLs (`http://127.0.0.1:7879/?token=...` + LAN IP URL).
- Hotkeys: `Alt+C` copies tokenized URL, `Alt+T` copies raw token, `Alt+O` opens browser,
  `Esc` cancels running turn.
- Supports `DAVAG_WEB_OPEN=true` and `DAVAG_WEB_CLIPBOARD=true` in `~/.davag/.env`.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import CompleteEvent, Completer, Completion
from prompt_toolkit.document import Document
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.patch_stdout import patch_stdout
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from davag import __version__
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
        text = document.text_before_cursor
        if text.startswith("/") and " " not in text:
            low = text.lower()
            for c in self._cmds:
                if c["name"].startswith(low):
                    yield Completion(
                        c["name"],
                        start_position=-len(text),
                        display=c["usage"],
                        display_meta=c["desc"],
                    )
            return

        word = document.get_word_before_cursor(WORD=True)
        if word.startswith("@"):
            rel_prefix = word[1:]
            base_dir = self.cwd
            prefix_dir = ""
            name_part = rel_prefix
            if "/" in rel_prefix or "\\" in rel_prefix:
                norm = rel_prefix.replace("\\", "/")
                dir_part, name_part = norm.rsplit("/", 1)
                cand_dir = (self.cwd / dir_part).resolve()
                if cand_dir.is_dir():
                    base_dir = cand_dir
                    prefix_dir = dir_part + "/"
            try:
                for child in sorted(base_dir.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
                    if child.name.startswith(".") and not name_part.startswith("."):
                        continue
                    if child.name.lower().startswith(name_part.lower()):
                        suffix = "/" if child.is_dir() else ""
                        full_ref = f"@{prefix_dir}{child.name}{suffix}"
                        yield Completion(
                            full_ref,
                            start_position=-len(word),
                            display=f"{child.name}{suffix}",
                        )
            except OSError:
                return


def _context_bar(fraction: float, width: int = 8) -> str:
    filled = min(width, max(0, round(fraction * width)))
    return "█" * filled + "░" * (width - filled)


async def run_tui_loop(
    *,
    session: Session,
    web_server: RunningWebServer | None = None,
    initial_input: str = "",
) -> int:
    should_exit = False
    pending_approvals: list[dict[str, Any]] = []
    in_thinking = False
    streamed_text_this_turn = False
    toolbar_flash: str = ""

    if web_server is not None:
        session.web_info = {  # type: ignore[attr-defined]
            "url": web_server.url,
            "auth_url": web_server.auth_url,
            "lan_auth_url": web_server.lan_auth_url,
            "token": web_server.token,
        }

    # Enter patch_stdout FIRST so Console() binds to prompt_toolkit's StdoutProxy!
    with patch_stdout(raw=True):
        console = Console(highlight=False, soft_wrap=True)

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

        def _render_entry(entry: dict[str, Any], *, from_web: bool = False) -> None:
            etype = entry.get("type", "info")
            if etype == "user":
                # Local TUI prompts were already echoed by PromptSession; only print Web prompts or steers!
                txt = str(entry.get("text", ""))
                if from_web:
                    console.print(f"\n[bold bright_white]❯ [web][/] {txt}")
                elif txt.startswith("[steer]"):
                    console.print(f"[magenta]  ↳ {txt}[/]")
            elif etype in ("notice", "info"):
                msg = entry.get("message") or entry.get("text") or ""
                console.print(f"[cyan]  · {msg}[/]")
            elif etype == "warning":
                console.print(f"[yellow]  ! {entry.get('message', '')}[/]")
            elif etype == "error":
                console.print(f"[bold red]  ✗ {entry.get('message', '')}[/]")
            elif etype == "shell":
                code = entry.get("exit_code", 0)
                color = "green" if code == 0 else "red"
                console.print(f"[{color}]  $ {entry.get('cmd')} (exit {code})[/]")
                if entry.get("output"):
                    console.print(Text(str(entry["output"]), style="dim"))

        submit = create_dispatcher(
            session=session,
            local_push=lambda entry: _render_entry(entry, from_web=False) if entry.get("origin") != "web" else None,
            on_exit=_request_exit,
        )

        def _on_bus_event(ev: dict[str, Any]) -> None:
            if ev["type"] == "transcript" and isinstance(ev["data"], dict) and ev["data"].get("origin") == "web":
                _render_entry(ev["data"], from_web=True)
            elif ev["type"] == "approval-resolved":
                ap_id = ev["data"].get("id")
                before = len(pending_approvals)
                pending_approvals[:] = [p for p in pending_approvals if p.get("id") != ap_id]
                if len(pending_approvals) < before:
                    dec = ev["data"].get("decision") or {}
                    verdict = "allowed" if dec.get("allow") else "denied"
                    console.print(f"[dim]  ✓ approval {ap_id} {verdict}[/]")

        session.bus.subscribe(_on_bus_event)

        def _on_turn_start(_p: Any) -> None:
            nonlocal in_thinking, streamed_text_this_turn
            in_thinking = False
            streamed_text_this_turn = False

        def _on_thinking(delta: str) -> None:
            nonlocal in_thinking
            if not session.cfg.thinking_visible or not delta:
                return
            if not in_thinking:
                in_thinking = True
                console.print("[dim italic]  💭 ", end="")
            console.print(Text(delta, style="dim italic"), end="")

        def _on_text(delta: str) -> None:
            nonlocal in_thinking, streamed_text_this_turn
            if in_thinking:
                in_thinking = False
                console.print()
            if not streamed_text_this_turn:
                streamed_text_this_turn = True
                console.print()
            console.print(Text(delta), end="")

        def _on_turn_end(p: dict[str, Any]) -> None:
            nonlocal in_thinking
            if in_thinking or streamed_text_this_turn:
                console.print()
            in_thinking = False
            tps = session.perf.live_tps
            ttft = p.get("ttft")
            ttft_s = f" · ttft {ttft:.2f}s" if ttft is not None else ""
            if tps > 0:
                console.print(f"[dim]{tps:.1f} tok/s{ttft_s}[/]")

        def _on_ops_parsed(ops: list[dict[str, Any]]) -> None:
            for o in ops:
                console.print(f"[bright_cyan]  · {o['op']}[/]")

        def _on_ops_result(res: dict[str, Any]) -> None:
            if res.get("checkpoint"):
                console.print(f"[dim]  ✓ checkpoint: {res['checkpoint']} (/undo to restore)[/]")
            for err in res.get("errors") or []:
                console.print(f"[red]  ✗ [{err.get('index')}] {err.get('op')}: {err.get('message')}[/]")

        def _on_artifact(art: dict[str, Any]) -> None:
            console.print(f"[dim]  📄 artifact #{art['id']} [{art['lang']}] ({art['lines']} lines) — /artifacts[/]")

        def _on_approval_request(req: dict[str, Any]) -> None:
            pending_approvals.append(req)
            op_json = json.dumps(req["op"], indent=2, ensure_ascii=False)
            console.print(
                Panel(
                    f"{op_json}\n\n[bold yellow]Type `y` to allow, `c` to checkpoint timeline & allow, or `n` to deny[/]",
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
        session.agent.on("compact-start", lambda _p: console.print("[dim]  ⟳ compacting context…[/]"))
        session.agent.on("compact-done", lambda p: console.print(f"[dim]  ✓ compacted (freed {p.get('freed_tokens', 0):,} tok)[/]"))
        session.agent.on("nudge", lambda _p: console.print("[dim]  ↻ nudging model for dr_ops…[/]"))
        session.agent.on("retry", lambda p: console.print(f"[yellow]  ↻ retrying provider (#{p.get('attempt')})…[/]"))
        session.agent.on("steer-queued", lambda p: console.print(f"[magenta]  ↳ steer queued ({p.get('queued')})[/]"))
        session.agent.on("steered", lambda _p: console.print("[magenta]  ↳ steer applied[/]"))
        session.agent.on("prompts-applied", lambda p: console.print(f"[cyan]  ✓ prompts rebuilt ({p.get('after_tokens', 0):,} tok; cache reset)[/]"))
        session.agent.on("repair", lambda p: console.print(f"[yellow]  ⚙ repairing dr_ops syntax (#{p.get('attempt')})…[/]"))
        session.agent.on("warning", lambda p: console.print(f"[yellow]  ! {p.get('message', p)}[/]"))
        session.agent.on("cancelled", lambda _p: console.print("\n[yellow]  ■ turn cancelled[/]"))
        session.agent.on("error", lambda p: console.print(f"\n[bold red]  ✗ {p.get('message', p)}[/]"))
        session.agent.on("done", lambda _p: None)

        # Startup banner with Ctrl+Click tokenized URLs
        res_badge = (
            f"[green]connected ({session.transport.mode})[/]"
            if session.transport is not None
            else f"[yellow]disconnected ({session.resolve_connect_note or 'run /resolve'})[/]"
        )
        banner_lines = [
            f"[bold]davag {__version__}[/] · [cyan]{session.cfg.provider}/{session.cfg.model.id}[/] (effort={session.cfg.effort})",
            f"DaVinci Resolve: {res_badge}",
        ]
        if web_server is not None:
            banner_lines.append(
                f"Web GUI (Ctrl+Click): [bold cyan]{web_server.auth_url}[/]"
            )
            if web_server.lan_auth_url:
                banner_lines.append(
                    f"LAN URL (Ctrl+Click): [bold cyan]{web_server.lan_auth_url}[/]"
                )
            banner_lines.append(
                "[dim]Web shortcuts: `Alt+O` open browser · `Alt+C` copy URL · `Alt+T` copy token · `/web`[/]"
            )

            # Honor .env auto-copy / auto-open preferences!
            if session.cfg.web_clipboard:
                target_u = web_server.lan_auth_url or web_server.auth_url
                if copy_to_clipboard(target_u):
                    banner_lines.append("[green]  ✓ Copied tokenized Web URL to clipboard (DAVAG_WEB_CLIPBOARD=true)[/]")
            if session.cfg.web_open and open_in_browser(web_server.auth_url):
                banner_lines.append("[green]  ✓ Opened Web GUI in default browser (DAVAG_WEB_OPEN=true)[/]")

        banner_lines.append("[dim]Type `/help` for commands, `@file` to attach media, `Esc` to cancel running turn.[/]")
        console.print(Panel("\n".join(banner_lines), border_style="bright_black", expand=False))

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
            web_part = " · [Alt+O open · Alt+C copy URL]" if web_server is not None and not session.agent.busy else ""
            flash_part = f' · <style fg="ansigreen"><b>{toolbar_flash}</b></style>' if toolbar_flash else ""
            busy_part = " · [thinking… type to steer, Esc to cancel]" if session.agent.busy else ""
            return HTML(
                f" <b>{cfg.model.id}</b>/{cfg.effort} · [{res_mode}] · {bar} {pct}% "
                f"· {st['turns']}t {st['ops']}ops · ${st['cost']:.3f}{tps_part}{yolo_part}{web_part}{busy_part}{flash_part}"
            )

        prompt_session: PromptSession[str] = PromptSession(
            completer=DavagCompleter(session.cfg.cwd),
            key_bindings=kb,
            bottom_toolbar=_toolbar,
        )

        active_tasks: set[asyncio.Task[Any]] = set()

        def _spawn_submit(line: str) -> None:
            t = asyncio.create_task(submit(line, "tui"))
            active_tasks.add(t)
            t.add_done_callback(active_tasks.discard)

        if initial_input.strip():
            console.print(f"\n[bold bright_white]❯[/] {initial_input.strip()}")
            _spawn_submit(initial_input.strip())

        while not should_exit:
            try:
                prompt_glyph = "▲ approve [y/c/n] ❯ " if pending_approvals else "❯ "
                line = await prompt_session.prompt_async(prompt_glyph)
            except KeyboardInterrupt:
                if session.agent.busy:
                    session.agent.cancel()
                    continue
                break
            except EOFError:
                break

            trimmed = line.strip()
            if not trimmed:
                continue

            if pending_approvals and trimmed.lower() in ("y", "yes", "n", "no", "c", "checkpoint"):
                ap = pending_approvals.pop(0)
                ap_id = ap.get("id")
                allow = trimmed.lower() in ("y", "yes", "c", "checkpoint")
                cp_first = trimmed.lower() in ("c", "checkpoint")
                if ap_id:
                    session.bus.resolve_approval(ap_id, {"allow": allow, "checkpoint_first": cp_first})
                else:
                    ap["respond"]({"allow": allow, "checkpoint_first": cp_first})
                continue

            if line.count("\n") >= 4:
                token_str, item = session.pastes.add(line)
                console.print(f"[dim]  folded {item.lines}-line paste as {token_str}[/]")
                _spawn_submit(token_str)
            else:
                _spawn_submit(trimmed)

        if active_tasks:
            for t in list(active_tasks):
                t.cancel()
            await asyncio.gather(*active_tasks, return_exceptions=True)

    return 0