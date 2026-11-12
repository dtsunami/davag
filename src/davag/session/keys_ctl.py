"""API key inspection and atomic persistence (`session/keys_ctl.py`).

Never exposes raw key strings on `SessionBus` or HTTP responses—only `mask_key` descriptions!
"""

from __future__ import annotations

from typing import Any

from davag.config.env import PROVIDERS, key_var_for
from davag.config.envfile import env_file_path, set_env_vars
from davag.providers import mask_key

from .bootstrap import Session


def key_state(session: Session) -> list[dict[str, Any]]:
    cfg = session.cfg
    out: list[dict[str, Any]] = []
    for p in PROVIDERS:
        val = cfg.keys.get(p)
        origin = cfg.key_origins.get(p) or {}
        out.append(
            {
                "provider": p,
                "configured": bool(val),
                "masked": mask_key(val),
                "var": origin.get("name") or key_var_for(p),
                "from": origin.get("from"),
            }
        )
    return out


def set_provider_key(
    session: Session,
    provider: str,
    api_key: str,
    *,
    save: bool = False,
) -> dict[str, Any]:
    prov = provider.strip().lower()
    var_name = key_var_for(prov)
    cleaned = api_key.strip()
    if not cleaned:
        raise ValueError("API key cannot be empty")

    session.cfg.keys[prov] = cleaned
    origin_src = "session (unsaved)"
    if save:
        target = env_file_path(session.cfg.home)
        set_env_vars(target, {var_name: cleaned})
        origin_src = str(target)

    session.cfg.key_origins[prov] = {"name": var_name, "from": origin_src}
    session.bus.publish("keys-changed", {"providers": key_state(session)})
    return {
        "provider": prov,
        "masked": mask_key(cleaned),
        "saved_to": origin_src if save else None,
    }


def clear_provider_key(session: Session, provider: str, *, save: bool = False) -> dict[str, Any]:
    prov = provider.strip().lower()
    var_name = key_var_for(prov)
    session.cfg.keys[prov] = None
    session.cfg.key_origins.pop(prov, None)
    if save:
        target = env_file_path(session.cfg.home)
        set_env_vars(target, {var_name: None})
    session.bus.publish("keys-changed", {"providers": key_state(session)})
    return {"provider": prov, "cleared": True}