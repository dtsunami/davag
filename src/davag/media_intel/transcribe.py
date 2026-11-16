"""Frame-mapped audio transcription with SHA-256 disk caching (`~/.davag/cache/transcripts/`).

Backend priority:
1. Local `faster-whisper` (if installed via `pip install -e ".[whisper]"`).
2. **Google Gemini Audio (`gemini-3.8-flash` via Files API)** — handles multi-hour recordings
   (>25 MB, up to 2 GB!) natively with timestamped JSON output!
3. OpenAI Whisper (`whisper-1`) when audio MP3 <= 24 MB and a valid OpenAI `sk-` key is configured.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import math
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from davag.resolve_ops.helpers import frames_to_tc, parse_fps

from .ffmpeg import probe_media

MMSS_RE = re.compile(r"^(?:(?P<h>\d+):)?(?P<m>\d{1,2}):(?P<s>\d{2}(?:\.\d+)?)$")


def _file_cache_key(
    path: Path,
    start_sec: float | None,
    duration_sec: float | None,
    include_fillers: bool = False,
) -> str:
    st = path.stat()
    h = hashlib.sha256()
    win = f"|{start_sec or 0:.1f}|{duration_sec or -1:.1f}|f={int(include_fillers)}"
    h.update(f"{path.resolve()}|{st.st_size}|{st.st_mtime_ns}{win}".encode())
    return h.hexdigest()[:24]


def extract_audio_mp3(
    src: Path,
    *,
    start_sec: float | None = None,
    duration_sec: float | None = None,
    bitrate_kbps: int = 32,
) -> Path:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg is required for audio extraction")
    with tempfile.NamedTemporaryFile(prefix="davag_audio_", suffix=".mp3", delete=False) as tmp:
        out = Path(tmp.name)

    cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error"]
    if start_sec is not None and start_sec > 0:
        cmd.extend(["-ss", f"{start_sec:.3f}"])
    cmd.extend(["-i", str(src)])
    if duration_sec is not None and duration_sec > 0:
        cmd.extend(["-t", f"{duration_sec:.3f}"])
    cmd.extend(["-vn", "-ac", "1", "-ar", "16000", "-b:a", f"{int(bitrate_kbps)}k", str(out)])

    res = subprocess.run(cmd, capture_output=True, text=True, timeout=600, check=False)
    if res.returncode != 0:
        out.unlink(missing_ok=True)
        raise RuntimeError(f"Audio extraction failed: {res.stderr.strip()}")
    return out


def _parse_sec(val: Any) -> float:
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).strip()
    m = MMSS_RE.match(s)
    if m:
        hh = int(m.group("h") or 0)
        mm = int(m.group("m"))
        ss = float(m.group("s"))
        return hh * 3600.0 + mm * 60.0 + ss
    return float(s)


def _annotate_frames(
    segments: list[dict[str, Any]], exact_fps: float, offset_sec: float = 0.0
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for seg in segments:
        s_sec = offset_sec + _parse_sec(seg["start_sec"])
        e_sec = offset_sec + _parse_sec(seg["end_sec"])
        sf = max(0, math.floor(s_sec * exact_fps))
        ef = max(sf + 1, math.ceil(e_sec * exact_fps))
        words_out = []
        for w in seg.get("words") or []:
            ws = offset_sec + _parse_sec(w["start_sec"])
            we = offset_sec + _parse_sec(w["end_sec"])
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


async def _transcribe_via_gemini(
    audio_mp3: Path,
    *,
    api_key: str,
    language: str | None = None,
    include_fillers: bool = False,
) -> list[dict[str, Any]]:
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=api_key)
    uploaded = await client.aio.files.upload(
        file=str(audio_mp3),
        config=types.UploadFileConfig(mime_type="audio/mpeg"),
    )
    fname = uploaded.name
    assert fname is not None
    try:
        for _ in range(90):
            st = str(getattr(uploaded.state, "name", uploaded.state) or "").upper()
            if "ACTIVE" in st:
                break
            await asyncio.sleep(1.0)
            uploaded = await client.aio.files.get(name=fname)

        lang_hint = f" Language: {language}." if language else ""
        filler_note = (
            " STRICT VERBATIM MODE: include EVERY vocal disfluency/filler (`uh`, `um`, `umm`, `uhh`, `er`, `ah`) "
            "as its own tight timestamped entry or inside `words` with exact `MM:SS.dd` boundaries!"
            if include_fillers
            else ""
        )
        seg_schema = types.Schema(
            type=types.Type.ARRAY,
            items=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "start": types.Schema(type=types.Type.STRING),
                    "end": types.Schema(type=types.Type.STRING),
                    "text": types.Schema(type=types.Type.STRING),
                },
                required=["start", "end", "text"],
            ),
        )
        prompt = (
            "Transcribe all spoken speech in this audio accurately into chronological segments "
            f"({'1-6 seconds per segment' if include_fillers else '5-20 seconds per segment'}).{lang_hint}{filler_note} "
            "Use exact `MM:SS.dd` timestamps (e.g. `\"00:04.20\"`, `\"05:31.85\"`) "
            "relative to `00:00.00` — NEVER write minutes as integer part of seconds! "
            'Return a JSON array of objects: [{"start": "00:00.00", "end": "00:08.40", "text": "..."}]. '
            "Omit silent stretches."
        )
        resp = await client.aio.models.generate_content(
            model="gemini-3.8-flash",
            contents=[
                types.Part.from_uri(file_uri=str(uploaded.uri), mime_type="audio/mpeg"),
                types.Part.from_text(text=prompt),
            ],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=seg_schema,
                max_output_tokens=65_536,
                temperature=0.0,
            ),
        )
        raw_txt = (resp.text or "[]").strip()
        try:
            parsed = json.loads(raw_txt)
        except ValueError:
            # Salvage complete JSON objects if output ever truncates near max_output_tokens
            objs = [json.loads(m.group(0)) for m in re.finditer(r"\{[^{}]+\}", raw_txt)]
            parsed = objs
        if isinstance(parsed, dict) and isinstance(parsed.get("segments"), list):
            parsed = parsed["segments"]
        if not isinstance(parsed, list):
            return []

        def _parse_ts(val: Any) -> float:
            if isinstance(val, (int, float)):
                return float(val)
            s = str(val or "0").strip()
            parts = s.split(":")
            if len(parts) == 3:
                return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
            if len(parts) == 2:
                return int(parts[0]) * 60 + float(parts[1])
            return float(s)

        return [
            {
                "text": str(item.get("text", "")),
                "start_sec": _parse_ts(item.get("start", item.get("start_sec", 0.0))),
                "end_sec": _parse_ts(item.get("end", item.get("end_sec", 0.0))),
            }
            for item in parsed
            if isinstance(item, dict) and str(item.get("text", "")).strip()
        ]
    finally:
        with contextlib.suppress(Exception):
            await client.aio.files.delete(name=fname)


def load_cached_transcript(cache_dir: str | Path, file_path: str | Path) -> dict[str, Any] | None:
    """Return the cached full-clip transcript (or most recent window transcript) for `file_path`."""
    cdir = Path(cache_dir)
    if not cdir.is_dir():
        return None
    src = Path(file_path).resolve()
    full_file = cdir / f"{_file_cache_key(src, None, None)}.json"
    if full_file.is_file():
        with contextlib.suppress(OSError, ValueError):
            return json.loads(full_file.read_text(encoding="utf-8"))
    # Otherwise check if any window transcript matches `src`
    best: tuple[float, dict[str, Any]] | None = None
    for jf in cdir.glob("*.json"):
        with contextlib.suppress(OSError, ValueError):
            data = json.loads(jf.read_text(encoding="utf-8"))
            if Path(str(data.get("path", ""))).resolve() == src:
                mt = jf.stat().st_mtime
                if best is None or mt > best[0]:
                    best = (mt, data)
    return best[1] if best else None


async def transcribe_media(
    file_path: str | Path,
    *,
    cache_dir: str | Path,
    gemini_api_key: str | None = None,
    openai_api_key: str | None = None,
    language: str | None = None,
    start_sec: float | None = None,
    duration_sec: float | None = None,
    include_fillers: bool = False,
) -> dict[str, Any]:
    """Transcribe a media clip (or `[start_sec, duration_sec]` slice) mapped to Resolve source frames."""
    src = Path(file_path).resolve()
    info = probe_media(src)
    vid_fps = (info.get("video") or {}).get("fps") or 24.0
    exact_fps, _, _ = parse_fps(vid_fps)
    offset = max(0.0, float(start_sec or 0.0))

    cdir = Path(cache_dir)
    cdir.mkdir(parents=True, exist_ok=True)
    cache_file = cdir / f"{_file_cache_key(src, start_sec, duration_sec, include_fillers)}.json"
    if cache_file.is_file():
        try:
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
            cached["cached"] = True
            return cached
        except (OSError, ValueError):
            pass

    raw_segments: list[dict[str, Any]] = []
    backend_used = ""
    loop = asyncio.get_running_loop()

    # 1. Local faster-whisper if installed
    if start_sec is None and duration_sec is None:
        try:
            from faster_whisper import WhisperModel  # type: ignore[import-not-found]

            def _run_fw() -> list[dict[str, Any]]:
                model = WhisperModel("base", device="cpu", compute_type="int8")
                segs_iter, _ = model.transcribe(str(src), word_timestamps=True, language=language)
                return [
                    {
                        "text": s.text,
                        "start_sec": s.start,
                        "end_sec": s.end,
                        "words": [
                            {"word": w.word, "start_sec": w.start, "end_sec": w.end}
                            for w in (s.words or [])
                        ],
                    }
                    for s in segs_iter
                ]

            raw_segments = await loop.run_in_executor(None, _run_fw)
            backend_used = "faster-whisper"
        except ImportError:
            pass

    audio_mp3: Path | None = None
    try:
        if not backend_used:
            audio_mp3 = await loop.run_in_executor(
                None,
                lambda: extract_audio_mp3(src, start_sec=start_sec, duration_sec=duration_sec),
            )

        # 2. Gemini Audio (Files API — handles multi-hour audio seamlessly!)
        if not backend_used and gemini_api_key and audio_mp3 is not None:
            raw_segments = await _transcribe_via_gemini(
                audio_mp3,
                api_key=gemini_api_key,
                language=language,
                include_fillers=include_fillers,
            )
            backend_used = "gemini:gemini-3.8-flash"

        # 3. OpenAI Whisper fallback (only if key starts with `sk-` and MP3 <= 24 MB)
        if (
            not backend_used
            and openai_api_key
            and openai_api_key.startswith("sk-")
            and audio_mp3 is not None
            and audio_mp3.stat().st_size <= 24 * 1024 * 1024
        ):
            from openai import AsyncOpenAI

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
                    {"text": getattr(s, "text", ""), "start_sec": s_start, "end_sec": s_end, "words": seg_words}
                )
            backend_used = "openai:whisper-1"

        if not backend_used:
            raise RuntimeError(
                "No transcription backend succeeded (configure GEMINI_API_KEY, a valid sk-* OPENAI_API_KEY "
                "for files <=24MB, or install `pip install -e .[whisper]`)."
            )
    finally:
        if audio_mp3 is not None:
            audio_mp3.unlink(missing_ok=True)

    annotated = _annotate_frames(raw_segments, exact_fps, offset_sec=offset)
    payload = {
        "path": str(src),
        "name": src.name,
        "backend": backend_used,
        "cached": False,
        "fps": round(exact_fps, 4),
        "start_sec": offset,
        "segment_count": len(annotated),
        "full_text": " ".join(s["text"] for s in annotated).strip()[:20_000],
        "segments": annotated,
    }
    cache_file.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return payload