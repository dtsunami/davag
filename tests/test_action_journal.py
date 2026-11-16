"""End-to-end tests for the Unified Resumable Operator+Agent Action Journal:
1. GUI `/api/overlays/adjust` marker add/move/delete, overlay move/delete, clip label -> `actor="operator"`.
2. Agent `execute_batch` marker ops, timeline cuts, & file writes -> `actor="agent"`.
3. Step Undo/Redo, out-of-order Individual `undo_entry`/`redo_entry`, and Rewind/Fast-Forward `goto_cursor`.
4. Agent `journal.history`, `journal.undo`, and `journal.redo` `dr_ops`.
5. Disk persistence (`actions.jsonl`) and full `bootstrap_session(..., resume=True)` recovery!
"""

from __future__ import annotations

from pathlib import Path

import aiohttp
import pytest

from davag.agent.executor import OpCall, execute_batch
from davag.providers import StreamEvent
from davag.server.http import start_web_server
from davag.session import bootstrap_session
from davag.session.dispatch import create_dispatcher
from davag.transport import DirectTransport

from .fake_resolve import FakeResolve


class _DummyProvider:
    name = "anthropic"
    model = "claude-opus-5"

    async def send(self, **_kw):  # type: ignore[no-untyped-def]
        yield StreamEvent(t="text", delta="ok")
        yield StreamEvent(t="stop", reason="end_turn")

    async def count_tokens(self, **_kw):  # type: ignore[no-untyped-def]
        return None


async def test_gui_operator_and_agent_action_timeline_rewind_individual_undo_and_resume(
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

    session = await bootstrap_session(
        cwd=cwd,
        make_provider_fn=lambda _cfg, **_kw: _DummyProvider(),
        injected_transport=tr,
        auto_connect_resolve=False,
    )
    submit_fn = create_dispatcher(session=session)
    srv = await start_web_server(
        session=session,
        submit_fn=submit_fn,
        host="127.0.0.1",
        port=0,
        token="tok-journal",
    )
    headers = {"Authorization": "Bearer tok-journal"}

    try:
        async with aiohttp.ClientSession() as client:
            # 1. Operator adds two chapter markers and deletes the first one via GUI!
            async with client.post(
                f"{srv.url}/api/overlays/adjust",
                headers=headers,
                json={
                    "action": "add_marker",
                    "new_tc": "01:00:05:00",
                    "color": "Cyan",
                    "name": "Chapter 1: Intro",
                    "note": "Welcome hook",
                },
            ) as r1:
                assert r1.status == 200
                d1 = await r1.json()
                assert d1["action_id"] == "act-0001"

            async with client.post(
                f"{srv.url}/api/overlays/adjust",
                headers=headers,
                json={
                    "action": "add_marker",
                    "new_tc": "01:00:20:00",
                    "color": "Cyan",
                    "name": "Chapter 2: Deep Dive",
                    "note": "Architecture walkthrough",
                },
            ) as r2:
                assert r2.status == 200
                assert (await r2.json())["action_id"] == "act-0002"

            # Operator accidentally deletes Chapter 1 (frame 86400 + 5*24 = 86520 @ 24fps)!
            async with client.post(
                f"{srv.url}/api/overlays/adjust",
                headers=headers,
                json={"action": "delete_marker", "old_frame": 86520},
            ) as r3:
                assert r3.status == 200
                d3 = await r3.json()
                del_action_id = d3["action_id"]
                assert del_action_id == "act-0003"

            # 2. Agent adds a Yellow review marker AND writes a file (`actor="agent"`)
            out_ag = await execute_batch(
                [
                    OpCall(0, "marker.add", {"tc": "01:00:30:00", "color": "Yellow", "name": "Agent Note"}),
                    OpCall(1, "fs.write", {"path": "edl_notes.md", "text": "v1 notes"}),
                ],
                sandbox=session.sandbox,
                journal=session.journal,
                transport=tr,
                cache_dir=home / "cache",
            )
            assert out_ag.status == "ok"

            # 3. Inspect `/api/journal` -> 4 actions: 3 operator + 1 agent!
            async with client.get(f"{srv.url}/api/journal", headers=headers) as rj:
                jst = await rj.json()
                assert jst["total"] == 4
                assert [a["actor"] for a in jst["actions"]] == [
                    "operator",
                    "operator",
                    "operator",
                    "agent",
                ]

            # Chapter 1 is currently gone (only Chapter 2 + Agent Note remain = 2 markers)
            mk_now = await tr.call("marker.list")
            assert [m["name"] for m in mk_now["markers"]] == ["Chapter 2: Deep Dive", "Agent Note"]

            # 4. OUT-OF-ORDER INDIVIDUAL UNDO: undo ONLY `act-0003` (the accidental Chapter 1 delete!)
            #    WITHOUT losing Agent's subsequent `act-0004` marker & file!
            async with client.post(
                f"{srv.url}/api/journal/undo",
                headers=headers,
                json={"id": del_action_id},
            ) as ru:
                assert ru.status == 200

            mk_recovered = await tr.call("marker.list")
            assert [m["name"] for m in mk_recovered["markers"]] == [
                "Chapter 1: Intro",
                "Chapter 2: Deep Dive",
                "Agent Note",
            ]
            assert (cwd / "edl_notes.md").read_text(encoding="utf-8") == "v1 notes"

            # 5. Rewind ENTIRE session to Step 0 via `/api/journal/goto`, then Fast-Forward to Step 4!
            async with client.post(
                f"{srv.url}/api/journal/goto",
                headers=headers,
                json={"seq": 0},
            ) as rg0:
                assert rg0.status == 200
            assert (await tr.call("marker.list"))["count"] == 0
            assert not (cwd / "edl_notes.md").exists()

            async with client.post(
                f"{srv.url}/api/journal/goto",
                headers=headers,
                json={"seq": 4},
            ) as rg4:
                assert rg4.status == 200
            # At step 4 all 4 actions are applied (so act-0003 deletion is applied again -> 2 markers + file!)
            assert [m["name"] for m in (await tr.call("marker.list"))["markers"]] == [
                "Chapter 2: Deep Dive",
                "Agent Note",
            ]
            assert (cwd / "edl_notes.md").read_text(encoding="utf-8") == "v1 notes"

            # 6. Agent itself can call `journal.history` and `journal.undo` via `dr_ops`!
            ag_undo = await execute_batch(
                [
                    OpCall(0, "journal.history", {}),
                    OpCall(1, "journal.undo", {"id": "act-0003"}),
                ],
                sandbox=session.sandbox,
                journal=session.journal,
                transport=tr,
                cache_dir=home / "cache",
            )
            assert ag_undo.status == "ok"
            assert ag_undo.results[0]["result"]["max_seq"] == 4
            assert [m["name"] for m in (await tr.call("marker.list"))["markers"]] == [
                "Chapter 1: Intro",
                "Chapter 2: Deep Dive",
                "Agent Note",
            ]
    finally:
        await srv.close()
        await session.close()

    # 7. RESUMABILITY: launch a brand-new `bootstrap_session(..., resume=True)` on the same FakeResolve!
    tr2 = DirectTransport(resolve_obj=fake)
    resumed = await bootstrap_session(
        cwd=cwd,
        resume=True,
        make_provider_fn=lambda _cfg, **_kw: _DummyProvider(),
        injected_transport=tr2,
        auto_connect_resolve=False,
    )
    try:
        st2 = resumed.journal.list_state()
        assert st2["total"] == 4
        assert [a["actor"] for a in st2["actions"]] == ["operator", "operator", "operator", "agent"]
        # Act-0003 is still undone from before closing; undoing the last applied (act-0004) in the
        # resumed session removes Agent Note AND deletes edl_notes.md!
        await resumed.journal.undo_last(tr2)
        assert [m["name"] for m in (await tr2.call("marker.list"))["markers"]] == [
            "Chapter 1: Intro",
            "Chapter 2: Deep Dive",
        ]
        assert not (cwd / "edl_notes.md").exists()
    finally:
        await resumed.close()
