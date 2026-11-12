"""Tests for `dr_ops` parser, provider stream/cost translation, SessionBus raced approvals,
degeneracy guard, and multi-turn Agent loop + session resume.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from davag.agent.loop import detect_repetition
from davag.agent.parser import ParseError, extract_turn
from davag.config import load_config
from davag.config.models import resolve_model
from davag.context import Ledger
from davag.providers import StreamEvent, cost_of_usage, mask_key
from davag.providers.openai_responses import translate_responses_stream
from davag.session import bootstrap_session, restore_ledger_from_session
from davag.transport import DirectTransport

from .fake_resolve import FakeResolve


def test_dr_ops_payloads_nested_fences_and_xml_leak_recovery() -> None:
    assistant_msg = """Let's write a helper doc with nested markdown code fences and run eval:

```dr_ops
{"ops": [
  {"op": "fs.write", "path": "notes.md", "text": "@doc"},
  {"op": "resolve.eval", "code": "@py"}
]}
</rfl:parameter>
--davag:doc--
# Editing Notes
```python
print("nested fence inside payload must NOT close dr_ops!")
```
--davag:end--
--davag:py--
result = 1 + 1
--davag:end--
"""  # Note: intentionally missing closing ``` fence too!

    parsed = extract_turn(assistant_msg)
    assert len(parsed.ops) == 2
    assert "```python" in parsed.ops[0].params["text"]
    assert parsed.ops[1].params["code"] == "result = 1 + 1"
    assert parsed.recovered is not None
    assert "stripped stray XML tag" in parsed.recovered
    assert "no closing fence" in parsed.recovered


def test_unreferenced_payload_raises_parse_error() -> None:
    bad = """```dr_ops
{"ops": [{"op": "resolve.status"}]}
--davag:unused--
hello
--davag:end--
```"""
    with pytest.raises(ParseError, match="Unreferenced payload"):
        extract_turn(bad)


def test_cost_math_and_key_masking() -> None:
    spec = resolve_model("anthropic", "claude-opus-5")  # $5/M in, $25/M out
    usage = StreamEvent(
        t="usage",
        in_fresh=100_000,
        cache_read=900_000,  # 0.1x = $0.50/M -> $0.45
        cache_write=0,
        out=10_000,          # $25/M -> $0.25
        context_in=1_000_000,
    )
    # 0.1M * $5 ($0.50) + 0.9M * $0.50 ($0.45) + 0.01M * $25 ($0.25) = $1.20
    assert abs(cost_of_usage(spec, usage) - 1.20) < 1e-6

    masked = mask_key("sk-or-v1-1234567890abcdef9999 ")
    assert "starts sk-or-v1-" in masked
    assert "leading/trailing whitespace" in masked


async def test_openai_responses_stream_translation() -> None:
    @dataclass
    class FakeEv:
        type: str
        delta: str = ""
        response: Any = None

    @dataclass
    class FakeUsage:
        input_tokens: int = 5000
        output_tokens: int = 300
        input_tokens_details: Any = None

    @dataclass
    class FakeResp:
        usage: Any
        status: str = "completed"

    async def _gen() -> AsyncIterator[Any]:
        yield FakeEv("response.reasoning_summary_text.delta", delta="Checking FPS... ")
        yield FakeEv("response.output_text.delta", delta="Timeline is 24fps.")
        yield FakeEv("response.completed", response=FakeResp(usage=FakeUsage()))

    events = [ev async for ev in translate_responses_stream(_gen())]
    assert events[0].t == "thinking"
    assert events[1].t == "text"
    assert events[2].t == "usage"
    assert events[2].context_in == 5000
    assert events[3].t == "stop"


def test_degeneracy_repetition_guard() -> None:
    normal = "This is a normal varied editing summary. " * 20
    assert detect_repetition(normal) is None

    looped = "Normal intro.\n" + ("{\"op\": \"resolve.status\"}\n" * 120)
    rep = detect_repetition(looped)
    assert rep is not None
    assert rep["repeats"] >= 4


class FakeScriptedProvider:
    """Deterministic scripted streaming provider for testing Agent + SessionBus + Resume."""

    name = "anthropic"
    model = "claude-opus-5"

    def __init__(self, turns: list[str]) -> None:
        self._turns = list(turns)

    async def send(
        self, *, system: str, messages: list[dict[str, Any]], cancel_event: Any = None
    ) -> AsyncIterator[StreamEvent]:
        assert "dr_ops" in system
        reply = self._turns.pop(0)
        yield StreamEvent(t="thinking", delta="Planning Resolve steps...")
        yield StreamEvent(t="text", delta=reply)
        yield StreamEvent(
            t="usage",
            in_fresh=1000,
            out=150,
            cache_read=4000,
            cache_write=0,
            context_in=5000,
        )
        yield StreamEvent(t="stop", reason="end_turn")

    async def count_tokens(self, *, system: str, messages: list[dict[str, Any]]) -> int | None:
        return None


async def test_agent_multiturn_loop_bus_approval_and_resume(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "davag_home"
    cwd = tmp_path / "workspace"
    home.mkdir()
    cwd.mkdir()
    monkeypatch.setenv("DAVAG_HOME", str(home))
    monkeypatch.setenv("DAVAI_HOME", str(tmp_path / "empty_davai"))

    fake_res = FakeResolve()
    tr = DirectTransport(resolve_obj=fake_res)

    scripted_provider = FakeScriptedProvider(
        [
            # Turn 1: inspect status, grab viewer still (multimodal!), add marker, and run gated eval
            """```dr_ops
{"ops": [
  {"op": "resolve.status"},
  {"op": "timeline.grab_still", "tc": "01:00:00:12"},
  {"op": "marker.add", "tc": "01:00:00:12", "color": "Cyan", "name": "Hero Frame"},
  {"op": "resolve.eval", "description": "Read project name", "code": "result = project.GetName()"}
]}
```""",
            # Turn 2: conclude with an artifact snippet
            """Added the Cyan 'Hero Frame' marker at `01:00:00:12` on project **Demo Edit** and verified the viewer frame.

```python
# Saved marker helper
print("Done!")
```""",
        ]
    )

    session = await bootstrap_session(
        cwd=cwd,
        make_provider_fn=lambda _cfg, **_kw: scripted_provider,
        injected_transport=tr,
        auto_connect_resolve=False,
    )

    bus_events: list[dict[str, Any]] = []
    session.bus.subscribe(lambda ev: bus_events.append(ev))

    # Attach a simulated Web browser subscriber that races to approve `approval-request` via SessionBus!
    def _web_auto_approver(ev: dict[str, Any]) -> None:
        if ev["type"] == "approval-request":
            ap_id = ev["data"]["id"]
            won = session.bus.resolve_approval(ap_id, {"allow": True})
            assert won is True
            # Second duplicate click from another browser tab returns False cleanly (stale, not error)
            assert session.bus.resolve_approval(ap_id, {"allow": False}) is False

    session.bus.subscribe(_web_auto_approver)

    res = await session.agent.run("Grab a viewer still at 01:00:00:12 and mark it")
    assert res["status"] == "done"
    assert session.agent.stats["turns"] == 2
    assert session.agent.stats["ops"] == 4
    assert len(session.artifacts.items) == 1

    # Verify the viewer PNG still was attached into the multimodal Ledger!
    media_segs = [s for s in session.ledger.segments if s.type == "media"]
    assert len(media_segs) == 1
    assert media_segs[0].part["type"] == "image"

    # Verify SessionBus recorded approval-request AND approval-resolved
    ev_types = [e["type"] for e in bus_events]
    assert "approval-request" in ev_types
    assert "approval-resolved" in ev_types

    sess_dir = session.log.dir
    await session.close()

    # Verify `--resume` restores conversation segments from `transcript.jsonl`
    cfg = load_config(cwd=cwd, home=home, davai_home=tmp_path / "empty_davai")
    restored_ledger = Ledger(limit=cfg.context_limit)
    count = restore_ledger_from_session(sess_dir, restored_ledger)
    assert count >= 3