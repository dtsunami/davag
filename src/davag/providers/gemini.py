"""Google Gemini adapter (`google-genai`) with full Multimodal Video + Files API + YouTube support!

Three native video paths:
1. Inline bytes (`inlineData`) when `data` (base64) is present (<= 18 MB).
2. **Gemini Files API** (`client.aio.files.upload` + active state polling) when `file_path` is
   present on a video part (> 18 MB up to 2 GB — cached by file path across turns in the session!).
3. **YouTube URL** (`fileData(file_uri=uri)`) when `uri` is present.
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from google import genai
from google.genai import types

from .base import ProviderError, StreamEvent, wrap_provider_error

# Session-level cache so an uploaded >18MB clip isn't re-uploaded on every subsequent turn!
_UPLOADED_FILES_CACHE: dict[str, tuple[str, str]] = {}  # abs_path -> (file_uri, mime_type)


class GeminiAdapter:
    name = "gemini"

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        max_tokens: int,
        temperature: float | None = None,
        thinking_visible: bool = True,
        base_url: str | None = None,
    ) -> None:
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.thinking_visible = thinking_visible
        http_opts = types.HttpOptions(base_url=base_url, timeout=210_000) if base_url else types.HttpOptions(timeout=210_000)
        self._client = genai.Client(api_key=api_key, http_options=http_opts)

    async def _ensure_uploaded(self, file_path: str, media_type: str) -> types.Part:
        key = str(Path(file_path).resolve())
        cached = _UPLOADED_FILES_CACHE.get(key)
        if cached is not None:
            uri, mime = cached
            return types.Part.from_uri(file_uri=uri, mime_type=mime)

        uploaded = await self._client.aio.files.upload(
            file=key,
            config=types.UploadFileConfig(mime_type=media_type),
        )
        file_name = uploaded.name
        assert file_name is not None

        # Poll until Gemini finishes processing the video stream (`ACTIVE`)
        for _ in range(120):
            state_str = str(getattr(uploaded.state, "name", uploaded.state) or "").upper()
            if "ACTIVE" in state_str:
                break
            if "FAILED" in state_str:
                raise ProviderError(
                    f"Gemini Files API processing failed for {Path(key).name}",
                    provider="gemini",
                )
            await asyncio.sleep(1.5)
            uploaded = await self._client.aio.files.get(name=file_name)

        uri = str(uploaded.uri)
        mime = str(uploaded.mime_type or media_type)
        _UPLOADED_FILES_CACHE[key] = (uri, mime)
        return types.Part.from_uri(file_uri=uri, mime_type=mime)

    async def _to_gemini_contents(self, messages: list[dict[str, Any]]) -> list[types.Content]:
        contents: list[types.Content] = []
        for msg in messages:
            role = "model" if msg["role"] == "assistant" else "user"
            parts: list[types.Part] = []
            for p in msg.get("content", []):
                ptype = p.get("type", "text")
                if ptype in ("image", "video"):
                    if p.get("uri"):
                        # YouTube URL by reference (mime_type omitted on purpose for YT streams)
                        mime = p.get("media_type")
                        if mime:
                            parts.append(types.Part.from_uri(file_uri=str(p["uri"]), mime_type=mime))
                        else:
                            parts.append(types.Part(file_data=types.FileData(file_uri=str(p["uri"]))))
                    elif p.get("file_path"):
                        part = await self._ensure_uploaded(
                            str(p["file_path"]),
                            str(p.get("media_type") or "video/mp4"),
                        )
                        parts.append(part)
                    else:
                        raw_bytes = base64.b64decode(p["data"])
                        parts.append(
                            types.Part.from_bytes(
                                data=raw_bytes,
                                mime_type=str(p.get("media_type") or "image/png"),
                            )
                        )
                else:
                    parts.append(types.Part.from_text(text=str(p.get("text", ""))))
            contents.append(types.Content(role=role, parts=parts))
        return contents

    async def send(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        cancel_event: Any = None,
    ) -> AsyncIterator[StreamEvent]:
        try:
            contents = await self._to_gemini_contents(messages)
            cfg = types.GenerateContentConfig(
                system_instruction=system,
                max_output_tokens=self.max_tokens,
                temperature=self.temperature,
                thinking_config=(
                    types.ThinkingConfig(include_thoughts=True) if self.thinking_visible else None
                ),
            )

            # 210s TTFT / per-chunk stall guard feeding cleanly into loop.py's automatic retry
            try:
                stream = await asyncio.wait_for(
                    self._client.aio.models.generate_content_stream(
                        model=self.model,
                        contents=contents,
                        config=cfg,
                    ),
                    timeout=210.0,
                )
            except TimeoutError as t_err:
                raise ProviderError(
                    "[gemini] Timed out waiting >210s for stream open",
                    provider="gemini",
                    status=408,
                    retryable=True,
                ) from t_err

            usage_meta = None
            finish_reason = "end_turn"
            emitted_any = False
            aiter = stream.__aiter__()
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    break
                try:
                    chunk = await asyncio.wait_for(aiter.__anext__(), timeout=210.0)
                except StopAsyncIteration:
                    break
                except TimeoutError as t_err:
                    raise ProviderError(
                        "[gemini] Stream stalled >210s waiting for next chunk",
                        provider="gemini",
                        status=408,
                        retryable=not emitted_any,
                    ) from t_err
                emitted_any = True

                if getattr(chunk, "usage_metadata", None) is not None:
                    usage_meta = chunk.usage_metadata
                cands = getattr(chunk, "candidates", None) or []
                if cands:
                    cand = cands[0]
                    if getattr(cand, "finish_reason", None):
                        finish_reason = str(getattr(cand.finish_reason, "name", cand.finish_reason))
                    content = getattr(cand, "content", None)
                    for part in getattr(content, "parts", None) or []:
                        txt = getattr(part, "text", None)
                        if txt:
                            is_thought = bool(getattr(part, "thought", False))
                            yield StreamEvent(t="thinking" if is_thought else "text", delta=txt)

            if cancel_event is not None and cancel_event.is_set():
                return

            if usage_meta is not None:
                cached = int(getattr(usage_meta, "cached_content_token_count", 0) or 0)
                prompt = int(getattr(usage_meta, "prompt_token_count", 0) or 0)
                cand_out = int(getattr(usage_meta, "candidates_token_count", 0) or 0)
                thought_out = int(getattr(usage_meta, "thoughts_token_count", 0) or 0)
                yield StreamEvent(
                    t="usage",
                    in_fresh=max(0, prompt - cached),
                    out=cand_out + thought_out,
                    cache_read=cached,
                    cache_write=0,
                    context_in=prompt,
                )

            stop = (
                "max_tokens"
                if "MAX_TOKENS" in finish_reason
                else "refusal"
                if any(k in finish_reason for k in ("SAFETY", "PROHIBITED"))
                else "end_turn"
            )
            yield StreamEvent(t="stop", reason=stop)
        except Exception as err:
            raise wrap_provider_error(err, "gemini") from err

    async def count_tokens(
        self, *, system: str, messages: list[dict[str, Any]]
    ) -> int | None:
        try:
            contents = await self._to_gemini_contents(messages)
            res = await self._client.aio.models.count_tokens(
                model=self.model,
                contents=contents,
                config=types.CountTokensConfig(system_instruction=system),
            )
            return int(res.total_tokens) if res.total_tokens is not None else None
        except Exception:  # noqa: BLE001
            return None