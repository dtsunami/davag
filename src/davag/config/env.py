"""DAVAG_HOME resolution, .env loading (with ~/.davai/.env key fallback), and validated Config."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .envfile import parse_dotenv, read_env, read_env_text
from .models import ModelSpec, resolve_model
from .profiles import parse_blocks

PROVIDERS = ("anthropic", "gemini", "openai", "grok", "openrouter")
EFFORTS = ("low", "medium", "high", "xhigh", "max")
TRANSPORTS = ("auto", "direct", "bridge")
BASE_URL_PROVIDERS = ("openai", "grok", "openrouter")
HOME_ONLY_VARS = ("DAVAG_BASE_URL", "DAVAI_BASE_URL", "OPENAI_BASE_URL")
DEFAULT_MAX_TOKENS = 32_000
DEFAULT_WEB_PORT = 7879
DEFAULT_BRIDGE_PORT = 18765

# Lookup order per provider: DAVAG_* first (what davag writes), then standard SDK env
# names, then DAVAI_* so an existing ~/.davai/.env works out of the box.
KEY_VARS: dict[str, tuple[str, ...]] = {
    "anthropic": ("DAVAG_ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY", "DAVAI_ANTHROPIC_API_KEY"),
    "gemini": (
        "DAVAG_GEMINI_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "DAVAI_GEMINI_API_KEY",
    ),
    "openai": ("DAVAG_OPENAI_API_KEY", "OPENAI_API_KEY", "DAVAI_OPENAI_API_KEY"),
    "grok": ("DAVAG_GROK_API_KEY", "XAI_API_KEY", "GROK_API_KEY", "DAVAI_GROK_API_KEY"),
    "openrouter": ("DAVAG_OPENROUTER_API_KEY", "OPENROUTER_API_KEY", "DAVAI_OPENROUTER_API_KEY"),
}

# Only key variables are inherited from ~/.davai/.env so davai's model/port/yolo settings
# never unexpectedly override davag's own defaults.
DAVAI_FALLBACK_KEYS: frozenset[str] = frozenset(
    var for group in KEY_VARS.values() for var in group
)


def key_var_for(provider: str) -> str:
    names = KEY_VARS.get(provider)
    if not names:
        raise ValueError(f'Unknown provider "{provider}"')
    return names[0]


def resolve_home(env: dict[str, str] | None = None) -> Path:
    e = env if env is not None else os.environ
    raw = e.get("DAVAG_HOME", "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return (Path.home() / ".davag").resolve()


def resolve_davai_home(env: dict[str, str] | None = None) -> Path:
    e = env if env is not None else os.environ
    raw = e.get("DAVAI_HOME", "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return (Path.home() / ".davai").resolve()


def _parse_bool(v: str | None, default: bool) -> bool:
    if v is None or v.strip() == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _parse_positive_int(v: str | None, name: str) -> int | None:
    if v is None or str(v).strip() == "":
        return None
    cleaned = str(v).strip().replace("_", "")
    try:
        n = int(cleaned)
    except ValueError as exc:
        raise ValueError(f'{name} must be a positive integer (got "{v}").') from exc
    if n <= 0:
        raise ValueError(f'{name} must be a positive integer (got "{v}").')
    return n


def _parse_port(v: str | None, name: str) -> int | None:
    if v is None or str(v).strip() == "":
        return None
    try:
        n = int(str(v).strip())
    except ValueError as exc:
        raise ValueError(f'{name} must be a port between 0 and 65535 (got "{v}").') from exc
    if n < 0 or n > 65535:
        raise ValueError(f'{name} must be a port between 0 and 65535 (got "{v}").')
    return n


def _parse_float(v: str | None, default: float) -> float:
    if v is None or str(v).strip() == "":
        return default
    try:
        return float(str(v).strip())
    except ValueError:
        return default


def resolve_limits(
    model: ModelSpec,
    *,
    max_tokens: int | None = None,
    context_limit: int | None = None,
) -> tuple[int, int, list[str]]:
    clamped: list[str] = []

    def cap(want: int, limit: int, name: str) -> int:
        if model.unverified or want <= limit:
            return want
        clamped.append(f"{name}={want} exceeds {model.id}'s limit of {limit}; using {limit}")
        return limit

    eff_max = (
        cap(max_tokens, model.max_output, "DAVAG_MAX_TOKENS")
        if max_tokens is not None
        else min(model.max_output, DEFAULT_MAX_TOKENS)
    )
    eff_ctx = (
        cap(context_limit, model.context, "DAVAG_CONTEXT_LIMIT")
        if context_limit is not None
        else model.context
    )
    return eff_max, eff_ctx, clamped


def _resolve_base_url(
    provider: str, merged: dict[str, str], origins: dict[str, str]
) -> tuple[str | None, dict[str, str | None] | None]:
    davag_url = merged.get("DAVAG_BASE_URL", "").strip()
    openai_url = merged.get("OPENAI_BASE_URL", "").strip() if provider == "openai" else ""
    raw = davag_url or openai_url
    if not raw:
        return None, None
    var_name = "DAVAG_BASE_URL" if davag_url else "OPENAI_BASE_URL"
    if provider not in BASE_URL_PROVIDERS:
        supported = ", ".join(BASE_URL_PROVIDERS)
        raise ValueError(
            f'{var_name} is set, but provider "{provider}" does not support a custom base URL '
            f"(supported: {supported}). For an OpenAI-compatible endpoint use "
            f"DAVAG_PROVIDER=openai; otherwise unset {var_name}."
        )
    parsed = urlparse(raw)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError(
            f'{var_name}="{raw}" must be an http or https URL (e.g. https://api.mistral.ai/v1).'
        )
    return raw.rstrip("/"), {"name": var_name, "from": origins.get(var_name)}


def _drop_project_only(
    merged: dict[str, str],
    *,
    home_vars: dict[str, str],
    project_vars: dict[str, str],
    home_env_file: Path,
    cwd_env_file: Path,
    real_env: dict[str, str],
    overrides: dict[str, str],
    origins: dict[str, str],
) -> list[str]:
    warnings: list[str] = []
    for name in HOME_ONLY_VARS:
        val = project_vars.get(name, "").strip()
        if not val:
            continue
        if real_env.get(name) or name in overrides:
            continue
        fallback = home_vars.get(name, "").strip()
        if fallback:
            merged[name] = fallback
            origins[name] = str(home_env_file)
        else:
            merged.pop(name, None)
            origins.pop(name, None)
        warnings.append(
            f"{cwd_env_file} sets {name}={val}, which was ignored: a project .env cannot "
            f"choose where your API key is sent."
            + (f" Using {fallback} from {home_env_file}." if fallback else "")
        )
    return warnings


@dataclass
class Config:
    home: Path
    davai_home: Path
    cwd: Path
    provider: str
    model: ModelSpec
    effort: str
    max_tokens: int
    context_limit: int
    limits_requested: dict[str, int | None]
    temperature: float | None
    compact_at: float
    max_cost: float
    thinking_visible: bool
    yolo: bool
    openai_surface: str  # "responses" | "chat"
    base_url: str | None
    base_url_origin: dict[str, str | None] | None
    transport: str  # "auto" | "direct" | "bridge"
    bridge_host: str
    bridge_port: int
    web_enabled: bool
    web_host: str
    web_port: int
    web_token: str | None
    web_open: bool
    web_clipboard: bool
    shell_timeout: float
    ro_dirs: list[Path] = field(default_factory=list)
    agents_files: list[str] = field(default_factory=list)
    agents_budget: int | None = None
    index_provider: str | None = None
    index_model: str | None = None
    keys: dict[str, str | None] = field(default_factory=dict)
    key_origins: dict[str, dict[str, str | None]] = field(default_factory=dict)
    var_origins: dict[str, str] = field(default_factory=dict)
    env_files: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_public_dict(self) -> dict[str, Any]:
        """Safe dictionary representation with API keys redacted."""
        return {
            "home": str(self.home),
            "davai_home": str(self.davai_home),
            "cwd": str(self.cwd),
            "provider": self.provider,
            "model": self.model.id,
            "effort": self.effort,
            "max_tokens": self.max_tokens,
            "context_limit": self.context_limit,
            "temperature": self.temperature,
            "compact_at": self.compact_at,
            "max_cost": self.max_cost,
            "thinking_visible": self.thinking_visible,
            "yolo": self.yolo,
            "openai_surface": self.openai_surface,
            "base_url": self.base_url,
            "base_url_origin": self.base_url_origin,
            "transport": self.transport,
            "bridge": {"host": self.bridge_host, "port": self.bridge_port},
            "web": {
                "enabled": self.web_enabled,
                "host": self.web_host,
                "port": self.web_port,
                "open_on_launch": self.web_open,
                "copy_on_launch": self.web_clipboard,
                "token": "set" if self.web_token else "ephemeral",
            },
            "keys": {p: ("set" if v else "—") for p, v in self.keys.items()},
            "key_origins": self.key_origins,
            "env_files": self.env_files,
            "warnings": self.warnings,
        }


def load_config(
    *,
    cwd: str | Path | None = None,
    defaults: dict[str, str] | None = None,
    overrides: dict[str, str] | None = None,
    environ: dict[str, str] | None = None,
    home: str | Path | None = None,
    davai_home: str | Path | None = None,
) -> Config:
    real_env = dict(os.environ if environ is None else environ)
    eff_cwd = Path(cwd or os.getcwd()).resolve()
    eff_home = Path(home).resolve() if home else resolve_home(real_env)
    eff_davai_home = Path(davai_home).resolve() if davai_home else resolve_davai_home(real_env)

    davai_env_file = eff_davai_home / ".env"
    home_env_file = eff_home / ".env"
    cwd_env_file = eff_cwd / ".env"

    merged: dict[str, str] = {}
    origins: dict[str, str] = {}
    env_files: list[str] = []

    # 1. Floor: settings.json defaults
    for k, v in (defaults or {}).items():
        if v is not None and v != "":
            merged[k] = str(v)
            origins[k] = "settings.json"

    # 2. Read-only key fallback from ~/.davai/.env (if present and distinct from ~/.davag/.env).
    # Also harvests keys parked in inactive `#---#` provider blocks when they do not collide
    # with an active assignment, so every provider configured in davai works out-of-the-box.
    if davai_env_file != home_env_file:
        davai_raw = read_env_text(davai_env_file)
        if davai_raw:
            davai_vars = parse_dotenv(davai_raw)
            adopted: dict[str, str] = {
                k: v for k, v in davai_vars.items() if k in DAVAI_FALLBACK_KEYS and v
            }
            for blk in parse_blocks(davai_raw)["blocks"]:
                for v in blk["vars"]:
                    if v["shadowed"] or v["name"] not in DAVAI_FALLBACK_KEYS:
                        continue
                    if v["name"] not in adopted:
                        val_parsed = parse_dotenv(f"{v['name']}={v['value']}").get(v["name"], "")
                        if val_parsed:
                            adopted[v["name"]] = val_parsed
            if adopted:
                env_files.append(str(davai_env_file))
                for k, v in adopted.items():
                    merged[k] = v
                    origins[k] = str(davai_env_file)

    # 3. Primary ~/.davag/.env then project ./.env
    home_vars = read_env(home_env_file)
    if home_vars:
        env_files.append(str(home_env_file))
        for k, v in home_vars.items():
            merged[k] = v
            origins[k] = str(home_env_file)

    project_vars: dict[str, str] = {}
    if cwd_env_file != home_env_file:
        project_vars = read_env(cwd_env_file)
        if project_vars:
            env_files.append(str(cwd_env_file))
            for k, v in project_vars.items():
                merged[k] = v
                origins[k] = str(cwd_env_file)

    # 4. Real OS environment
    for k, v in real_env.items():
        if v is not None and v != "":
            merged[k] = v
            origins[k] = "environment"

    # 5. Explicit CLI / live-session overrides
    eff_overrides = {k: v for k, v in (overrides or {}).items() if v is not None}
    for k, v in eff_overrides.items():
        merged[k] = str(v)
        origins[k] = "command-line flag"

    warnings = (
        []
        if cwd_env_file == home_env_file
        else _drop_project_only(
            merged,
            home_vars=home_vars,
            project_vars=project_vars,
            home_env_file=home_env_file,
            cwd_env_file=cwd_env_file,
            real_env=real_env,
            overrides=eff_overrides,
            origins=origins,
        )
    )

    provider = merged.get("DAVAG_PROVIDER", "anthropic").strip().lower()
    if provider not in PROVIDERS:
        raise ValueError(
            f'Unknown DAVAG_PROVIDER="{provider}" (expected one of: {", ".join(PROVIDERS)}).'
        )

    model = resolve_model(provider, merged.get("DAVAG_MODEL"))

    effort = merged.get("DAVAG_EFFORT", "high").strip().lower()
    if effort not in EFFORTS:
        raise ValueError(
            f'Invalid DAVAG_EFFORT="{effort}" (expected one of: {", ".join(EFFORTS)}).'
        )

    req_max = _parse_positive_int(merged.get("DAVAG_MAX_TOKENS"), "DAVAG_MAX_TOKENS")
    req_ctx = _parse_positive_int(merged.get("DAVAG_CONTEXT_LIMIT"), "DAVAG_CONTEXT_LIMIT")
    max_tokens, context_limit, clamp_notes = resolve_limits(
        model, max_tokens=req_max, context_limit=req_ctx
    )
    warnings.extend(clamp_notes)

    temp_raw = merged.get("DAVAG_TEMPERATURE", "").strip()
    temperature: float | None = None
    if temp_raw:
        try:
            temperature = float(temp_raw)
        except ValueError as exc:
            raise ValueError(f'DAVAG_TEMPERATURE must be a number (got "{temp_raw}").') from exc

    compact_at = _parse_float(merged.get("DAVAG_COMPACT_AT"), 0.75)
    if not (0.2 <= compact_at <= 0.98):
        raise ValueError(f"DAVAG_COMPACT_AT must be between 0.2 and 0.98 (got {compact_at}).")

    max_cost = _parse_float(merged.get("DAVAG_MAX_COST"), 0.0)
    thinking_visible = _parse_bool(merged.get("DAVAG_THINKING"), True)
    yolo = _parse_bool(merged.get("DAVAG_YOLO"), False)

    base_url, base_url_origin = _resolve_base_url(provider, merged, origins)

    surface_raw = (
        merged.get("DAVAG_OPENAI_SURFACE") or merged.get("DAVAG_OPENAI_API") or ""
    ).strip().lower()
    if surface_raw:
        if surface_raw not in ("responses", "chat"):
            raise ValueError(
                f'DAVAG_OPENAI_SURFACE must be "responses" or "chat" (got "{surface_raw}").'
            )
        openai_surface = surface_raw
    else:
        # Custom base URLs default to Chat Completions since most OpenAI-compatible servers
        # do not implement /v1/responses.
        openai_surface = "chat" if base_url else "responses"

    transport = merged.get("DAVAG_TRANSPORT", "auto").strip().lower()
    if transport not in TRANSPORTS:
        raise ValueError(
            f'Invalid DAVAG_TRANSPORT="{transport}" (expected one of: {", ".join(TRANSPORTS)}).'
        )

    bridge_host = merged.get("DAVAG_BRIDGE_HOST", "127.0.0.1").strip() or "127.0.0.1"
    bridge_port = _parse_port(merged.get("DAVAG_BRIDGE_PORT"), "DAVAG_BRIDGE_PORT")
    if bridge_port is None:
        bridge_port = DEFAULT_BRIDGE_PORT

    web_open = _parse_bool(merged.get("DAVAG_WEB_OPEN"), False)
    web_clipboard = _parse_bool(merged.get("DAVAG_WEB_CLIPBOARD"), False)
    web_enabled = _parse_bool(merged.get("DAVAG_WEB"), False) or web_open
    web_host = merged.get("DAVAG_WEB_HOST", "127.0.0.1").strip() or "127.0.0.1"
    web_port = _parse_port(merged.get("DAVAG_WEB_PORT"), "DAVAG_WEB_PORT")
    if web_port is None:
        web_port = DEFAULT_WEB_PORT
    web_token = merged.get("DAVAG_WEB_TOKEN", "").strip() or None

    shell_timeout = _parse_float(merged.get("DAVAG_SHELL_TIMEOUT"), 120.0)

    ro_raw = merged.get("DAVAG_RO_DIRS", "").strip()
    ro_dirs = [
        Path(p.strip()).resolve()
        for p in ro_raw.split(os.pathsep)
        if p.strip()
    ]

    agents_md_raw = merged.get("DAVAG_AGENTS_MD", "").strip()
    agents_files = [p.strip() for p in agents_md_raw.split(os.pathsep) if p.strip()]
    agents_budget = _parse_positive_int(merged.get("DAVAG_AGENTS_BUDGET"), "DAVAG_AGENTS_BUDGET")
    index_provider = merged.get("DAVAG_INDEX_PROVIDER", "").strip().lower() or None
    index_model = merged.get("DAVAG_INDEX_MODEL", "").strip() or None

    keys: dict[str, str | None] = {}
    key_origins: dict[str, dict[str, str | None]] = {}
    for prov, candidates in KEY_VARS.items():
        found_val: str | None = None
        for var in candidates:
            val = merged.get(var, "").strip()
            if val:
                found_val = val
                key_origins[prov] = {"name": var, "from": origins.get(var)}
                break
        keys[prov] = found_val

    return Config(
        home=eff_home,
        davai_home=eff_davai_home,
        cwd=eff_cwd,
        provider=provider,
        model=model,
        effort=effort,
        max_tokens=max_tokens,
        context_limit=context_limit,
        limits_requested={"max_tokens": req_max, "context_limit": req_ctx},
        temperature=temperature,
        compact_at=compact_at,
        max_cost=max_cost,
        thinking_visible=thinking_visible,
        yolo=yolo,
        openai_surface=openai_surface,
        base_url=base_url,
        base_url_origin=base_url_origin,
        transport=transport,
        bridge_host=bridge_host,
        bridge_port=bridge_port,
        web_enabled=web_enabled,
        web_host=web_host,
        web_port=web_port,
        web_token=web_token,
        web_open=web_open,
        web_clipboard=web_clipboard,
        shell_timeout=shell_timeout,
        ro_dirs=ro_dirs,
        agents_files=agents_files,
        agents_budget=agents_budget,
        index_provider=index_provider,
        index_model=index_model,
        keys=keys,
        key_origins=key_origins,
        var_origins=origins,
        env_files=env_files,
        warnings=warnings,
    )