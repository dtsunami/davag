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
# Matches @"C:\path with spaces\file.mp4", @'path', or @unquoted/path.png
AT_REF_RE = re.compile(r'@"([^"]+)"|@\'([^\']+)\'|@([^\s"\']+)')
MAX_RAW_IMAGE_BYTES = 3_500_000  # Downscale/compress >3.5 MB stills (e.g. 25 MB 16-bit 4K Resolve grabs!)


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


def _compress_oversized_image(src: Path) -> tuple[bytes, str] | None:
    """Compress >3.5 MB images (such as 25 MB 16-bit 4K Resolve stills) to crisp <=1920px JPEG."""
    import shutil
    import subprocess
    import tempfile

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return None
    with tempfile.NamedTemporaryFile(prefix="davag_img_opt_", suffix=".jpg", delete=False) as tmp:
        out_jpg = Path(tmp.name)
    try:
        res = subprocess.run(
            [
                ffmpeg,
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(src),
                "-vf",
                "scale='min(1920,iw)':-2",
                "-frames:v",
                "1",
                "-q:v",
                "3",
                str(out_jpg),
            ],
            capture_output=True,
            timeout=30,
            check=False,
        )
        if res.returncode == 0 and out_jpg.is_file() and out_jpg.stat().st_size > 64:
            return out_jpg.read_bytes(), "image/jpeg"
        return None
    finally:
        out_jpg.unlink(missing_ok=True)


def load_image_part(path_or_abs: str | Path, sandbox: Sandbox | None = None) -> dict[str, Any]:
    p = sandbox.resolve_for_read(path_or_abs) if sandbox is not None else Path(path_or_abs).resolve()
    ext = p.suffix.lower()
    mime = IMAGE_MEDIA.get(ext)
    if not mime:
        raise ValueError(f"{p.name}: unsupported image format ({', '.join(IMAGE_MEDIA)})")
    size = p.stat().st_size
    if size == 0:
        raise ValueError(f"{p.name}: image file is empty (0 bytes)")

    if size > MAX_RAW_IMAGE_BYTES:
        compressed = _compress_oversized_image(p)
        if compressed is not None:
            raw, mime = compressed
        else:
            raw = p.read_bytes()
    else:
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


def _match_ref(m: re.Match[str]) -> str:
    return (m.group(1) or m.group(2) or m.group(3) or "").strip()


def extract_image_refs(text: str, sandbox: Sandbox) -> tuple[str, list[str]]:
    images: list[str] = []

    def _sub(m: re.Match[str]) -> str:
        ref = _match_ref(m)
        if not is_image_path(ref):
            return m.group(0)
        with contextlib.suppress(Exception):
            cand = (Path(ref) if Path(ref).is_absolute() else (sandbox.cwd / ref)).resolve()
            if cand.is_file():
                sandbox.allow_read_file(cand)
                images.append(str(cand))
                return f"[attached image: {cand}]"
        return m.group(0)

    return AT_REF_RE.sub(_sub, text), images


def extract_video_refs(text: str, sandbox: Sandbox) -> tuple[str, list[str], list[str]]:
    videos: list[str] = []
    youtube: list[str] = []

    def _sub(m: re.Match[str]) -> str:
        ref = _match_ref(m)
        if is_youtube_url(ref):
            youtube.append(ref)
            return f"[video: {ref}]"
        if not is_video_path(ref):
            return m.group(0)
        with contextlib.suppress(Exception):
            cand = (Path(ref) if Path(ref).is_absolute() else (sandbox.cwd / ref)).resolve()
            if cand.is_file():
                sandbox.allow_read_file(cand)
                videos.append(str(cand))
                return f"[video: {cand}]"
        return m.group(0)

    return AT_REF_RE.sub(_sub, text), videos, youtube