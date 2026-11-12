"""Bootstraps `Session`, optional `RunningWebServer` (`--web` / `--host` / `--open`), and `run_tui_loop`."""

from __future__ import annotations

import asyncio

from davag.cli import assert_safe_bind
from davag.server.http import RunningWebServer, start_web_server
from davag.session.bootstrap import bootstrap_session
from davag.session.dispatch import create_dispatcher

from .tui import run_tui_loop


async def _start_async(
    *,
    initial_input: str = "",
    cwd: str | None = None,
    overrides: dict[str, str] | None = None,
    resume: str | bool = False,
    accept_stale_prompts: bool = False,
) -> int:
    session = await bootstrap_session(
        cwd=cwd,
        overrides=overrides,
        resume=resume,
        accept_stale_prompts=accept_stale_prompts,
    )
    web_srv: RunningWebServer | None = None
    try:
        if session.cfg.web_enabled:
            assert_safe_bind(host=session.cfg.web_host, yolo=session.cfg.yolo)
            web_submit = create_dispatcher(session=session)
            web_srv = await start_web_server(
                session=session,
                submit_fn=web_submit,
                host=session.cfg.web_host,
                port=session.cfg.web_port,
                token=session.cfg.web_token,
            )

        return await run_tui_loop(
            session=session,
            web_server=web_srv,
            initial_input=initial_input,
        )
    finally:
        if web_srv is not None:
            await web_srv.close()
        await session.close()


def start_tui(
    *,
    initial_input: str = "",
    cwd: str | None = None,
    overrides: dict[str, str] | None = None,
    resume: str | bool = False,
    accept_stale_prompts: bool = False,
) -> int:
    return asyncio.run(
        _start_async(
            initial_input=initial_input,
            cwd=cwd,
            overrides=overrides,
            resume=resume,
            accept_stale_prompts=accept_stale_prompts,
        )
    )