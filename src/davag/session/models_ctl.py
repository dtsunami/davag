"""Live model/provider/effort switching shared by `/model`, TUI pane, and `POST /api/model`."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from davag.config.env import EFFORTS, PROVIDERS, load_config
from davag.config.models import MODELS
from davag.config.settings import load_settings, save_settings, settings_to_defaults

from .bootstrap import Session


def provider_catalog_state(session: Session) -> dict[str, Any]:
    cfg = session.cfg
    return {
        "current_provider": cfg.provider,
        "current_model": asdict(cfg.model),
        "effort": cfg.effort,
        "efforts": list(EFFORTS),
        "providers": [
            {
                "name": p,
                "has_key": bool(cfg.keys.get(p)),
                "models": [asdict(m) for m in MODELS if m.provider == p],
            }
            for p in PROVIDERS
        ],
    }


def apply_model_change(
    session: Session,
    *,
    provider: str | None = None,
    model_id: str | None = None,
    effort: str | None = None,
    persist: bool = False,
) -> dict[str, Any]:
    """Validate and apply a live model/provider/effort switch through `load_config`."""
    cur = session.cfg
    want_prov = (provider or cur.provider).strip().lower()
    want_eff = (effort or cur.effort).strip().lower()

    overrides: dict[str, str] = {
        "DAVAG_PROVIDER": want_prov,
        "DAVAG_EFFORT": want_eff,
    }
    if model_id:
        overrides["DAVAG_MODEL"] = model_id.strip()
    elif want_prov == cur.provider:
        overrides["DAVAG_MODEL"] = cur.model.id
    if cur.yolo:
        overrides["DAVAG_YOLO"] = "true"
    if cur.web_enabled:
        overrides["DAVAG_WEB"] = "true"
        overrides["DAVAG_WEB_HOST"] = cur.web_host
        overrides["DAVAG_WEB_PORT"] = str(cur.web_port)

    defaults = settings_to_defaults(load_settings(cur.home))
    new_cfg = load_config(
        cwd=cur.cwd,
        defaults=defaults,
        overrides=overrides,
        home=cur.home,
        davai_home=cur.davai_home,
    )
    # Preserve any in-memory session keys set via `/keys` without `--save` and resumed ro_dirs
    for p_name, k_val in cur.keys.items():
        if k_val and not new_cfg.keys.get(p_name):
            new_cfg.keys[p_name] = k_val
            new_cfg.key_origins[p_name] = cur.key_origins.get(p_name, {"name": "session", "from": "session"})
    for ro_p in cur.ro_dirs:
        if ro_p != new_cfg.cwd and ro_p not in new_cfg.ro_dirs:
            new_cfg.ro_dirs.append(ro_p)
    session.sandbox.ro_dirs = list(new_cfg.ro_dirs)

    session.cfg = new_cfg
    session.agent.cfg = new_cfg
    session.ledger.limit = new_cfg.context_limit

    persisted_note: str | None = None
    if persist:
        save_settings(
            new_cfg.home,
            {
                "provider": new_cfg.provider,
                "model": new_cfg.model.id,
                "effort": new_cfg.effort,
            },
        )
        env_origin = new_cfg.var_origins.get("DAVAG_MODEL")
        if env_origin and env_origin not in ("settings.json", "command-line flag"):
            persisted_note = (
                f"Saved to settings.json, Note: {env_origin} sets DAVAG_MODEL and will "
                "take precedence on future runs unless updated."
            )

    payload = {
        "provider": new_cfg.provider,
        "model": asdict(new_cfg.model),
        "effort": new_cfg.effort,
        "note": persisted_note,
    }
    session.bus.publish("model-changed", payload)
    return payload