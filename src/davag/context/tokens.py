"""Token estimation and live provider usage calibration."""

from __future__ import annotations

import math

VIDEO_TOKENS_PER_SECOND = 290  # Gemini ~258 video + ~32 audio tokens/sec


def estimate_text_tokens(text: str) -> int:
    if not text:
        return 0
    # ~3.8 chars/token heuristic calibrated after first provider response
    return max(1, math.ceil(len(text) / 3.8))


def estimate_image_tokens(size_bytes: int) -> int:
    if size_bytes <= 0:
        return 258
    return min(2400, max(258, round(size_bytes / 750)))


def estimate_video_tokens(size_bytes: int, duration_sec: float | None = None) -> int:
    if duration_sec is not None and duration_sec > 0:
        return max(300, round(duration_sec * VIDEO_TOKENS_PER_SECOND))
    # Fallback heuristic from file size (~1.5 Mbps average proxy bitrate -> ~187.5 KB/s)
    approx_sec = max(2.0, size_bytes / (187.5 * 1024.0))
    return round(approx_sec * VIDEO_TOKENS_PER_SECOND)


class TokenCalibrator:
    """Adjusts heuristic token estimates against authoritative provider `context_in` counts."""

    def __init__(self) -> None:
        self.ratio: float = 1.0

    def observe(self, estimated: int, reported: int) -> None:
        if estimated > 200 and reported > 0:
            raw = reported / estimated
            clamped = max(0.5, min(2.2, raw))
            self.ratio = 0.6 * self.ratio + 0.4 * clamped

    def scale(self, raw_estimate: int) -> int:
        return max(1, round(raw_estimate * self.ratio)) if raw_estimate > 0 else 0