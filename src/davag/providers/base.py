"""Shared provider wire types, error normalization, and cache-aware token cost math."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Protocol

from davag.config.models import ModelSpec

OPENAI_EFFORT: dict[str, str] = {
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "high",
    "max": "high",
}

OPENROUTER_EFFORT: dict[str, str] = {
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "xhigh",
    "max": "xhigh",
}


class ProviderError(Exception):
    """Normalized provider exception preserving HTTP status, retryability, and auth diagnostics."""

    def __init__(
        self,
        message: str,
        *,
        provider: str,
        status: int | None = None,
        retryable: bool = False,
        detail: str = "",
    ) -> None:
        super().__init__(message)
        self.provider = provider
        self.status = status
        self.retryable = retryable
        self.detail = detail
        self.auth_context: str | None = None


def wrap_provider_error(err: Exception, provider: str) -> ProviderError:
    if isinstance(err, ProviderError):
        return err
    status = getattr(err, "status_code", None) or getattr(err, "code", None)
    if not isinstance(status, int):
        status = None
    retryable = status in (408, 429, 500, 502, 503, 504, 529)
    msg = str(err) or type(err).__name__
    return ProviderError(
        f"[{provider}] {msg}",
        provider=provider,
        status=status,
        retryable=retryable,
        detail=msg,
    )


def video_unsupported(provider: str) -> ProviderError:
    return ProviderError(
        f'Provider "{provider}" does not accept raw video attachments natively. '
        "Switch `/model gemini-3-pro-preview` (or `gemini-2.5-flash`) to watch video directly, "
        "or use `timeline.grab_still` + `media.transcribe`.",
        provider=provider,
        status=400,
        retryable=False,
    )


@dataclass
class StreamEvent:
    """Canonical event yielded by every provider adapter:
    - `t="text"`: `delta` string
    - `t="thinking"`: `delta` reasoning/thought summary string
    - `t="usage"`: `in_fresh`, `out`, `cache_read`, `cache_write`, `context_in`
    - `t="tool_calls"`: `calls` list of `{id, name, args}` (salvaged if a model emits native calls)
    - `t="stop"`: `reason` ("end_turn" | "max_tokens" | "refusal" | "tool_calls"), `detail`
    """

    t: str
    delta: str = ""
    in_fresh: int = 0
    out: int = 0
    cache_read: int = 0
    cache_write: int = 0
    context_in: int = 0
    reason: str = ""
    detail: str = ""
    calls: list[dict[str, Any]] = field(default_factory=list)


class ProviderAdapter(Protocol):
    name: str
    model: str

    def send(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        cancel_event: Any = None,
    ) -> AsyncIterator[StreamEvent]: ...

    async def count_tokens(
        self, *, system: str, messages: list[dict[str, Any]]
    ) -> int | None: ...


# Cache multipliers per provider (matches davai's conservative exact/published rates)
CACHE_READ_RATE: dict[str, float] = {
    "anthropic": 0.10,
    "openai": 0.50,
    "gemini": 0.25,
    "grok": 0.25,
    "openrouter": 0.50,
}
CACHE_WRITE_RATE: dict[str, float] = {
    "anthropic": 1.25,
}


def cost_of_usage(spec: ModelSpec, usage: StreamEvent) -> float:
    """Calculate USD cost for a single turn's normalized `usage` event without double-billing cache."""
    if spec.input_price is None or spec.output_price is None:
        return 0.0
    r_mult = CACHE_READ_RATE.get(spec.provider, 0.50)
    w_mult = CACHE_WRITE_RATE.get(spec.provider, 1.0)
    in_cost = (
        usage.in_fresh * spec.input_price
        + usage.cache_read * spec.input_price * r_mult
        + usage.cache_write * spec.input_price * w_mult
    ) / 1_000_000.0
    out_cost = (usage.out * spec.output_price) / 1_000_000.0
    return in_cost + out_cost