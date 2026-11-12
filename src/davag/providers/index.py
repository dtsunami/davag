"""Provider factory, key masking, auth diagnostics (`sk-or-` detection), and live catalog queries."""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import aiohttp

from davag.config.env import Config
from davag.config.models import ModelSpec

from .anthropic import AnthropicAdapter
from .base import ProviderAdapter, ProviderError, StreamEvent
from .gemini import GeminiAdapter
from .openai_chat import OpenAIChatAdapter
from .openai_responses import OpenAIResponsesAdapter

GROK_BASE_URL = "https://api.x.ai/v1"
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

DEFAULT_ENDPOINTS: dict[str, str] = {
    "anthropic": "https://api.anthropic.com/v1",
    "gemini": "https://generativelanguage.googleapis.com",
    "openai": "https://api.openai.com/v1",
    "grok": GROK_BASE_URL,
    "openrouter": OPENROUTER_BASE_URL,
}

KEY_PREFIX_RE = re.compile(r"^(sk-or-v1-|sk-or-|sk-ant-|sk-proj-|sk-svcacct-|sk-|xai-|AIza)")


def mask_key(key: str | None) -> str:
    """Describe an API key safely (issuer prefix, length, last 4 chars, whitespace/quote warnings)."""
    if not key:
        return "(none)"
    k = str(key)
    m = KEY_PREFIX_RE.match(k)
    parts = [f"{len(k)} chars"]
    if m:
        parts.append(f"starts {m.group(1)}")
    if len(k) >= 16:
        parts.append(f"ends …{k[-4:]}")
    notes: list[str] = []
    if k != k.strip():
        notes.append("leading/trailing whitespace")
    if any(ord(c) < 32 or ord(c) == 127 for c in k):
        notes.append("control characters")
    if len(k) >= 2 and k[0] in ('"', "'", "`") and k[-1] == k[0]:
        notes.append("quote characters")
    suffix = f" — contains {', '.join(notes)}" if notes else ""
    return ", ".join(parts) + suffix


def _endpoint_of(cfg: Config) -> str:
    base = cfg.base_url or DEFAULT_ENDPOINTS.get(cfg.provider, "(provider default)")
    if cfg.provider == "anthropic":
        return f"{base}/messages"
    if cfg.provider == "gemini":
        return base
    route = "responses" if (cfg.provider == "openai" and cfg.openai_surface != "chat") else "chat/completions"
    return f"{base}/{route}"


def describe_auth(cfg: Config, model_id: str) -> str:
    key = cfg.keys.get(cfg.provider)
    ko = cfg.key_origins.get(cfg.provider) or {}
    key_src = f"{ko.get('name')} from {ko.get('from')}" if ko.get("name") else "not set"
    lines = [
        f"  endpoint  {_endpoint_of(cfg)}",
        f"  model     {model_id}",
        f"  key       {mask_key(key)}  ({key_src})",
    ]
    if cfg.provider == "openai" and not cfg.base_url and key and key.startswith("sk-or-"):
        lines.append(
            "  note      this looks like an OpenRouter key (sk-or-…) sent to OpenAI. "
            "Set DAVAG_PROVIDER=openrouter in ~/.davag/.env."
        )
    return "\n".join(lines)


class _DiagnosedAdapter:
    def __init__(self, inner: ProviderAdapter, cfg: Config, model_id: str) -> None:
        self._inner = inner
        self._cfg = cfg
        self.name = inner.name
        self.model = model_id

    async def send(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        cancel_event: Any = None,
    ) -> AsyncIterator[StreamEvent]:
        try:
            async for ev in self._inner.send(
                system=system, messages=messages, cancel_event=cancel_event
            ):
                yield ev
        except ProviderError as err:
            if err.status in (401, 403) and not err.auth_context:
                ctx = describe_auth(self._cfg, self.model)
                err.auth_context = ctx
                err.args = (f"{err}\n{ctx}",)
            raise

    async def count_tokens(
        self, *, system: str, messages: list[dict[str, Any]]
    ) -> int | None:
        return await self._inner.count_tokens(system=system, messages=messages)


def create_provider(
    cfg: Config,
    *,
    model_override: str | None = None,
    effort_override: str | None = None,
) -> ProviderAdapter:
    prov = cfg.provider
    model_id = model_override or cfg.model.id
    effort = effort_override or cfg.effort
    api_key = cfg.keys.get(prov)
    if not api_key:
        raise ProviderError(
            f'No API key configured for provider "{prov}". Run `/keys` or add it to ~/.davag/.env.\n'
            f"{describe_auth(cfg, model_id)}",
            provider=prov,
            status=401,
        )

    inner: ProviderAdapter
    if prov == "anthropic":
        inner = AnthropicAdapter(
            api_key=api_key,
            model=model_id,
            max_tokens=cfg.max_tokens,
            effort=effort,
            temperature=cfg.temperature,
            thinking_visible=cfg.thinking_visible,
        )
    elif prov == "gemini":
        inner = GeminiAdapter(
            api_key=api_key,
            model=model_id,
            max_tokens=cfg.max_tokens,
            temperature=cfg.temperature,
            thinking_visible=cfg.thinking_visible,
            base_url=cfg.base_url,
        )
    elif prov == "openai":
        if cfg.openai_surface == "chat":
            inner = OpenAIChatAdapter(
                api_key=api_key,
                model=model_id,
                max_tokens=cfg.max_tokens,
                effort=effort,
                temperature=cfg.temperature,
                base_url=cfg.base_url,
                name="openai",
            )
        else:
            inner = OpenAIResponsesAdapter(
                api_key=api_key,
                model=model_id,
                max_tokens=cfg.max_tokens,
                effort=effort,
                temperature=cfg.temperature,
                thinking_visible=cfg.thinking_visible,
                base_url=cfg.base_url,
            )
    elif prov == "grok":
        inner = OpenAIChatAdapter(
            api_key=api_key,
            model=model_id,
            max_tokens=cfg.max_tokens,
            effort=effort,
            temperature=cfg.temperature,
            base_url=cfg.base_url or GROK_BASE_URL,
            name="grok",
        )
    elif prov == "openrouter":
        inner = OpenAIChatAdapter(
            api_key=api_key,
            model=model_id,
            max_tokens=cfg.max_tokens,
            effort=effort,
            temperature=cfg.temperature,
            base_url=cfg.base_url or OPENROUTER_BASE_URL,
            default_headers={
                "HTTP-Referer": "https://github.com/davag-cli/davag",
                "X-Title": "davag",
            },
            name="openrouter",
        )
    else:
        raise ValueError(f'Unsupported provider "{prov}"')

    return _DiagnosedAdapter(inner, cfg, model_id)


async def fetch_openrouter_catalog(
    *, base_url: str = OPENROUTER_BASE_URL, cache_file: Path | None = None
) -> list[ModelSpec]:
    url = f"{base_url.rstrip('/')}/models"
    async with aiohttp.ClientSession() as http, http.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
        resp.raise_for_status()
        payload = await resp.json()

    if cache_file is not None:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps(payload), encoding="utf-8")

    specs: list[ModelSpec] = []
    for item in payload.get("data") or []:
        mid = str(item.get("id", "")).strip()
        if not mid:
            continue
        top = item.get("top_provider") or {}
        ctx = int(top.get("context_length") or item.get("context_length") or 128_000)
        max_out = int(top.get("max_completion_tokens") or 32_000)
        pricing = item.get("pricing") or {}
        try:
            in_p = float(pricing["prompt"]) * 1_000_000 if "prompt" in pricing else None
            out_p = float(pricing["completion"]) * 1_000_000 if "completion" in pricing else None
        except (ValueError, TypeError):
            in_p, out_p = None, None
        arch = item.get("architecture") or {}
        in_mods = arch.get("input_modalities") or []
        specs.append(
            ModelSpec(
                id=mid,
                provider="openrouter",
                label=str(item.get("name") or mid),
                context=ctx,
                max_output=max_out,
                vision="image" in in_mods,
                video=False,
                effort=True,
                thinking=True,
                input_price=in_p,
                output_price=out_p,
            )
        )
    return specs


async def list_remote_models(cfg: Config) -> list[str]:
    prov = cfg.provider
    if prov == "openrouter":
        specs = await fetch_openrouter_catalog(
            base_url=cfg.base_url or OPENROUTER_BASE_URL,
            cache_file=Path(cfg.home) / "cache" / "openrouter.json",
        )
        return sorted(s.id for s in specs)

    api_key = cfg.keys.get(prov)
    if not api_key:
        raise ProviderError(f"No API key configured for {prov}", provider=prov, status=401)

    if prov == "anthropic":
        from anthropic import AsyncAnthropic

        client = AsyncAnthropic(api_key=api_key)
        out: list[str] = []
        async for m in client.models.list():
            out.append(str(m.id))
        return sorted(out)

    if prov in ("openai", "grok"):
        from openai import AsyncOpenAI

        client = AsyncOpenAI(
            api_key=api_key,
            base_url=cfg.base_url or (GROK_BASE_URL if prov == "grok" else None),
        )
        res = await client.models.list()
        return sorted(str(m.id) for m in res.data)

    if prov == "gemini":
        from google import genai

        client = genai.Client(api_key=api_key)
        pager = await client.aio.models.list()
        out = []
        async for m in pager:
            name = str(getattr(m, "name", "") or "").removeprefix("models/")
            if name:
                out.append(name)
        return sorted(out)

    raise ValueError(f"Cannot list remote models for {prov}")