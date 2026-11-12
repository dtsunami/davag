"""OpenAI Chat Completions adapter (serves OpenRouter, Grok, local vLLM/Ollama/IPEX-LLM,
and `DAVAG_OPENAI_SURFACE=chat`).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from openai import APIStatusError, AsyncOpenAI

from .base import (
    OPENAI_EFFORT,
    OPENROUTER_EFFORT,
    StreamEvent,
    video_unsupported,
    wrap_provider_error,
)


def _to_chat_message(msg: dict[str, Any], provider_name: str) -> dict[str, Any]:
    parts = msg.get("content", [])
    if len(parts) == 1 and parts[0].get("type", "text") == "text":
        return {"role": msg["role"], "content": str(parts[0].get("text", ""))}

    out_parts: list[dict[str, Any]] = []
    for p in parts:
        ptype = p.get("type", "text")
        if ptype == "image":
            out_parts.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{p['media_type']};base64,{p['data']}"},
                }
            )
        elif ptype == "video":
            raise video_unsupported(provider_name)
        else:
            out_parts.append({"type": "text", "text": str(p.get("text", ""))})
    return {"role": msg["role"], "content": out_parts}


class OpenAIChatAdapter:
    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        max_tokens: int,
        effort: str = "high",
        temperature: float | None = None,
        base_url: str | None = None,
        default_headers: dict[str, str] | None = None,
        name: str = "openai",
    ) -> None:
        self.name = name
        self.model = model
        self.max_tokens = max_tokens
        self.effort = effort
        self.temperature = temperature
        self._client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            default_headers=default_headers,
            max_retries=2,
            timeout=600.0,
        )

    async def send(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        cancel_event: Any = None,
    ) -> AsyncIterator[StreamEvent]:
        chat_msgs = [
            {"role": "system", "content": system},
            *[_to_chat_message(m, self.name) for m in messages],
        ]
        params: dict[str, Any] = {
            "model": self.model,
            "max_completion_tokens": self.max_tokens,
            "messages": chat_msgs,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if self.temperature is not None:
            params["temperature"] = self.temperature

        extra_body: dict[str, Any] = {}
        if self.effort:
            if self.name == "openrouter":
                extra_body["reasoning"] = {
                    "effort": OPENROUTER_EFFORT.get(self.effort, "medium")
                }
            else:
                params["reasoning_effort"] = OPENAI_EFFORT.get(self.effort, "medium")
        if extra_body:
            params["extra_body"] = extra_body

        try:
            stream = await self._client.chat.completions.create(**params)
        except APIStatusError as err:
            hint = "extra_body" if "extra_body" in params else "reasoning_effort" if "reasoning_effort" in params else None
            if err.status_code == 400 and hint:
                params.pop(hint, None)
                try:
                    stream = await self._client.chat.completions.create(**params)
                except Exception as retry_err:
                    raise wrap_provider_error(retry_err, self.name) from retry_err
            else:
                raise wrap_provider_error(err, self.name) from err
        except Exception as err:
            raise wrap_provider_error(err, self.name) from err

        usage_obj: Any = None
        finish = "end_turn"
        tool_calls: dict[int, dict[str, str]] = {}

        try:
            async for chunk in stream:
                if cancel_event is not None and cancel_event.is_set():
                    await stream.close()
                    return
                if getattr(chunk, "usage", None) is not None:
                    usage_obj = chunk.usage
                choices = getattr(chunk, "choices", None) or []
                if not choices:
                    continue
                choice = choices[0]
                delta = getattr(choice, "delta", None)
                if delta is not None:
                    content = getattr(delta, "content", None)
                    if isinstance(content, str) and content:
                        yield StreamEvent(t="text", delta=content)
                    elif isinstance(content, list):
                        for item in content:
                            if isinstance(item, dict) and item.get("text"):
                                yield StreamEvent(t="text", delta=str(item["text"]))

                    r_delta = getattr(delta, "reasoning_content", None) or getattr(
                        delta, "reasoning", None
                    )
                    if isinstance(r_delta, str) and r_delta:
                        yield StreamEvent(t="thinking", delta=r_delta)

                    for tc in getattr(delta, "tool_calls", None) or []:
                        idx = int(getattr(tc, "index", 0))
                        slot = tool_calls.setdefault(idx, {"id": "", "name": "", "args": ""})
                        if getattr(tc, "id", None):
                            slot["id"] = str(tc.id)
                        fn = getattr(tc, "function", None)
                        if fn is not None:
                            if getattr(fn, "name", None):
                                slot["name"] += str(fn.name)
                            if getattr(fn, "arguments", None):
                                slot["args"] += str(fn.arguments)

                if getattr(choice, "finish_reason", None):
                    finish = str(choice.finish_reason)
        except Exception as err:
            raise wrap_provider_error(err, self.name) from err

        if tool_calls:
            yield StreamEvent(
                t="tool_calls",
                calls=[tool_calls[k] for k in sorted(tool_calls.keys())],
            )

        if usage_obj is not None:
            prompt = int(getattr(usage_obj, "prompt_tokens", 0) or 0)
            out_toks = int(getattr(usage_obj, "completion_tokens", 0) or 0)
            details = getattr(usage_obj, "prompt_tokens_details", None)
            cached = int(getattr(details, "cached_tokens", 0) or 0) if details else 0
            yield StreamEvent(
                t="usage",
                in_fresh=max(0, prompt - cached),
                out=out_toks,
                cache_read=cached,
                cache_write=0,
                context_in=prompt,
            )

        stop = (
            "max_tokens"
            if finish == "length"
            else "tool_calls"
            if finish == "tool_calls"
            else "end_turn"
        )
        yield StreamEvent(t="stop", reason=stop)

    async def count_tokens(
        self, *, system: str, messages: list[dict[str, Any]]
    ) -> int | None:
        return None