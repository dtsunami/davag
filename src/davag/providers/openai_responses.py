"""OpenAI Responses API adapter (`AsyncOpenAI.responses.create`).

Streams reasoning summaries (`response.reasoning_summary_text.delta`) alongside output text,
sets `store=False`, and strips `reasoning` automatically on 400 if a non-reasoning model is selected.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from openai import APIStatusError, AsyncOpenAI

from .base import OPENAI_EFFORT, StreamEvent, video_unsupported, wrap_provider_error


def _to_responses_item(msg: dict[str, Any]) -> dict[str, Any]:
    is_assistant = msg["role"] == "assistant"
    text_type = "output_text" if is_assistant else "input_text"
    parts: list[dict[str, Any]] = []
    for p in msg.get("content", []):
        ptype = p.get("type", "text")
        if ptype == "image":
            parts.append(
                {
                    "type": "input_image",
                    "image_url": f"data:{p['media_type']};base64,{p['data']}",
                }
            )
        elif ptype == "video":
            raise video_unsupported("openai")
        else:
            parts.append({"type": text_type, "text": str(p.get("text", ""))})
    return {"role": msg["role"], "content": parts}


async def translate_responses_stream(
    stream: Any, *, provider_name: str = "openai", cancel_event: Any = None
) -> AsyncIterator[StreamEvent]:
    usage_obj: Any = None
    stop_reason = "end_turn"
    refusal_detail = ""

    try:
        async for event in stream:
            if cancel_event is not None and cancel_event.is_set():
                break
            ev_type = str(getattr(event, "type", "") or "")
            if ev_type == "response.output_text.delta":
                delta = getattr(event, "delta", "")
                if delta:
                    yield StreamEvent(t="text", delta=str(delta))
            elif ev_type in (
                "response.reasoning_summary_text.delta",
                "response.reasoning_summary.delta",
            ):
                delta = getattr(event, "delta", "")
                if delta:
                    yield StreamEvent(t="thinking", delta=str(delta))
            elif ev_type == "response.refusal.delta":
                refusal_detail += str(getattr(event, "delta", ""))
            elif ev_type == "response.completed":
                resp = getattr(event, "response", None)
                usage_obj = getattr(resp, "usage", None)
                status = str(getattr(resp, "status", "") or "")
                if status == "incomplete":
                    stop_reason = "max_tokens"
    except Exception as err:
        raise wrap_provider_error(err, provider_name) from err

    if cancel_event is not None and cancel_event.is_set():
        return

    if usage_obj is not None:
        prompt = int(getattr(usage_obj, "input_tokens", 0) or 0)
        out_toks = int(getattr(usage_obj, "output_tokens", 0) or 0)
        details = getattr(usage_obj, "input_tokens_details", None)
        cached = int(getattr(details, "cached_tokens", 0) or 0) if details else 0
        yield StreamEvent(
            t="usage",
            in_fresh=max(0, prompt - cached),
            out=out_toks,
            cache_read=cached,
            cache_write=0,
            context_in=prompt,
        )

    if refusal_detail:
        yield StreamEvent(t="stop", reason="refusal", detail=refusal_detail)
    else:
        yield StreamEvent(t="stop", reason=stop_reason)


class OpenAIResponsesAdapter:
    name = "openai"

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        max_tokens: int,
        effort: str = "high",
        temperature: float | None = None,
        thinking_visible: bool = True,
        base_url: str | None = None,
    ) -> None:
        self.model = model
        self.max_tokens = max_tokens
        self.effort = effort
        self.temperature = temperature
        self.thinking_visible = thinking_visible
        self._client = AsyncOpenAI(
            api_key=api_key, base_url=base_url, max_retries=2, timeout=600.0
        )

    async def send(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        cancel_event: Any = None,
    ) -> AsyncIterator[StreamEvent]:
        params: dict[str, Any] = {
            "model": self.model,
            "instructions": system,
            "input": [_to_responses_item(m) for m in messages],
            "max_output_tokens": self.max_tokens,
            "stream": True,
            "store": False,
        }
        if self.temperature is not None:
            params["temperature"] = self.temperature
        if self.effort:
            r_cfg: dict[str, Any] = {"effort": OPENAI_EFFORT.get(self.effort, "medium")}
            if self.thinking_visible:
                r_cfg["summary"] = "auto"
            params["reasoning"] = r_cfg

        try:
            stream = await self._client.responses.create(**params)
        except APIStatusError as err:
            if err.status_code == 400 and "reasoning" in params:
                params.pop("reasoning", None)
                try:
                    stream = await self._client.responses.create(**params)
                except Exception as retry_err:
                    raise wrap_provider_error(retry_err, "openai") from retry_err
            else:
                raise wrap_provider_error(err, "openai") from err
        except Exception as err:
            raise wrap_provider_error(err, "openai") from err

        async for ev in translate_responses_stream(stream, cancel_event=cancel_event):
            yield ev

    async def count_tokens(
        self, *, system: str, messages: list[dict[str, Any]]
    ) -> int | None:
        return None