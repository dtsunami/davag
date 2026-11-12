"""`#---#` `.env` provider block inspection and rollback-safe live switching."""

from __future__ import annotations

from typing import Any

from davag.config.env import KEY_VARS, load_config
from davag.config.envfile import edit_env_file, env_file_path, read_env_text, write_env_text
from davag.config.profiles import block_state, enable_block, select_block_var
from davag.config.settings import load_settings, settings_to_defaults
from davag.providers import mask_key

from .bootstrap import Session

ALL_SECRET_NAMES = frozenset(v for group in KEY_VARS.values() for v in group)


def _mask_var_if_secret(name: str, val: str) -> str:
    if name in ALL_SECRET_NAMES or "KEY" in name.upper() or "TOKEN" in name.upper():
        return mask_key(val)
    return val


def profile_state(session: Session) -> dict[str, Any]:
    path = env_file_path(session.cfg.home)
    raw = read_env_text(path)
    st = block_state(raw)
    masked_blocks = []
    for b in st["blocks"]:
        masked_vars = [
            {
                **v,
                "value": _mask_var_if_secret(v["name"], v["value"]),
                "choices": [
                    {**c, "value": _mask_var_if_secret(v["name"], c["value"])}
                    for c in v.get("choices", [])
                ],
            }
            for v in b["vars"]
        ]
        masked_blocks.append({**b, "vars": masked_vars})
    return {
        "file": str(path),
        "active": st["active"],
        "preamble": st["preamble"],
        "blocks": masked_blocks,
    }


def switch_profile(session: Session, name: str | None) -> dict[str, Any]:
    path = env_file_path(session.cfg.home)
    before_raw = read_env_text(path)
    edit_env_file(path, lambda txt: enable_block(txt, name)["text"])

    try:
        defaults = settings_to_defaults(load_settings(session.cfg.home))
        overrides: dict[str, str] = {}
        if session.cfg.yolo:
            overrides["DAVAG_YOLO"] = "true"
        if session.cfg.web_enabled:
            overrides["DAVAG_WEB"] = "true"
            overrides["DAVAG_WEB_HOST"] = session.cfg.web_host
            overrides["DAVAG_WEB_PORT"] = str(session.cfg.web_port)
        new_cfg = load_config(
            cwd=session.cfg.cwd,
            defaults=defaults,
            overrides=overrides,
            home=session.cfg.home,
            davai_home=session.cfg.davai_home,
        )
    except Exception:
        # Roll back file byte-for-byte on invalid profile!
        write_env_text(path, before_raw)
        raise

    session.cfg = new_cfg
    session.agent.cfg = new_cfg
    session.ledger.limit = new_cfg.context_limit
    st = profile_state(session)
    session.bus.publish("env-changed", st)
    return st


def choose_profile_var(
    session: Session, block_name: str, var_name: str, *, line: int | None = None, value: str | None = None
) -> dict[str, Any]:
    path = env_file_path(session.cfg.home)
    before_raw = read_env_text(path)
    edit_env_file(
        path,
        lambda txt: select_block_var(txt, block_name, var_name, line=line, value=value)["text"],
    )
    try:
        defaults = settings_to_defaults(load_settings(session.cfg.home))
        new_cfg = load_config(
            cwd=session.cfg.cwd,
            defaults=defaults,
            home=session.cfg.home,
            davai_home=session.cfg.davai_home,
        )
    except Exception:
        write_env_text(path, before_raw)
        raise

    session.cfg = new_cfg
    session.agent.cfg = new_cfg
    st = profile_state(session)
    session.bus.publish("env-changed", st)
    return st