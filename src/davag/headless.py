"""Headless runner (`davag -p "..."`, `davag --json "..."`, and piped stdin).

Consumes every declared event in `davag.agent.loop.EVENTS` without importing any TUI modules.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from .session.bootstrap import bootstrap_session


def read_piped_stdin() -> str:
    if sys.stdin is None or sys.stdin.isatty():
        return ""
    try:
        return sys.stdin.read().strip()
    except OSError:
        return ""


async def run_headless_async(
    prompt: str,
    *,
    json_mode: bool = False,
    yes: bool = False,
    cwd: str | None = None,
    overrides: dict[str, str] | None = None,
    resume: str | bool = False,
    accept_stale_prompts: bool = False,
) -> int:
    eff_overrides = dict(overrides or {})
    if yes:
        eff_overrides["DAVAG_YOLO"] = "true"

    session = await bootstrap_session(
        cwd=cwd,
        overrides=eff_overrides,
        resume=resume,
        accept_stale_prompts=accept_stale_prompts,
    )
    exit_code = 0

    def _emit_ndjson(ev_type: str, payload: Any) -> None:
        sys.stdout.write(json.dumps({"event": ev_type, "data": payload}, ensure_ascii=False) + "\n")
        sys.stdout.flush()

    if json_mode:
        for ev_name in (
            "turn-start",
            "thinking",
            "text",
            "turn-end",
            "usage",
            "ops-parsed",
            "ops-result",
            "artifact",
            "compact-start",
            "compact-done",
            "nudge",
            "retry",
            "steer-queued",
            "steered",
            "prompts-applied",
            "repair",
            "warning",
            "cancelled",
            "done",
        ):
            session.agent.on(ev_name, lambda p, n=ev_name: _emit_ndjson(n, p))

        def _on_err_json(p: Any) -> None:
            nonlocal exit_code
            exit_code = 1
            _emit_ndjson("error", p)

        session.agent.on("error", _on_err_json)
    else:
        # Plain streaming text mode (`-p` / `--print`)
        session.agent.on("text", lambda delta: (sys.stdout.write(str(delta)), sys.stdout.flush()))
        session.agent.on(
            "ops-parsed",
            lambda ops: sys.stderr.write(
                "  · " + ", ".join(str(o["op"]) for o in ops) + "\n"
            ),
        )
        session.agent.on(
            "warning",
            lambda w: sys.stderr.write(f"  ! {w.get('message', w)}\n"),
        )
        session.agent.on(
            "retry",
            lambda r: sys.stderr.write(f"  ~ retry #{r.get('attempt')} in {r.get('delay')}s\n"),
        )
        # Register lightweight no-op listeners for non-visual lifecycle events so unretired_events stays 0
        for silent_ev in (
            "turn-start",
            "thinking",
            "turn-end",
            "usage",
            "ops-result",
            "artifact",
            "compact-start",
            "compact-done",
            "nudge",
            "steer-queued",
            "steered",
            "prompts-applied",
            "repair",
            "cancelled",
            "done",
        ):
            session.agent.on(silent_ev, lambda _p: None)

        def _on_err_plain(p: Any) -> None:
            nonlocal exit_code
            exit_code = 1
            msg = p.get("message", p) if isinstance(p, dict) else p
            sys.stderr.write(f"\nerror: {msg}\n")

        session.agent.on("error", _on_err_plain)

    # Headless approval handler if `--yes` / `--yolo` was not passed
    def _on_approval(req: dict[str, Any]) -> None:
        if session.cfg.yolo:
            req["respond"]({"allow": True})
        else:
            req["respond"](
                {
                    "allow": False,
                    "reason": "headless mode denied gated op (pass -y / --yolo to allow)",
                }
            )

    session.agent.on("approval-request", _on_approval)

    try:
        out = await session.agent.run(prompt)
        if not json_mode and out.get("status") == "done":
            sys.stdout.write("\n")
            sys.stdout.flush()
    finally:
        await session.close()

    return exit_code