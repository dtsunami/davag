"""Anthropic Claude adapter (`AsyncAnthropic`).

Supports:
- Claude Opus 5 / Opus 5.5 / Sonnet 5 adaptive thinking (`thinking={"type": "adaptive"}`)
- `output_config={"effort": effort}`
- Ephemeral prompt prefix caching (`cache_control={"type": "ephemeral"}` on system + rolling tail)
- Exact pre-flight token counting via `messages.count_tokens`
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from anthropic import AsyncAnthropic

from .base import StreamEvent, video_unsupported, wrap_provider_error


def _to_anthropic_message(msg: dict[str, Any]) -> dict[str, Any]:
    blocks: list[dict[str, Any]] = []
    for p in msg.get("content", []):
        ptype = p.get("type", "text")
        if ptype == "image":
            blocks.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": p["media_type"],
                        "data": p["data"],
                    },
                }
            )
        elif ptype == "video":
            raise video_unsupported("anthropic")
        else:
            blocks.append({"type": "text", "text": str(p.get("text", ""))})
    return {"role": msg["role"], "content": blocks}


def _with_cache_breakpoints(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Mark the last content block of the penultimate and final user turns ephemeral so
    multi-step agent loops hit Anthropic's prefix cache at 0.1x input cost!
    """
    if not messages:
        return messages
    out = [
        {"role": m["role"], "content": [dict(b) for b in m["content"]]}
        for m in messages
    ]
    # Anthropic allows up to 4 cache_control blocks total (1 is used on system); mark last 2 messages
    marked = 0
    for idx in range(len(out) - 1, -1, -1):
        content = out[idx]["content"]
        if content:
            content[-1]["cache_control"] = {"type": "ephemeral"}
            marked += 1
            if marked >= 2:
                break
    return out


class AnthropicAdapter:
    name = "anthropic"

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        max_tokens: int,
        effort: str = "high",
        temperature: float | None = None,
        thinking_visible: bool = True,
    ) -> None:
        self.model = model
        self.max_tokens = max_tokens
        self.effort = effort
        self.temperature = temperature
        self.thinking_visible = thinking_visible
        self._client = AsyncAnthropic(api_key=api_key, max_retries=2, timeout=600.0)

    async def send(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        cancel_event: Any = None,
    ) -> AsyncIterator[StreamEvent]:
        anth_msgs = _with_cache_breakpoints([_to_anthropic_message(m) for m in messages])
        params: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "messages": anth_msgs,
        }

        if "haiku" in self.model:
            if self.temperature is not None:
                params["temperature"] = self.temperature
        else:
            thinking_cfg: dict[str, Any] = {"type": "adaptive"}
            if self.thinking_visible:
                thinking_cfg["display"] = "summarized"
            params["thinking"] = thinking_cfg
            params["output_config"] = {"effort": self.effort}

        try:
            async with self._client.messages.stream(**params) as stream:
                async for event in stream:
                    if cancel_event is not None and cancel_event.is_set():
                        await stream.close()
                        break
                    ev_type = getattr(event, "type", "")
                    if ev_type == "content_block_delta":
                        delta = getattr(event, "delta", None)
                        dtype = getattr(delta, "type", "")
                        if dtype == "text_delta":
                            yield StreamEvent(t="text", delta=getattr(delta, "text", ""))
                        elif dtype == "thinking_delta":
                            yield StreamEvent(t="thinking", delta=getattr(delta, "thinking", ""))

                if cancel_event is not None and cancel_event.is_set():
                    return

                final = await stream.get_final_message()
                usage = final.usage
                in_fresh = int(getattr(usage, "input_tokens", 0) or 0)
                cache_read = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
                cache_write = int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
                out_toks = int(getattr(usage, "output_tokens", 0) or 0)

                yield StreamEvent(
                    t="usage",
                    in_fresh=in_fresh,
                    out=out_toks,
                    cache_read=cache_read,
                    cache_write=cache_write,
                    context_in=in_fresh + cache_read + cache_write,
                )

                stop_reason = getattr(final, "stop_reason", None) or "end_turn"
                if stop_reason == "refusal":
                    yield StreamEvent(t="stop", reason="refusal", detail="declined by model")
                else:
                    yield StreamEvent(t="stop", reason=str(stop_reason))
        except Exception as err:
            raise wrap_provider_error(err, "anthropic") from err

    async def count_tokens(
        self, *, system: str, messages: list[dict[str, Any]]
    ) -> int | None:
        try:
            res = await self._client.messages.count_tokens(
                model=self.model,
                system=system,
                messages=[_to_anthropic_message(m) for m in messages],
            )
            return int(res.input_tokens)
        except Exception:  # noqa: BLE001
            return None