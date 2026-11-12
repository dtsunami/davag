"""Tests for .env precedence, ~/.davai/.env fallback, HOME_ONLY_VARS, and CRLF profile round-trips."""

from __future__ import annotations

from pathlib import Path

from davag.config import (
    block_state,
    edit_env_file,
    enable_block,
    load_config,
    load_settings,
    save_settings,
    select_block_var,
    set_block_var,
    set_env_vars,
    settings_to_defaults,
)
from davag.config.envfile import read_env_text, write_env_text


def test_precedence_and_davai_key_fallback(tmp_path: Path) -> None:
    davag_home = tmp_path / "davag"
    davai_home = tmp_path / "davai"
    cwd = tmp_path / "proj"
    davag_home.mkdir()
    davai_home.mkdir()
    cwd.mkdir()

    # ~/.davai/.env has live keys, a parked #---# provider block key, + a DAVAI_MODEL
    # that must NOT hijack davag's model
    write_env_text(
        davai_home / ".env",
        "GEMINI_API_KEY=AIza-from-davai\n"
        "DAVAI_MODEL=claude-haiku-4-5\n"
        "#---# anthropic-parked\n"
        "#ANTHROPIC_API_KEY=sk-ant-from-davai\n"
        "#-end-provider-#\n",
    )

    # settings.json sets gemini, which should be beaten by ~/.davag/.env
    saved = save_settings(davag_home, {"provider": "gemini", "model": "gemini-2.5-flash", "yolo": True})
    assert "yolo" not in saved  # yolo must never persist

    write_env_text(
        davag_home / ".env",
        "DAVAG_PROVIDER=anthropic\n"
        "DAVAG_MODEL=claude-opus-5\n",
    )

    cfg = load_config(
        cwd=cwd,
        defaults=settings_to_defaults(load_settings(davag_home)),
        environ={},
        home=davag_home,
        davai_home=davai_home,
    )
    assert cfg.provider == "anthropic"
    assert cfg.model.id == "claude-opus-5"
    assert cfg.keys["anthropic"] == "sk-ant-from-davai"
    assert cfg.keys["gemini"] == "AIza-from-davai"
    assert cfg.key_origins["anthropic"]["from"] == str(davai_home / ".env")

    # ~/.davag/.env key overrides ~/.davai/.env key
    set_env_vars(davag_home / ".env", {"DAVAG_ANTHROPIC_API_KEY": "sk-ant-from-davag"})
    cfg2 = load_config(
        cwd=cwd,
        defaults=settings_to_defaults(load_settings(davag_home)),
        environ={},
        home=davag_home,
        davai_home=davai_home,
    )
    assert cfg2.keys["anthropic"] == "sk-ant-from-davag"
    assert cfg2.key_origins["anthropic"]["from"] == str(davag_home / ".env")


def test_project_env_cannot_redirect_base_url(tmp_path: Path) -> None:
    davag_home = tmp_path / "davag"
    davai_home = tmp_path / "davai"
    cwd = tmp_path / "untrusted_repo"
    davag_home.mkdir()
    davai_home.mkdir()
    cwd.mkdir()

    write_env_text(davag_home / ".env", "DAVAG_PROVIDER=openai\n")
    write_env_text(cwd / ".env", "DAVAG_BASE_URL=https://evil.example.com/v1\n")

    cfg = load_config(cwd=cwd, environ={}, home=davag_home, davai_home=davai_home)
    assert cfg.base_url is None
    assert any("cannot choose where your API key is sent" in w for w in cfg.warnings)


def test_crlf_profile_blocks_roundtrip_byte_for_byte(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    original = (
        "## Global defaults\r\n"
        "DAVAG_EFFORT=high\r\n"
        "\r\n"
        "#---# opus primary editing brain\r\n"
        "## keep opus 5 for timeline cuts\r\n"
        "DAVAG_PROVIDER=anthropic\r\n"
        "DAVAG_MODEL=claude-opus-5\r\n"
        "#-end-provider-#\r\n"
        "\r\n"
        "#---# gemini-video\r\n"
        "## native video input\r\n"
        "#DAVAG_PROVIDER=gemini\r\n"
        "#DAVAG_MODEL=gemini-2.5-flash\r\n"
        "#DAVAG_MODEL=gemini-3-pro-preview\r\n"
        "#-end-provider-#\r\n"
    )
    write_env_text(env_path, original)

    state = block_state(read_env_text(env_path))
    assert state["active"] == ["opus"]
    assert len(state["blocks"]) == 2
    assert state["blocks"][1]["name"] == "gemini-video"

    # Switch to gemini-video -> CRLF must survive and `##` prose must remain untouched
    edit_env_file(env_path, lambda txt: enable_block(txt, "gemini-video")["text"])
    switched = read_env_text(env_path)
    assert "\r\n" in switched
    assert "\r\r\n" not in switched
    assert "## native video input\r\n" in switched
    # Shadowed earlier duplicate stays commented; last candidate becomes live!
    assert "#DAVAG_MODEL=gemini-2.5-flash\r\n" in switched
    assert "\r\nDAVAG_MODEL=gemini-3-pro-preview\r\n" in switched

    # Switch back to opus -> must restore the file byte-for-byte!
    edit_env_file(env_path, lambda txt: enable_block(txt, "opus")["text"])
    restored = read_env_text(env_path)
    assert restored == original


def test_select_and_set_block_var() -> None:
    txt = (
        "#---# openrouter\n"
        "DAVAG_PROVIDER=openrouter\n"
        "## fast\n"
        "#DAVAG_MODEL=anthropic/claude-sonnet-4.5\n"
        "## smart\n"
        "DAVAG_MODEL=anthropic/claude-opus-5\n"
        "#-end-provider-#\n"
    )
    picked = select_block_var(
        txt, "openrouter", "DAVAG_MODEL", value="anthropic/claude-sonnet-4.5"
    )
    assert picked["value"] == "anthropic/claude-sonnet-4.5"
    # Switch away and back: the chosen model must still be the active one
    off = enable_block(picked["text"], None)["text"]
    back = enable_block(off, "openrouter")["text"]
    st = block_state(back)
    model_var = next(v for v in st["blocks"][0]["vars"] if v["name"] == "DAVAG_MODEL")
    assert model_var["value"] == "anthropic/claude-sonnet-4.5"

    # Adding a variable to a new block starts parked by default
    added = set_block_var(back, "local-vllm", "DAVAG_BASE_URL", "http://127.0.0.1:8000/v1")
    assert added["created"] is True
    assert added["enabled"] is False
    assert "#DAVAG_BASE_URL=http://127.0.0.1:8000/v1" in added["text"]