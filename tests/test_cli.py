"""Tests for CLI parsing, --host/--port implying --web, and non-loopback --yolo refusal."""

from __future__ import annotations

import pytest

from davag.cli import UsageError, assert_safe_bind, is_loopback_host, main, parse_args


def test_default_args() -> None:
    args = parse_args([])
    assert args.web is False
    assert args.print_mode is False
    assert args.input_text == ""


def test_host_or_port_implies_web() -> None:
    by_host = parse_args(["--host", "0.0.0.0"])
    assert by_host.web is True
    assert by_host.host == "0.0.0.0"
    assert by_host.overrides["DAVAG_WEB"] == "true"
    assert by_host.overrides["DAVAG_WEB_HOST"] == "0.0.0.0"

    by_port = parse_args(["--port", "0"])
    assert by_port.web is True
    assert by_port.port == 0
    assert by_port.overrides["DAVAG_WEB_PORT"] == "0"

    by_open = parse_args(["--open"])
    assert by_open.web is True
    assert by_open.overrides["DAVAG_WEB"] == "true"
    assert by_open.overrides["DAVAG_WEB_OPEN"] == "true"


def test_web_with_numeric_arg_rejected_cleanly() -> None:
    with pytest.raises(UsageError, match="--web takes no port"):
        parse_args(["--web", "8080"])


def test_loopback_detection_and_yolo_guard() -> None:
    assert is_loopback_host("127.0.0.1")
    assert is_loopback_host("localhost")
    assert is_loopback_host("::1")
    assert not is_loopback_host("0.0.0.0")
    assert not is_loopback_host("192.168.1.50")

    # Loopback + yolo is allowed
    assert_safe_bind(host="127.0.0.1", yolo=True)

    # Non-loopback + yolo is refused
    with pytest.raises(UsageError, match="Refusing to combine"):
        assert_safe_bind(host="0.0.0.0", yolo=True)


def test_cli_non_loopback_yolo_rejected_via_main(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("DAVAG_HOME", str(tmp_path / "davag_home"))
    monkeypatch.setenv("DAVAI_HOME", str(tmp_path / "davai_home"))
    with pytest.raises(UsageError, match="Refusing to combine"):
        main(["--host", "0.0.0.0", "--yolo"])


def test_resume_optional_arg() -> None:
    a1 = parse_args(["--resume"])
    assert a1.resume is True

    a2 = parse_args(["--resume", "9ffcf35f", "continue", "editing"])
    assert a2.resume == "9ffcf35f"
    assert a2.input_text == "continue editing"

@pytest.mark.asyncio
async def test_resume_keeps_active_cwd_and_appends_orig_cwd_readonly(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    from davag.agent.sandbox import SandboxError
    from davag.session.bootstrap import bootstrap_session

    home = tmp_path / "davag_home"
    davai_home = tmp_path / "davai_home"
    orig_dir = tmp_path / "orig_workspace"
    new_dir = tmp_path / "new_workspace"
    third_dir = tmp_path / "third_workspace"
    orig_dir.mkdir()
    new_dir.mkdir()
    third_dir.mkdir()
    (orig_dir / "from_orig.txt").write_text("hello from orig", encoding="utf-8")
    (new_dir / "from_new.txt").write_text("hello from new", encoding="utf-8")

    monkeypatch.setenv("DAVAG_HOME", str(home))
    monkeypatch.setenv("DAVAI_HOME", str(davai_home))
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")

    class _DummyProvider:
        name = "gemini"

        async def send(self, **_kw):
            if False:
                yield None

    # 1. Start initial session in orig_dir and record a turn
    s1 = await bootstrap_session(
        cwd=orig_dir,
        make_provider_fn=lambda _cfg, **_kw: _DummyProvider(),
        auto_connect_resolve=False,
    )
    s1.log.record("user", {"text": "check timeline"})
    await s1.close()

    # 2. cd into new_dir and run `davag --resume` (cwd=None -> uses os.getcwd() == new_dir)
    monkeypatch.chdir(new_dir)
    s2 = await bootstrap_session(
        cwd=None,
        resume=True,
        make_provider_fn=lambda _cfg, **_kw: _DummyProvider(),
        auto_connect_resolve=False,
    )
    try:
        assert s2.cfg.cwd == new_dir.resolve()
        assert orig_dir.resolve() in s2.cfg.ro_dirs
        assert orig_dir.resolve() in s2.sandbox.ro_dirs
        # Can read from both new_dir and orig_dir
        assert s2.sandbox.resolve_for_read("from_new.txt") == (new_dir / "from_new.txt").resolve()
        assert s2.sandbox.resolve_for_read(orig_dir / "from_orig.txt") == (orig_dir / "from_orig.txt").resolve()
        # Writes succeed in new_dir, but are refused in orig_dir (read-only!)
        assert s2.sandbox.resolve_for_write("out.txt") == (new_dir / "out.txt").resolve()
        with pytest.raises(SandboxError, match="write denied"):
            s2.sandbox.resolve_for_write(orig_dir / "nope.txt")
        # System prompt grounding reflects new_dir as Working directory and orig_dir as RO
        assert f"Working directory: {new_dir.resolve()}" in s2.ledger.system
        assert str(orig_dir.resolve()) in s2.ledger.system
        s2.log.record("user", {"text": "resumed turn in new_dir"})
    finally:
        await s2.close()

    # 3. Chained resume from third_dir keeps both new_dir and orig_dir in ro_dirs!
    monkeypatch.chdir(third_dir)
    s3 = await bootstrap_session(
        cwd=None,
        resume=True,
        make_provider_fn=lambda _cfg, **_kw: _DummyProvider(),
        auto_connect_resolve=False,
    )
    try:
        assert s3.cfg.cwd == third_dir.resolve()
        assert new_dir.resolve() in s3.cfg.ro_dirs
        assert orig_dir.resolve() in s3.cfg.ro_dirs
    finally:
        await s3.close()


@pytest.mark.asyncio
async def test_gemini_stream_idle_timeout_resets_while_streaming(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio
    from types import SimpleNamespace

    from davag.providers.base import ProviderError
    from davag.providers.gemini import GeminiAdapter

    monkeypatch.setenv("DAVAG_STREAM_IDLE_TIMEOUT", "0.08")
    adapter = GeminiAdapter(api_key="fake-key", model="gemini-4-carbon", max_tokens=1024)
    # Verify client HttpOptions has NO total wall-clock timeout (which would set X-Server-Timeout / ClientTimeout(total=...))
    assert adapter._client._api_client._http_options.timeout is None

    # Fake stream emitting 6 chunks spaced 0.03s apart -> total 0.18s (> 0.08s idle timeout!),
    # proving active token streaming resets the timer on every chunk instead of dying at 0.08s!
    async def _active_stream(**_kw):
        async def _gen():
            for i in range(6):
                await asyncio.sleep(0.03)
                part = SimpleNamespace(text=f"tok{i} ", thought=False)
                cand = SimpleNamespace(content=SimpleNamespace(parts=[part]), finish_reason="STOP")
                yield SimpleNamespace(candidates=[cand], usage_metadata=None)

        return _gen()

    monkeypatch.setattr(adapter._client.aio.models, "generate_content_stream", _active_stream)
    collected = [ev async for ev in adapter.send(system="sys", messages=[{"role": "user", "content": [{"type": "text", "text": "hi"}]}])]
    text_out = "".join(ev.delta for ev in collected if ev.t == "text")
    assert text_out == "tok0 tok1 tok2 tok3 tok4 tok5 "

    # Conversely, a genuinely dead stream (silent > 0.08s) DOES raise a timeout ProviderError
    async def _stalled_stream(**_kw):
        async def _gen():
            await asyncio.sleep(0.20)
            yield SimpleNamespace(candidates=[], usage_metadata=None)

        return _gen()

    monkeypatch.setattr(adapter._client.aio.models, "generate_content_stream", _stalled_stream)
    with pytest.raises(ProviderError, match="Stream idle"):
        _ = [ev async for ev in adapter.send(system="sys", messages=[{"role": "user", "content": [{"type": "text", "text": "hi"}]}])]


@pytest.mark.asyncio
async def test_multihop_resume_accumulates_transcript_stats_perf_and_resets_spend_limit(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    from davag.providers.base import StreamEvent
    from davag.session.bootstrap import bootstrap_session

    home = tmp_path / "davag_home"
    davai_home = tmp_path / "davai_home"
    d1 = tmp_path / "ws1"
    d2 = tmp_path / "ws2"
    d3 = tmp_path / "ws3"
    d1.mkdir()
    d2.mkdir()
    d3.mkdir()

    monkeypatch.setenv("DAVAG_HOME", str(home))
    monkeypatch.setenv("DAVAI_HOME", str(davai_home))
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")

    class _ScriptedProvider:
        name = "gemini"

        def __init__(self, reply: str, out_toks: int = 500_000) -> None:
            self.reply = reply
            self.out_toks = out_toks

        async def send(self, **_kw):
            yield StreamEvent(t="text", delta=self.reply)
            yield StreamEvent(
                t="usage",
                in_fresh=100_000,
                out=self.out_toks,
                cache_read=50_000,
                cache_write=0,
                context_in=150_000,
            )
            yield StreamEvent(t="stop", reason="end_turn")

    # Hop 1 in d1 with DAVAG_MAX_COST=$0.01: runs Turn #1 (which exceeds $0.01), then next run in same session hits over_budget
    s1 = await bootstrap_session(
        cwd=d1,
        overrides={"DAVAG_MAX_COST": "0.01"},
        make_provider_fn=lambda _cfg, **_kw: _ScriptedProvider("answer from hop 1"),
        auto_connect_resolve=False,
    )
    r1 = await s1.agent.run("question in hop 1")
    assert r1["status"] == "done"
    cost_after_hop1 = s1.agent.stats["cost"]
    assert cost_after_hop1 > 0.01
    assert s1.agent.stats["turns"] == 1
    r1_blocked = await s1.agent.run("second question in hop 1")
    assert r1_blocked["status"] == "over_budget"
    await s1.close()

    # Hop 2 in d2: resume WITHOUT running any turns (e.g. opened & closed), verify nothing is lost!
    monkeypatch.chdir(d2)
    s2 = await bootstrap_session(
        cwd=None,
        resume=True,
        overrides={"DAVAG_MAX_COST": "0.01"},
        make_provider_fn=lambda _cfg, **_kw: _ScriptedProvider("unused"),
        auto_connect_resolve=False,
    )
    assert s2.cfg.cwd == d2.resolve()
    assert d1.resolve() in s2.cfg.ro_dirs
    assert s2.agent.stats["turns"] == 1
    assert s2.agent.stats["cost"] == pytest.approx(cost_after_hop1, rel=1e-4)
    assert len(s2.perf.turns) == 1 and s2.perf.turns[0].turn == 1
    await s2.close()

    # Hop 3 in d3: resume from s2! Even though s2 ran 0 turns and DAVAG_MAX_COST is STILL $0.01:
    # - hop 1's transcript is STILL in ledger!
    # - both d2 and d1 are in ro_dirs!
    # - spend limit reset (`run_cost == 0.0`), so Turn #2 runs cleanly and accumulates stats to 2 turns & 2x cost!
    monkeypatch.chdir(d3)
    s3 = await bootstrap_session(
        cwd=None,
        resume=True,
        overrides={"DAVAG_MAX_COST": "0.01"},
        make_provider_fn=lambda _cfg, **_kw: _ScriptedProvider("answer from hop 3"),
        auto_connect_resolve=False,
    )
    try:
        assert s3.cfg.cwd == d3.resolve()
        assert d2.resolve() in s3.cfg.ro_dirs
        assert d1.resolve() in s3.cfg.ro_dirs
        # Hop 1 conversation survived through 0-turn Hop 2!
        all_msg_txt = " ".join(
            p.get("text", "") for m in s3.ledger.to_messages() for p in m.get("content", [])
        )
        assert "question in hop 1" in all_msg_txt
        assert "answer from hop 1" in all_msg_txt

        # Spend limit reset -> Turn #2 succeeds despite accumulated cost > $0.01!
        assert s3.agent.run_cost == 0.0
        r3 = await s3.agent.run("question in hop 3")
        assert r3["status"] == "done"
        assert s3.agent.stats["turns"] == 2
        assert s3.agent.stats["cost"] == pytest.approx(cost_after_hop1 * 2, rel=1e-4)
        assert [t.turn for t in s3.perf.turns] == [1, 2]

        # Now run_cost in s3 ALSO exceeded $0.01 -> next turn in s3 is gated by DAVAG_MAX_COST
        r3_blocked = await s3.agent.run("another in hop 3")
        assert r3_blocked["status"] == "over_budget"
    finally:
        await s3.close()


def test_doctor_reports_motion_overlay_status(
    monkeypatch: pytest.MonkeyPatch, tmp_path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DAVAG_HOME", str(tmp_path / "davag_home"))
    monkeypatch.setenv("DAVAI_HOME", str(tmp_path / "davai_home"))
    rc = main(["--doctor"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Motion Overlay Engine" in out
    assert "svg fast path" in out
    assert "canvas fast path" in out
    assert "html/css cdp pool" in out
    assert "alpha codecs" in out
