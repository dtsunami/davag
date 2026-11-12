"""Image, local Video (inline <=18MB vs Gemini Files API >18MB), and YouTube `@ref` loaders."""

from __future__ import annotations

import base64
import contextlib
import re
from pathlib import Path
from typing import Any

from davag.agent.sandbox import Sandbox
from davag.media_intel.ffmpeg import probe_media

from .tokens import VIDEO_TOKENS_PER_SECOND, estimate_image_tokens, estimate_video_tokens

IMAGE_MEDIA: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}

VIDEO_MEDIA: dict[str, str] = {
    ".mp4": "video/mp4",
    ".mov": "video/mov",
    ".mpeg": "video/mpeg",
    ".mpg": "video/mpg",
    ".avi": "video/avi",
    ".webm": "video/webm",
    ".wmv": "video/wmv",
    ".mkv": "video/mp4",
}

INLINE_VIDEO_MAX_BYTES = 18 * 1024 * 1024  # 18 MB inline threshold; larger uses Gemini Files API!
FILES_API_MAX_BYTES = 2 * 1024 * 1024 * 1024  # 2 GB Gemini Files API cap
DEFAULT_YOUTUBE_MINUTES = 10.0

YOUTUBE_RE = re.compile(
    r"^https?://(?:www\.|m\.)?(?:youtube\.com/(?:watch\?(?:[^&\s]*&)*v=|embed/|shorts/|live/)|youtu\.be/)([\w-]{11})"
)
AT_REF_RE = re.compile(r"@([^\s\"']+)")


def is_youtube_url(s: str) -> bool:
    return bool(YOUTUBE_RE.match(str(s or "").strip()))


def is_image_path(p: str | Path) -> bool:
    return Path(p).suffix.lower() in IMAGE_MEDIA


def is_video_path(p: str | Path) -> bool:
    return Path(p).suffix.lower() in VIDEO_MEDIA


def youtube_ref(url: str, *, minutes: float | None = None) -> dict[str, Any]:
    if not is_youtube_url(url):
        raise ValueError(f"{url}: not a valid YouTube URL")
    assumed = minutes is None or minutes <= 0
    mins = DEFAULT_YOUTUBE_MINUTES if assumed else float(minutes)
    return {
        "uri": url,
        "media_type": None,
        "tokens": round(mins * 60.0 * VIDEO_TOKENS_PER_SECOND),
        "minutes": mins,
        "assumed": assumed,
        "name": url,
    }


def load_image_part(path_or_abs: str | Path, sandbox: Sandbox | None = None) -> dict[str, Any]:
    p = sandbox.resolve_for_read(path_or_abs) if sandbox is not None else Path(path_or_abs).resolve()
    ext = p.suffix.lower()
    mime = IMAGE_MEDIA.get(ext)
    if not mime:
        raise ValueError(f"{p.name}: unsupported image format ({', '.join(IMAGE_MEDIA)})")
    raw = p.read_bytes()
    return {
        "media_type": mime,
        "data": base64.b64encode(raw).decode("ascii"),
        "bytes": len(raw),
        "tokens": estimate_image_tokens(len(raw)),
        "name": sandbox.rel(p) if sandbox else p.name,
        "path": str(p),
    }


def load_video_part(path_or_abs: str | Path, sandbox: Sandbox | None = None) -> dict[str, Any]:
    """Load a local video for Gemini:
    - <= 18 MB: inline base64 `data`
    - > 18 MB (up to 2 GB): `file_path` reference uploaded lazily via Gemini Files API!
    """
    p = sandbox.resolve_for_read(path_or_abs) if sandbox is not None else Path(path_or_abs).resolve()
    ext = p.suffix.lower()
    mime = VIDEO_MEDIA.get(ext)
    if not mime:
        raise ValueError(f"{p.name}: unsupported video format ({', '.join(VIDEO_MEDIA)})")
    size = p.stat().st_size
    if size > FILES_API_MAX_BYTES:
        raise ValueError(f"{p.name} is {size / (1024**3):.2f} GB (exceeds 2 GB Files API limit)")

    duration_sec: float | None = None
    try:
        info = probe_media(p)
        duration_sec = float(info.get("duration_sec") or 0.0) or None
    except Exception:  # noqa: BLE001
        duration_sec = None

    tokens = estimate_video_tokens(size, duration_sec)
    name = sandbox.rel(p) if sandbox else p.name

    if size <= INLINE_VIDEO_MAX_BYTES:
        raw = p.read_bytes()
        return {
            "media_type": mime,
            "data": base64.b64encode(raw).decode("ascii"),
            "bytes": size,
            "duration_sec": duration_sec,
            "tokens": tokens,
            "delivery": "inline",
            "name": name,
        }

    return {
        "media_type": mime,
        "file_path": str(p),
        "bytes": size,
        "duration_sec": duration_sec,
        "tokens": tokens,
        "delivery": "files_api",
        "name": name,
    }


def extract_image_refs(text: str, sandbox: Sandbox) -> tuple[str, list[str]]:
    images: list[str] = []

    def _sub(m: re.Match[str]) -> str:
        ref = m.group(1)
        if not is_image_path(ref):
            return m.group(0)
        with contextlib.suppress(Exception):
            p = sandbox.resolve_for_read(ref)
            if p.is_file():
                images.append(ref)
                return f"[image: {ref}]"
        return m.group(0)

    return AT_REF_RE.sub(_sub, text), images


def extract_video_refs(text: str, sandbox: Sandbox) -> tuple[str, list[str], list[str]]:
    videos: list[str] = []
    youtube: list[str] = []

    def _sub(m: re.Match[str]) -> str:
        ref = m.group(1)
        if is_youtube_url(ref):
            youtube.append(ref)
            return f"[video: {ref}]"
        if not is_video_path(ref):
            return m.group(0)
        with contextlib.suppress(Exception):
            p = sandbox.resolve_for_read(ref)
            if p.is_file():
                videos.append(ref)
                return f"[video: {ref}]"
        return m.group(0)

    return AT_REF_RE.sub(_sub, text), videos, youtube