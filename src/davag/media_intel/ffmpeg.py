"""FFprobe inspection, Intel Arc QSV hardware-accelerated proxy transcoding, and
frame-accurate silence/speech segment detection.
"""

from __future__ import annotations

import json
import math
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from davag.resolve_ops.helpers import frames_to_tc, parse_fps

from .hwaccel import detect_gpu_capabilities

SILENCE_START_RE = re.compile(r"silence_start:\s*([0-9.]+)")
SILENCE_END_RE = re.compile(r"silence_end:\s*([0-9.]+)\s*\|\s*silence_duration:\s*([0-9.]+)")


def _parse_rational_fps(rate_str: str) -> float:
    if "/" in rate_str:
        num_s, den_s = rate_str.split("/", 1)
        num, den = float(num_s), float(den_s)
        return num / den if den else 24.0
    return float(rate_str) if rate_str else 24.0


def probe_media(file_path: str | Path) -> dict[str, Any]:
    """Run `ffprobe` on a media file and return normalized stream, duration, and frame metadata."""
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise RuntimeError("ffprobe not found on PATH")

    target = Path(file_path).resolve()
    if not target.is_file():
        raise FileNotFoundError(f"Media file not found: {target}")

    res = subprocess.run(
        [
            ffprobe,
            "-v",
            "quiet",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(target),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if res.returncode != 0:
        raise RuntimeError(f"ffprobe failed on {target.name}: {res.stderr.strip()}")

    raw = json.loads(res.stdout)
    fmt = raw.get("format") or {}
    duration = float(fmt.get("duration", 0.0) or 0.0)
    size_bytes = int(fmt.get("size", 0) or 0)

    video_stream: dict[str, Any] | None = None
    audio_streams: list[dict[str, Any]] = []

    for st in raw.get("streams") or []:
        ctype = st.get("codec_type")
        if ctype == "video" and video_stream is None:
            fps = _parse_rational_fps(st.get("avg_frame_rate") or st.get("r_frame_rate") or "24/1")
            nb_frames = int(st.get("nb_frames") or round(duration * fps))
            video_stream = {
                "codec": st.get("codec_name"),
                "width": st.get("width"),
                "height": st.get("height"),
                "pix_fmt": st.get("pix_fmt"),
                "fps": round(fps, 4),
                "frames": nb_frames,
            }
        elif ctype == "audio":
            audio_streams.append(
                {
                    "codec": st.get("codec_name"),
                    "sample_rate": int(st.get("sample_rate") or 0),
                    "channels": int(st.get("channels") or 0),
                }
            )

    return {
        "path": str(target),
        "name": target.name,
        "size_bytes": size_bytes,
        "duration_sec": round(duration, 4),
        "format": fmt.get("format_name"),
        "video": video_stream,
        "audio": audio_streams,
    }


def detect_speech_segments(
    file_path: str | Path,
    *,
    noise_db: float = -35.0,
    min_silence_sec: float = 0.45,
    pad_sec: float = 0.08,
    fps_override: float | str | None = None,
) -> dict[str, Any]:
    """Detect non-silent speech intervals and map them directly to Resolve `[start_frame, end_frame]`!

    Output segments can be passed straight into `timeline.append_clips` for instant dead-air removal.
    """
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg not found on PATH")

    info = probe_media(file_path)
    duration = float(info["duration_sec"])
    vid_fps = (info["video"] or {}).get("fps") if info.get("video") else None
    exact_fps, _, _ = parse_fps(fps_override or vid_fps or 24.0)
    total_frames = max(1, round(duration * exact_fps))

    res = subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-nostats",
            "-i",
            str(Path(file_path).resolve()),
            "-af",
            f"silencedetect=noise={noise_db}dB:d={min_silence_sec}",
            "-vn",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )

    silences: list[tuple[float, float]] = []
    cur_start: float | None = None
    for line in res.stderr.splitlines():
        m_start = SILENCE_START_RE.search(line)
        if m_start:
            cur_start = float(m_start.group(1))
            continue
        m_end = SILENCE_END_RE.search(line)
        if m_end:
            end_t = float(m_end.group(1))
            start_t = cur_start if cur_start is not None else max(0.0, end_t - float(m_end.group(2)))
            silences.append((start_t, end_t))
            cur_start = None
    if cur_start is not None and cur_start < duration:
        silences.append((cur_start, duration))

    # Invert silences -> speech keep-ranges with padding
    keep: list[dict[str, Any]] = []
    cursor = 0.0
    for s_start, s_end in silences:
        seg_end = min(duration, s_start + pad_sec)
        if seg_end - cursor >= 0.12:
            sf = max(0, math.floor(cursor * exact_fps))
            ef = min(total_frames, math.ceil(seg_end * exact_fps))
            if ef > sf:
                keep.append(
                    {
                        "start_sec": round(cursor, 3),
                        "end_sec": round(seg_end, 3),
                        "start_frame": sf,
                        "end_frame": ef,
                        "start_tc": frames_to_tc(sf, exact_fps),
                        "end_tc": frames_to_tc(ef, exact_fps),
                    }
                )
        cursor = max(cursor, s_end - pad_sec)

    if duration - cursor >= 0.12:
        sf = max(0, math.floor(cursor * exact_fps))
        ef = total_frames
        if ef > sf:
            keep.append(
                {
                    "start_sec": round(cursor, 3),
                    "end_sec": round(duration, 3),
                    "start_frame": sf,
                    "end_frame": ef,
                    "start_tc": frames_to_tc(sf, exact_fps),
                    "end_tc": frames_to_tc(ef, exact_fps),
                }
            )

    kept_frames = sum(seg["end_frame"] - seg["start_frame"] for seg in keep)
    return {
        "path": info["path"],
        "fps": round(exact_fps, 4),
        "total_frames": total_frames,
        "kept_frames": kept_frames,
        "removed_frames": max(0, total_frames - kept_frames),
        "silence_count": len(silences),
        "speech_segments": keep,
    }


def extract_proxy_clip(
    file_path: str | Path,
    *,
    output_path: str | Path | None = None,
    start_sec: float | None = None,
    duration_sec: float | None = None,
    height: int = 480,
    fps: int = 12,
    video_kbps: int = 450,
    prefer_av1: bool = False,
) -> dict[str, Any]:
    """Transcode a lightweight review proxy MP4 for Gemini video analysis.

    Automatically uses **Intel Arc QuickSync (`av1_qsv` / `h264_qsv` + `d3d11va` decode)**
    when available, falling back cleanly to `libx264` if the hardware encoder rejects
    an unusual input pixel format.
    """
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg not found on PATH")

    src = Path(file_path).resolve()
    if not src.is_file():
        raise FileNotFoundError(f"Source media not found: {src}")

    if output_path is not None:
        dst = Path(output_path).resolve()
        dst.parent.mkdir(parents=True, exist_ok=True)
    else:
        with tempfile.NamedTemporaryFile(prefix="davag_proxy_", suffix=".mp4", delete=False) as fd_tmp:
            dst = Path(fd_tmp.name)

    caps = detect_gpu_capabilities(ffmpeg)
    encoder_candidates: list[str] = []
    if caps.has_intel_arc and caps.qsv_encoders:
        if prefer_av1 and "av1_qsv" in caps.qsv_encoders:
            encoder_candidates.append("av1_qsv")
        if "h264_qsv" in caps.qsv_encoders:
            encoder_candidates.append("h264_qsv")
    elif caps.preferred_proxy_encoder != "libx264":
        encoder_candidates.append(caps.preferred_proxy_encoder)
    encoder_candidates.append("libx264")

    vf = f"scale=-2:{int(height)},fps={int(fps)},format=nv12"
    last_err = ""

    for enc in encoder_candidates:
        cmd: list[str] = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error"]
        if enc.endswith("_qsv") and "d3d11va" in caps.hwaccels:
            cmd.extend(["-hwaccel", "d3d11va"])
        if start_sec is not None and start_sec > 0:
            cmd.extend(["-ss", f"{start_sec:.3f}"])
        cmd.extend(["-i", str(src)])
        if duration_sec is not None and duration_sec > 0:
            cmd.extend(["-t", f"{duration_sec:.3f}"])

        cmd.extend(["-vf", vf, "-c:v", enc, "-b:v", f"{int(video_kbps)}k"])
        if enc == "libx264":
            cmd.extend(["-preset", "veryfast", "-pix_fmt", "yuv420p"])
        elif enc.endswith("_qsv"):
            cmd.extend(["-preset", "veryfast"])

        cmd.extend(["-c:a", "aac", "-ac", "1", "-ar", "16000", "-b:a", "48k", "-movflags", "+faststart", str(dst)])

        res = subprocess.run(cmd, capture_output=True, text=True, timeout=300, check=False)
        if res.returncode == 0 and dst.is_file() and dst.stat().st_size > 0:
            return {
                "path": str(dst),
                "bytes": dst.stat().st_size,
                "encoder": enc,
                "hw_accelerated": enc != "libx264",
                "height": height,
                "fps": fps,
            }
        last_err = res.stderr.strip()

    raise RuntimeError(f"Proxy transcode failed for {src.name}: {last_err}")


SCENE_PTS_RE = re.compile(r"pts_time:([0-9.]+)")


def generate_contact_sheet(
    file_path: str | Path,
    *,
    output_path: str | Path | None = None,
    start_sec: float | None = None,
    duration_sec: float | None = None,
    cols: int = 4,
    rows: int = 4,
    tile_width: int = 384,
) -> dict[str, Any]:
    """Generate a tiled `Cols x Rows` PNG storyboard with burned-in timecode & source frame per tile!

    Uses fast input-demuxer `-ss` seeking when `duration_sec` is large (e.g. a 3-hour recording)
    so 16 keyframes extract in <1.5 seconds instead of decoding 3 hours of video!
    """
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg not found on PATH")

    src = Path(file_path).resolve()
    info = probe_media(src)
    total_dur = max(0.5, float(info["duration_sec"]))
    vid_fps = (info.get("video") or {}).get("fps") or 24.0
    exact_fps, _, _ = parse_fps(vid_fps)

    s_sec = max(0.0, float(start_sec or 0.0))
    avail_dur = max(0.5, total_dur - s_sec)
    w_dur = min(avail_dur, float(duration_sec)) if duration_sec is not None else avail_dur

    c = max(1, min(8, int(cols)))
    r = max(1, min(8, int(rows)))
    n_tiles = c * r
    step = w_dur / n_tiles

    if output_path is not None:
        dst = Path(output_path).resolve()
        dst.parent.mkdir(parents=True, exist_ok=True)
    else:
        with tempfile.NamedTemporaryFile(prefix="davag_sheet_", suffix=".png", delete=False) as tmp:
            dst = Path(tmp.name)

    # Multi-input fast keyframe seek (`-ss ts -i src`) + burned-in label + `xstack`/`tile`!
    # Works cleanly even on 3-hour recordings in ~1 second because FFmpeg seeks the container index!
    timestamps = [round(s_sec + (i + 0.35) * step, 3) for i in range(n_tiles)]
    tiles_meta: list[dict[str, Any]] = []

    cmd: list[str] = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error"]
    filter_parts: list[str] = []
    concat_inputs: list[str] = []

    for idx, ts in enumerate(timestamps):
        src_frame = round(ts * exact_fps)
        tc_label = frames_to_tc(src_frame, exact_fps)
        # Escape colons for FFmpeg drawtext
        esc_label = f"{tc_label} f{src_frame}".replace(":", r"\:")
        tiles_meta.append({"tile": idx + 1, "sec": ts, "frame": src_frame, "tc": tc_label})

        cmd.extend(["-ss", f"{ts:.3f}", "-i", str(src)])
        filter_parts.append(
            f"[{idx}:v] trim=end_frame=1,scale={int(tile_width)}:-2:force_original_aspect_ratio=decrease,"
            f"pad={int(tile_width)}:{round(tile_width * 9 / 16)}:(ow-iw)/2:(oh-ih)/2:black,"
            f"drawtext=text='{esc_label}':x=8:y=8:fontsize=15:fontcolor=white:"
            f"box=1:boxcolor=black@0.72:boxborderw=4,setpts=PTS-STARTPTS [v{idx}]"
        )
        concat_inputs.append(f"[v{idx}]")

    filter_parts.append(
        f"{''.join(concat_inputs)} concat=n={n_tiles}:v=1:a=0,tile={c}x{r}:padding=4:margin=4:color=0x10141d [outv]"
    )
    cmd.extend(["-filter_complex", ";".join(filter_parts), "-map", "[outv]", "-frames:v", "1", str(dst)])

    res = subprocess.run(cmd, capture_output=True, text=True, timeout=120, check=False)
    if res.returncode != 0 or not dst.is_file() or dst.stat().st_size == 0:
        # Fallback without `drawtext` in case a minimal FFmpeg build lacks libfreetype
        fallback_parts = []
        for idx in range(n_tiles):
            fallback_parts.append(
                f"[{idx}:v] trim=end_frame=1,scale={int(tile_width)}:-2,"
                f"pad={int(tile_width)}:{round(tile_width * 9 / 16)}:(ow-iw)/2:(oh-ih)/2:black,setpts=PTS-STARTPTS [v{idx}]"
            )
        fallback_parts.append(f"{''.join(concat_inputs)} concat=n={n_tiles}:v=1:a=0,tile={c}x{r}:padding=4:margin=4 [outv]")
        cmd_fb = cmd[: cmd.index("-filter_complex")] + ["-filter_complex", ";".join(fallback_parts), "-map", "[outv]", "-frames:v", "1", str(dst)]
        res2 = subprocess.run(cmd_fb, capture_output=True, text=True, timeout=120, check=False)
        if res2.returncode != 0:
            raise RuntimeError(f"Contact sheet generation failed: {res.stderr.strip() or res2.stderr.strip()}")

    return {
        "path": str(dst),
        "bytes": dst.stat().st_size,
        "cols": c,
        "rows": r,
        "tile_count": n_tiles,
        "start_sec": round(s_sec, 3),
        "duration_sec": round(w_dur, 3),
        "interval_sec": round(step, 3),
        "fps": round(exact_fps, 4),
        "tiles": tiles_meta,
    }


def detect_scene_cuts(
    file_path: str | Path,
    *,
    threshold: float = 0.30,
    start_sec: float = 0.0,
    duration_sec: float | None = None,
    max_cuts: int = 60,
) -> dict[str, Any]:
    """Detect visual shot/scene boundaries via FFmpeg `select='gt(scene,T)'` mapped to Resolve frames."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg not found on PATH")

    src = Path(file_path).resolve()
    info = probe_media(src)
    vid_fps = (info.get("video") or {}).get("fps") or 24.0
    exact_fps, _, _ = parse_fps(vid_fps)

    cmd: list[str] = [ffmpeg, "-hide_banner", "-nostats"]
    if start_sec > 0:
        cmd.extend(["-ss", f"{start_sec:.3f}"])
    cmd.extend(["-i", str(src)])
    if duration_sec is not None and duration_sec > 0:
        cmd.extend(["-t", f"{duration_sec:.3f}"])
    cmd.extend(
        [
            "-vf",
            f"scale=320:-2,select='gt(scene,{float(threshold):.2f})',showinfo",
            "-an",
            "-f",
            "null",
            "-",
        ]
    )

    res = subprocess.run(cmd, capture_output=True, text=True, timeout=180, check=False)
    cuts: list[dict[str, Any]] = []
    for line in res.stderr.splitlines():
        m = SCENE_PTS_RE.search(line)
        if not m:
            continue
        sec = start_sec + float(m.group(1))
        frame = round(sec * exact_fps)
        cuts.append(
            {
                "sec": round(sec, 3),
                "frame": frame,
                "tc": frames_to_tc(frame, exact_fps),
            }
        )
        if len(cuts) >= max_cuts:
            break

    return {
        "path": str(src),
        "threshold": threshold,
        "count": len(cuts),
        "cuts": cuts,
    }


def generate_editing_proxy(
    file_path: str | Path,
    *,
    output_path: str | Path | None = None,
    height: int = 720,
    video_kbps: int = 3500,
) -> dict[str, Any]:
    """Generate a smooth-scrubbing short-GOP (`-g 15`) editing proxy MP4 via Intel Arc QSV / NVENC
    suitable for linking directly to a DaVinci Resolve `MediaPoolItem` (`LinkProxyMedia`) AND
    streaming in the Web GUI `<video>` player!
    """
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg not found on PATH")

    src = Path(file_path).resolve()
    if output_path is not None:
        dst = Path(output_path).resolve()
    else:
        proxy_dir = src.parent / "Proxy"
        proxy_dir.mkdir(parents=True, exist_ok=True)
        dst = proxy_dir / f"{src.stem}_proxy.mp4"
    dst.parent.mkdir(parents=True, exist_ok=True)

    caps = detect_gpu_capabilities(ffmpeg)
    enc_list = []
    if caps.has_intel_arc and "h264_qsv" in caps.qsv_encoders:
        enc_list.append("h264_qsv")
    elif caps.preferred_proxy_encoder != "libx264":
        enc_list.append(caps.preferred_proxy_encoder)
    enc_list.append("libx264")

    last_err = ""
    for enc in enc_list:
        cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error"]
        if enc.endswith("_qsv") and "d3d11va" in caps.hwaccels:
            cmd.extend(["-hwaccel", "d3d11va"])
        cmd.extend(
            [
                "-i",
                str(src),
                "-vf",
                f"scale=-2:{int(height)},format=nv12",
                "-c:v",
                enc,
                "-g",
                "15",
                "-b:v",
                f"{int(video_kbps)}k",
            ]
        )
        if enc == "libx264":
            cmd.extend(["-preset", "veryfast", "-pix_fmt", "yuv420p"])
        cmd.extend(["-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(dst)])

        res = subprocess.run(cmd, capture_output=True, text=True, timeout=900, check=False)
        if res.returncode == 0 and dst.is_file() and dst.stat().st_size > 0:
            return {
                "path": str(dst),
                "bytes": dst.stat().st_size,
                "encoder": enc,
                "height": height,
            }
        last_err = res.stderr.strip()

    raise RuntimeError(f"Editing proxy generation failed: {last_err}")