"""Static contract test modeled on `davai/test/events.test.js` & `davai/test/webclient.test.js`.

Verifies:
1. Every event in `davag.agent.loop.EVENTS` is registered in TUI (`ui/tui.py`) and Headless (`headless.py`).
2. `ui/commands.py` `command_list()` parses every slash command cleanly without duplicates.
3. `ui/web/app.css` preserves `[hidden] { display: none !important; }` and every `$('id')` lookup
   in `ui/web/app.js` exists in `ui/web/index.html`!
"""

from __future__ import annotations

import re
from pathlib import Path

from davag.agent.loop import EVENTS
from davag.ui.commands import command_list


def test_every_declared_event_is_consumed_by_tui_and_headless() -> None:
    src_root = Path(__file__).resolve().parents[1] / "src" / "davag"
    tui_src = (src_root / "ui" / "tui.py").read_text(encoding="utf-8")
    headless_src = (src_root / "headless.py").read_text(encoding="utf-8")
    loop_src = (src_root / "agent" / "loop.py").read_text(encoding="utf-8")

    # 1. Every self.emit("...") in loop.py must be declared in EVENTS
    emitted_in_loop = set(re.findall(r'self\.emit\(\s*"([^"]+)"', loop_src))
    assert emitted_in_loop <= set(EVENTS), f"Undeclared emits in loop.py: {emitted_in_loop - set(EVENTS)}"

    # 2. Every event in EVENTS must be wired in tui.py and headless.py
    for ev in EVENTS:
        assert f'"{ev}"' in tui_src, f'Event "{ev}" has no listener in ui/tui.py'
        assert f'"{ev}"' in headless_src, f'Event "{ev}" has no listener in headless.py'


def test_command_list_single_source_of_truth() -> None:
    cmds = command_list()
    names = [c["name"] for c in cmds]
    assert len(names) >= 18
    assert len(names) == len(set(names))
    for expected in ("/model", "/resolve", "/grab", "/watch", "/undo", "/context", "/tps", "/env", "/keys"):
        assert expected in names


def test_web_client_dom_ids_and_hidden_rule() -> None:
    web_dir = Path(__file__).resolve().parents[1] / "src" / "davag" / "ui" / "web"
    html = (web_dir / "index.html").read_text(encoding="utf-8")
    css = (web_dir / "app.css").read_text(encoding="utf-8")
    js = (web_dir / "app.js").read_text(encoding="utf-8")

    assert "[hidden]" in css and "display: none !important" in css

    html_ids = set(re.findall(r'\bid="([^"]+)"', html))
    js_dollar_ids = set(re.findall(r"\$\('([^']+)'\)", js))
    missing = js_dollar_ids - html_ids
    assert not missing, f"app.js references missing HTML element IDs: {missing}"


def test_repl_history_loads_and_persists_across_sessions(tmp_path: Path) -> None:
    from davag.session.history import PromptHistory
    from davag.ui.tui import DavagPtHistory

    store1 = PromptHistory(tmp_path)
    pt1 = DavagPtHistory(store1)
    pt1.append_string("cut first 30 seconds")
    pt1.append_string("/model opus")  # in-memory for session, excluded from history.jsonl
    pt1.append_string("sh git status")  # excluded from history.jsonl
    pt1.append_string("make a 10 min video")

    # Simulate fresh REPL relaunch reading ~/.davag/history.jsonl
    store2 = PromptHistory(tmp_path)
    pt2 = DavagPtHistory(store2)
    loaded = list(pt2.load_history_strings())
    # Newest-first for Up-Arrow recall!
    assert loaded == ["make a 10 min video", "cut first 30 seconds"]