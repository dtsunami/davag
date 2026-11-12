"""Typed, individually addressable context segment ledger (mirrors davai/src/context/ledger.js)."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from .tokens import (
    TokenCalibrator,
    estimate_image_tokens,
    estimate_text_tokens,
    estimate_video_tokens,
)

SEGMENT_TYPES = (
    "grounding",
    "user",
    "assistant",
    "op-result",
    "shell",
    "media",
    "paste",
    "summary",
)
PROTECTED_TYPES = frozenset({"grounding"})


@dataclass
class Segment:
    id: int
    type: str
    label: str
    role: str  # "user" | "assistant"
    tokens: int
    part: dict[str, Any]
    pinned: bool = False
    created_at: float = field(default_factory=time.time)

    def to_summary_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "label": self.label,
            "role": self.role,
            "tokens": self.tokens,
            "pinned": self.pinned,
            "protected": self.type in PROTECTED_TYPES,
            "created_at": self.created_at,
        }


class Ledger:
    def __init__(self, *, limit: int = 1_000_000, compact_at: float = 0.75) -> None:
        self.limit = limit
        self.compact_at = compact_at
        self.system: str = ""
        self.system_tokens: int = 0
        self.segments: list[Segment] = []
        self.calibrator = TokenCalibrator()
        self._next_id = 1
        self._compacting = False
        self._lock = asyncio.Lock()

    def set_system(self, text: str) -> None:
        self.system = text
        self.system_tokens = estimate_text_tokens(text)

    def add(
        self,
        *,
        seg_type: str,
        label: str,
        role: str = "user",
        text: str | None = None,
        image: dict[str, Any] | None = None,
        video: dict[str, Any] | None = None,
        pinned: bool = False,
    ) -> Segment:
        seg_id = self._next_id
        self._next_id += 1

        if image is not None:
            part = {
                "type": "image",
                "media_type": image["media_type"],
                "data": image["data"],
                "name": image.get("name", ""),
            }
            toks = int(image.get("tokens") or estimate_image_tokens(int(image.get("bytes", 0))))
        elif video is not None:
            if video.get("uri"):
                part = {
                    "type": "video",
                    "media_type": video.get("media_type"),
                    "uri": video["uri"],
                    "name": video.get("name", video["uri"]),
                }
            elif video.get("file_path"):
                part = {
                    "type": "video",
                    "media_type": video.get("media_type", "video/mp4"),
                    "file_path": video["file_path"],
                    "name": video.get("name", ""),
                }
            else:
                part = {
                    "type": "video",
                    "media_type": video["media_type"],
                    "data": video["data"],
                    "name": video.get("name", ""),
                }
            toks = int(
                video.get("tokens")
                or estimate_video_tokens(int(video.get("bytes", 0)), video.get("duration_sec"))
            )
        else:
            body = text or ""
            part = {"type": "text", "text": body}
            toks = estimate_text_tokens(body)

        seg = Segment(
            id=seg_id,
            type=seg_type,
            label=label,
            role=role,
            tokens=toks,
            part=part,
            pinned=pinned or (seg_type in PROTECTED_TYPES),
        )
        self.segments.append(seg)
        return seg

    @property
    def raw_tokens(self) -> int:
        return self.system_tokens + sum(s.tokens for s in self.segments)

    @property
    def total_tokens(self) -> int:
        return self.calibrator.scale(self.raw_tokens)

    @property
    def usage_fraction(self) -> float:
        return (self.total_tokens / self.limit) if self.limit > 0 else 0.0

    @property
    def needs_compaction(self) -> bool:
        return self.usage_fraction >= self.compact_at

    def observe_provider_usage(self, context_in: int) -> None:
        self.calibrator.observe(self.raw_tokens, context_in)

    def drop(self, seg_id: int) -> bool:
        for i, seg in enumerate(self.segments):
            if seg.id == seg_id:
                if seg.type in PROTECTED_TYPES or seg.pinned:
                    return False
                self.segments.pop(i)
                return True
        return False

    def toggle_pin(self, seg_id: int, pinned: bool | None = None) -> bool:
        for seg in self.segments:
            if seg.id == seg_id:
                if seg.type in PROTECTED_TYPES:
                    return True
                seg.pinned = (not seg.pinned) if pinned is None else bool(pinned)
                return seg.pinned
        return False

    def clear_conversation(self) -> int:
        before = len(self.segments)
        self.segments = [s for s in self.segments if s.type in PROTECTED_TYPES]
        return before - len(self.segments)

    def to_messages(self) -> list[dict[str, Any]]:
        """Coalesce adjacent same-role segments into provider-ready `{role, content: [...]}` messages."""
        messages: list[dict[str, Any]] = []
        for seg in self.segments:
            if messages and messages[-1]["role"] == seg.role:
                messages[-1]["content"].append(seg.part)
            else:
                messages.append({"role": seg.role, "content": [seg.part]})
        return messages

    async def with_compaction_lock(self, coro_fn: Any) -> Any:
        if self._compacting:
            raise RuntimeError("A context compaction is already in progress.")
        async with self._lock:
            self._compacting = True
            try:
                return await coro_fn()
            finally:
                self._compacting = False