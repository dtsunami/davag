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