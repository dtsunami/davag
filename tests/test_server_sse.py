"""End-to-end HTTP + SSE Web Sidecar integration test (`davag.server.http`).

Verifies:
- Static `/` shell loads without auth, while `/api/*` rejects missing/bad tokens with 401.
- `GET /api/events` streams `hello` + live turn events + `Last-Event-ID` gap-free replay.
- `POST /api/resolve/grab` exports a viewer PNG and serves it over `/api/media/still/*`.
- `POST /api/submit` + `POST /api/approve` drives a complete multi-turn Resolve edit over HTTP/SSE.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import aiohttp
import pytest

from davag.providers import StreamEvent
from davag.server.http import start_web_server
from davag.session import bootstrap_session
from davag.session.dispatch import create_dispatcher
from davag.transport import DirectTransport

from .fake_resolve import FakeResolve


class _WebTestProvider:
    name = "anthropic"
    model = "claude-opus-5"

    def __init__(self) -> None:
        self.call_count = 0

    async def send(
        self, *, system: str, messages: list[dict[str, Any]], cancel_event: Any = None
    ) -> AsyncIterator[StreamEvent]:
        self.call_count += 1
        if self.call_count == 1:
            yield StreamEvent(
                t="text",
                delta='```dr_ops\n{"ops": [{"op": "resolve.eval", "code": "result = 7 * 6"}]}\n```',
            )
        else:
            yield StreamEvent(t="text", delta="Eval returned 42.")
        yield StreamEvent(t="usage", in_fresh=500, out=40, context_in=500)
        yield StreamEvent(t="stop", reason="end_turn")

    async def count_tokens(self, *, system: str, messages: list[dict[str, Any]]) -> int | None:
        return None


async def test_web_sidecar_auth_sse_replay_grab_and_approval(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    cwd = tmp_path / "cwd"
    home.mkdir()
    cwd.mkdir()
    monkeypatch.setenv("DAVAG_HOME", str(home))
    monkeypatch.setenv("DAVAI_HOME", str(tmp_path / "davai_empty"))

    fake = FakeResolve()
    tr = DirectTransport(resolve_obj=fake)
    prov = _WebTestProvider()

    session = await bootstrap_session(
        cwd=cwd,
        make_provider_fn=lambda _cfg, **_kw: prov,
        injected_transport=tr,
        auto_connect_resolve=False,
    )
    submit_fn = create_dispatcher(session=session)
    srv = await start_web_server(
        session=session,
        submit_fn=submit_fn,
        host="127.0.0.1",
        port=0,
        token="test-secret-token",
    )

    assert srv.auth_url == f"{srv.url}/?token=test-secret-token"
    headers = {"Authorization": "Bearer test-secret-token"}
    try:
        async with aiohttp.ClientSession() as client:
            # 1. Index HTML is public; /api/session without token is 401
            async with client.get(f"{srv.url}/") as r_idx:
                assert r_idx.status == 200
                assert "davag" in await r_idx.text()

            async with client.get(f"{srv.url}/api/session") as r_unauth:
                assert r_unauth.status == 401

            async with client.get(f"{srv.url}/api/session", headers=headers) as r_sess:
                assert r_sess.status == 200
                s_data = await r_sess.json()
                assert s_data["resolve_connected"] is True

            # 2. Grab viewer frame via REST & fetch PNG bytes
            async with client.post(
                f"{srv.url}/api/resolve/grab", headers=headers, json={"attach": True}
            ) as r_grab:
                assert r_grab.status == 200
                g_data = await r_grab.json()
                still_url = g_data["url"]

            async with client.get(f"{srv.url}{still_url}?token=test-secret-token") as r_png:
                assert r_png.status == 200
                raw_png = await r_png.read()
                assert raw_png.startswith(b"\x89PNG")

            # 3. Connect SSE stream, submit prompt that triggers `approval-request`, approve via REST!
            seen_events: list[tuple[str, Any, str | None]] = []
            done_ev = asyncio.Event()

            async def _sse_reader() -> None:
                async with client.get(f"{srv.url}/api/events?token=test-secret-token") as sse:
                    cur_event = ""
                    cur_id: str | None = None
                    async for raw_line in sse.content:
                        line = raw_line.decode("utf-8").rstrip("\r\n")
                        if line.startswith("id: "):
                            cur_id = line[4:]
                        elif line.startswith("event: "):
                            cur_event = line[7:]
                        elif line.startswith("data: "):
                            payload = json.loads(line[6:])
                            seen_events.append((cur_event, payload, cur_id))
                            if cur_event == "approval-request":
                                await client.post(
                                    f"{srv.url}/api/approve",
                                    headers=headers,
                                    json={"id": payload["id"], "allow": True},
                                )
                            elif cur_event == "done":
                                done_ev.set()
                                return

            reader_task = asyncio.create_task(_sse_reader())
            await asyncio.sleep(0.05)

            async with client.post(
                f"{srv.url}/api/submit",
                headers=headers,
                json={"text": "compute 7 * 6 in resolve"},
            ) as r_sub:
                assert r_sub.status == 200

            await asyncio.wait_for(done_ev.wait(), timeout=5.0)
            await reader_task

            names = [e[0] for e in seen_events]
            assert "hello" in names
            assert "approval-request" in names
            assert "approval-resolved" in names
            assert "done" in names

            # 4. Verify Last-Event-ID reconnect replays missed events accurately
            async with client.get(
                f"{srv.url}/api/events",
                headers={**headers, "Last-Event-ID": "1"},
            ) as sse_replay:
                chunk = await asyncio.wait_for(sse_replay.content.read(2048), timeout=2.0)
                assert b"event: hello" in chunk
                assert b"id: 2" in chunk

            # 5. Regression test for WinError 10054 (`_ProactorBasePipeTransport._call_connection_lost`
            #    when browser aborts in-flight `<img>` / `<video>` sockets with TCP RST):
            import socket
            import struct

            loop = asyncio.get_running_loop()
            unhandled_excs: list[dict[str, Any]] = []
            prev_handler = loop.get_exception_handler()
            from davag.server.http import is_benign_socket_disconnect
            loop.set_exception_handler(lambda _l, ctx: None if is_benign_socket_disconnect(ctx.get("exception")) else unhandled_excs.append(ctx))
            try:
                def _rst_storm() -> None:
                    for _ in range(12):
                        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                        s.settimeout(2.0)
                        s.connect(("127.0.0.1", srv.port))
                        s.sendall(f"GET /api/session HTTP/1.1\r\nHost: 127.0.0.1\r\nAuthorization: Bearer {srv.token}\r\n\r\n".encode())
                        # Force hard TCP RST (`SO_LINGER` l_onoff=1, l_linger=0) -> triggers WinError 10054 on server!
                        s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("hh", 1, 0))
                        s.close()
                    # Also reset an already-accepted connection mid-response to exercise `_call_connection_lost`!
                    s2 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    s2.settimeout(2.0)
                    s2.connect(("127.0.0.1", srv.port))
                    s2.sendall(f"GET /api/events?token={srv.token} HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n".encode())
                    _ = s2.recv(64)
                    s2.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("hh", 1, 0))
                    s2.close()

                await asyncio.to_thread(_rst_storm)
                await asyncio.sleep(0.15)
                assert unhandled_excs == [], f"Expected zero unhandled loop exceptions on TCP RST, got: {unhandled_excs}"

                # Server remains 100% healthy after the TCP RST storm!
                async with client.get(f"{srv.url}/api/session", headers=headers) as r_after:
                    assert r_after.status == 200
            finally:
                loop.set_exception_handler(prev_handler)
    finally:
        await srv.close()
        await session.close()

async def test_web_sidecar_utf16_surrogate_resilience(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Verify Win32 UTF-16 surrogate pairs and lone surrogates in `history.jsonl`,
    `Ledger`, and `SessionBus` never crash FastAPI/Pydantic or SSE."""
    home = tmp_path / "home_surr"
    cwd = tmp_path / "cwd_surr"
    home.mkdir()
    cwd.mkdir()
    monkeypatch.setenv("DAVAG_HOME", str(home))
    monkeypatch.setenv("DAVAI_HOME", str(tmp_path / "davai_empty"))

    # Pre-seed history.jsonl with escaped UTF-16 surrogate pair + lone surrogate (as json.loads produces!)
    (home / "history.jsonl").write_text(
        '{"t": 1.0, "text": "pasted thought \\ud83d\\udcad and broken \\ud83d end"}\n',
        encoding="utf-8",
    )

    session = await bootstrap_session(
        cwd=cwd,
        make_provider_fn=lambda _cfg, **_kw: _WebTestProvider(),
        auto_connect_resolve=False,
    )
    surr_pair = chr(0xD83D) + chr(0xDCAD)
    lone_surr = chr(0xD83D)

    # Also inject raw un-sanitized surrogates directly into Ledger & Bus to test SafeJSONResponse & SSE _fmt!
    session.ledger.add(
        seg_type="user",
        label=f"raw {surr_pair} label {lone_surr}",
        role="user",
        text=f"raw body {surr_pair} and lone {lone_surr}",
    )
    session.bus.publish("transcript", {"type": "user", "text": f"sse {surr_pair} and {lone_surr}"})

    submit_fn = create_dispatcher(session=session)
    srv = await start_web_server(
        session=session,
        submit_fn=submit_fn,
        host="127.0.0.1",
        port=0,
        token="surr-token",
    )
    headers = {"Authorization": "Bearer surr-token"}
    try:
        async with aiohttp.ClientSession() as client:
            async with client.get(f"{srv.url}/api/history", headers=headers) as r_hist:
                assert r_hist.status == 200
                h_data = await r_hist.json()
                assert "💭" in h_data["prompts"][0]
                assert "\ufffd" in h_data["prompts"][0]

            async with client.get(f"{srv.url}/api/context", headers=headers) as r_ctx:
                assert r_ctx.status == 200
                c_data = await r_ctx.json()
                assert any("💭" in s["label"] for s in c_data["segments"])

            async with client.get(f"{srv.url}/api/session", headers=headers) as r_sess:
                assert r_sess.status == 200

            # Verify SSE stream yields both hello and the surrogate-healed transcript event cleanly
            async with client.get(f"{srv.url}/api/events?token=surr-token") as sse:
                assert sse.status == 200
                chunk = (await asyncio.wait_for(sse.content.readany(), timeout=2.0)).decode("utf-8")
                assert "event: hello" in chunk
                assert "sse 💭" in chunk
    finally:
        await srv.close()
        await session.close()
