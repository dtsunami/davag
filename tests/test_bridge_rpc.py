"""Tests for resolve_ops helpers, stdlib-only isolation, Bridge RPC server, and Direct/Bridge parity."""

from __future__ import annotations

import ast
import asyncio
import sys
import threading
from pathlib import Path

import pytest

from davag.bridge.installer import install_bridge_script
from davag.bridge.server import ResolveBridgeServer
from davag.resolve_ops import OpError
from davag.resolve_ops.helpers import frames_to_tc, lua_list, tc_to_frames
from davag.transport import (
    BridgeTransport,
    DirectTransport,
    TransportConnectionError,
    connect_resolve,
)

from .fake_resolve import FakeResolve


def test_resolve_ops_and_bridge_are_strictly_stdlib_only() -> None:
    """Architectural invariant #1: resolve_ops/ and bridge/ MUST NOT import third-party packages."""
    stdlib = sys.stdlib_module_names
    src_root = Path(__file__).resolve().parents[1] / "src" / "davag"
    for pkg_name in ("resolve_ops", "bridge"):
        for py_file in (src_root / pkg_name).rglob("*.py"):
            tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        top = alias.name.split(".")[0]
                        assert top in stdlib or top == "davag", (
                            f"{py_file} imports non-stdlib module {alias.name!r}"
                        )
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    top = node.module.split(".")[0]
                    assert top in stdlib or top == "davag", (
                        f"{py_file} imports non-stdlib module {node.module!r}"
                    )


def test_lua_list_and_smpte_timecode_math() -> None:
    assert lua_list({2: "b", 1: "a", 3: "c"}) == ["a", "b", "c"]
    assert lua_list({"1": "first", "2": "second"}) == ["first", "second"]
    assert lua_list(None) == []

    # Non-drop 24fps: 01:00:00:00 == 86400 frames
    assert tc_to_frames("01:00:00:00", 24) == 86400
    assert frames_to_tc(86400, 24) == "01:00:00:00"

    # Drop-frame 29.97: 10 minutes has 18000 - 18 = 17982 frames!
    assert tc_to_frames("00:10:00;00", "29.97 DF") == 17982
    assert frames_to_tc(17982, "29.97 DF", drop_frame=True) == "00:10:00;00"
    # Minute 1 drops 2 frames (00:01:00;02 is frame 1800)
    assert frames_to_tc(1800, 29.97, drop_frame=True) == "00:01:00;02"
    assert tc_to_frames("00:01:00;02", 29.97) == 1800


async def test_direct_and_bridge_parity_and_main_thread_execution(tmp_path: Path) -> None:
    fake = FakeResolve(studio=False, version="20.1.0")
    server = ResolveBridgeServer(
        resolve=fake,
        host="127.0.0.1",
        port=0,
        token="secret-bridge-token",
        home=tmp_path,
    )

    main_pump_tid: int | None = None

    def pump_loop() -> None:
        nonlocal main_pump_tid
        main_pump_tid = threading.get_ident()
        server.serve_main_thread(poll_interval=0.005, prefer_ui=False)

    pump_thread = threading.Thread(target=pump_loop, daemon=True)
    pump_thread.start()

    # Wait until discovery file is written
    for _ in range(100):
        if (tmp_path / "bridge.json").is_file():
            break
        await asyncio.sleep(0.01)

    try:
        # 1. Wrong token is rejected cleanly
        bad_client = BridgeTransport(host="127.0.0.1", port=server.port, token="wrong-token")
        with pytest.raises(TransportConnectionError, match="Bridge rejected token"):
            await bad_client.call("system.ping")
        await bad_client.close()

        # 2. `connect_resolve(mode="auto")` discovers bridge.json automatically!
        bridge_tr = await connect_resolve(mode="auto", home=tmp_path)
        assert bridge_tr.mode == "bridge"

        direct_tr = DirectTransport(resolve_obj=fake)

        # 3. Verify parity between DirectTransport and BridgeTransport on shared resolve_ops
        b_status = await bridge_tr.call("resolve.status")
        d_status = await direct_tr.call("resolve.status")

        assert b_status["mode"] == "bridge"
        assert d_status["mode"] == "direct"
        assert b_status["product"] == "DaVinci Resolve"
        assert b_status["studio"] is False
        assert b_status["project"] == d_status["project"]
        assert b_status["timeline"] == d_status["timeline"]

        # 4. Concurrent RPC calls serialize cleanly onto the bridge pump without corruption
        pages = ["color", "fusion", "cut", "edit"]
        results = await asyncio.gather(
            *(bridge_tr.call("resolve.open_page", {"page": p}) for p in pages)
        )
        assert len(results) == 4
        assert fake.GetCurrentPage() in pages

        # 5. Structured OpError propagates across TCP bridge with its error code intact
        with pytest.raises(OpError) as exc_info:
            await bridge_tr.call("resolve.open_page", {"page": "nonexistent"})
        assert exc_info.value.code == "INVALID_PAGE"

        await bridge_tr.close()
        await direct_tr.close()
    finally:
        server.request_stop()
        pump_thread.join(timeout=2.0)


def test_install_bridge_script_writes_valid_launcher(tmp_path: Path) -> None:
    out_file = install_bridge_script(target_dir=tmp_path, port=18765)
    assert out_file.is_file()
    code = out_file.read_text(encoding="utf-8")
    ast.parse(code)  # Valid Python syntax
    assert "run_inside_resolve" in code