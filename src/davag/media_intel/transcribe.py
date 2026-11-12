"""Frame-mapped audio transcription with SHA-256 disk caching (`~/.davag/cache/transcripts/`).

Supports:
1. Local `faster-whisper` (if installed via `pip install -e ".[whisper]"`) or OpenVINO Whisper.
2. Cloud OpenAI Whisper (`whisper-1` / `gpt-4o-transcribe`) with word timestamps when an OpenAI key is configured.
Maps every segment & word directly to DaVinci Resolve `[start_frame, end_frame]` at the source clip's FPS!
"""

from __future__ import annotations

import hashlib
import json
import math
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from davag.resolve_ops.helpers import frames_to_tc, parse_fps

from .ffmpeg import probe_media


def _file_cache_key(path: Path) -> str:
    st = path.stat()
    h = hashlib.sha256()
    h.update(f"{path.resolve()}|{st.st_size}|{st.st_mtime_ns}".encode())
    return h.hexdigest()[:24]


def extract_audio_wav(src: Path) -> Path:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg is required for audio extraction")
    with tempfile.NamedTemporaryFile(prefix="davag_audio_", suffix=".mp3", delete=False) as tmp:
        out = Path(tmp.name)
    res = subprocess.run(
        [
            ffmpeg,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(src),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-b:a",
            "48k",
            str(out),
        ],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    if res.returncode != 0:
        out.unlink(missing_ok=True)
        raise RuntimeError(f"Audio extraction failed: {res.stderr.strip()}")
    return out


def _annotate_frames(segments: list[dict[str, Any]], exact_fps: float) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for seg in segments:
        s_sec = float(seg["start_sec"])
        e_sec = float(seg["end_sec"])
        sf = max(0, math.floor(s_sec * exact_fps))
        ef = max(sf + 1, math.ceil(e_sec * exact_fps))
        words_out = []
        for w in seg.get("words") or []:
            ws = float(w["start_sec"])
            we = float(w["end_sec"])
            words_out.append(
                {
                    "word": w["word"],
                    "start_sec": round(ws, 3),
                    "end_sec": round(we, 3),
                    "start_frame": max(0, math.floor(ws * exact_fps)),
                    "end_frame": max(1, math.ceil(we * exact_fps)),
                }
            )
        out.append(
            {
                "text": str(seg["text"]).strip(),
                "start_sec": round(s_sec, 3),
                "end_sec": round(e_sec, 3),
                "start_frame": sf,
                "end_frame": ef,
                "start_tc": frames_to_tc(sf, exact_fps),
                "end_tc": frames_to_tc(ef, exact_fps),
                **({"words": words_out} if words_out else {}),
            }
        )
    return out


async def transcribe_media(
    file_path: str | Path,
    *,
    cache_dir: str | Path,
    openai_api_key: str | None = None,
    language: str | None = None,
) -> dict[str, Any]:
    """Transcribe a media clip and map timestamps to exact source clip frames."""
    src = Path(file_path).resolve()
    info = probe_media(src)
    vid_fps = (info.get("video") or {}).get("fps") or 24.0
    exact_fps, _, _ = parse_fps(vid_fps)

    cdir = Path(cache_dir)
    cdir.mkdir(parents=True, exist_ok=True)
    cache_file = cdir / f"{_file_cache_key(src)}.json"
    if cache_file.is_file():
        try:
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
            cached["cached"] = True
            return cached
        except (OSError, ValueError):
            pass

    raw_segments: list[dict[str, Any]] = []
    backend_used = ""

    # 1. Prefer local faster-whisper if installed
    try:
        from faster_whisper import WhisperModel  # type: ignore[import-not-found]

        model = WhisperModel("base", device="cpu", compute_type="int8")
        segs_iter, _ = model.transcribe(str(src), word_timestamps=True, language=language)
        for s in segs_iter:
            raw_segments.append(
                {
                    "text": s.text,
                    "start_sec": s.start,
                    "end_sec": s.end,
                    "words": [
                        {"word": w.word, "start_sec": w.start, "end_sec": w.end}
                        for w in (s.words or [])
                    ],
                }
            )
        backend_used = "faster-whisper"
    except ImportError:
        pass

    # 2. Fall back to OpenAI Whisper API with word + segment timestamps
    if not backend_used:
        if not openai_api_key:
            raise RuntimeError(
                "No transcription backend available: either install local `faster-whisper` "
                "(`pip install -e .[whisper]`) or configure OPENAI_API_KEY."
            )
        from openai import AsyncOpenAI

        audio_mp3 = extract_audio_wav(src)
        try:
            client = AsyncOpenAI(api_key=openai_api_key)
            audio_bytes = audio_mp3.read_bytes()
            resp = await client.audio.transcriptions.create(
                model="whisper-1",
                file=(audio_mp3.name, audio_bytes, "audio/mpeg"),
                response_format="verbose_json",
                timestamp_granularities=["word", "segment"],
                **({"language": language} if language else {}),
            )
            words_all = getattr(resp, "words", None) or []
            for s in getattr(resp, "segments", None) or []:
                s_start = float(getattr(s, "start", 0.0))
                s_end = float(getattr(s, "end", 0.0))
                seg_words = [
                    {
                        "word": getattr(w, "word", ""),
                        "start_sec": float(getattr(w, "start", s_start)),
                        "end_sec": float(getattr(w, "end", s_end)),
                    }
                    for w in words_all
                    if s_start - 0.05 <= float(getattr(w, "start", 0.0)) <= s_end + 0.05
                ]
                raw_segments.append(
                    {
                        "text": getattr(s, "text", ""),
                        "start_sec": s_start,
                        "end_sec": s_end,
                        "words": seg_words,
                    }
                )
            backend_used = "openai:whisper-1"
        finally:
            audio_mp3.unlink(missing_ok=True)

    annotated = _annotate_frames(raw_segments, exact_fps)
    payload = {
        "path": str(src),
        "name": src.name,
        "backend": backend_used,
        "cached": False,
        "fps": round(exact_fps, 4),
        "full_text": " ".join(s["text"] for s in annotated).strip(),
        "segments": annotated,
    }
    cache_file.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return payload