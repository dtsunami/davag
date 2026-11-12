"""GPU & FFmpeg hardware acceleration discovery (Intel Arc QSV / OneVPL, NVENC, AMF, VideoToolbox).

On Intel Arc GPUs (e.g. Arc B580 / A770 / Pro with 16-32GB VRAM):
- Dual QuickSync media engines provide ultra-fast hardware AV1 (`av1_qsv`), H.264 (`h264_qsv`),
  and HEVC (`hevc_qsv`) encoding plus `d3d11va`/`qsv` hardware decoding.
- `av1_qsv` packs ~2x more duration into Gemini's 18MB inline video budget at 480p/720p while
  transcoding at hundreds of frames per second.
"""

from __future__ import annotations

import functools
import platform
import shutil
import subprocess
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class GPUCapabilities:
    gpu_names: tuple[str, ...]
    has_intel_arc: bool
    has_nvidia: bool
    has_amd: bool
    ffmpeg_available: bool
    qsv_encoders: tuple[str, ...]      # e.g. ('av1_qsv', 'h264_qsv', 'hevc_qsv')
    nvenc_encoders: tuple[str, ...]    # e.g. ('av1_nvenc', 'h264_nvenc', 'hevc_nvenc')
    amf_encoders: tuple[str, ...]
    vt_encoders: tuple[str, ...]
    hwaccels: tuple[str, ...]          # e.g. ('qsv', 'd3d11va', 'dxva2', 'vulkan')
    preferred_proxy_encoder: str       # Best encoder for fast Gemini proxy transcodes

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _detect_windows_gpus() -> list[str]:
    try:
        res = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "Get-CimInstance Win32_VideoController | Select-Object -ExpandProperty Name",
            ],
            capture_output=True,
            text=True,
            timeout=4,
            check=False,
        )
        if res.returncode == 0:
            return [ln.strip() for ln in res.stdout.splitlines() if ln.strip()]
    except (OSError, subprocess.SubprocessError):
        pass
    return []


def _query_ffmpeg_list(ffmpeg_bin: str, flag: str) -> set[str]:
    try:
        res = subprocess.run(
            [ffmpeg_bin, "-hide_banner", flag],
            capture_output=True,
            text=True,
            timeout=4,
            check=False,
        )
        tokens: set[str] = set()
        for line in res.stdout.splitlines():
            parts = line.strip().split()
            if len(parts) >= 2 and parts[0].startswith("V"):
                tokens.add(parts[1])
            elif len(parts) == 1 and flag == "-hwaccels" and not parts[0].endswith(":"):
                tokens.add(parts[0])
        return tokens
    except (OSError, subprocess.SubprocessError):
        return set()


@functools.lru_cache(maxsize=1)
def detect_gpu_capabilities(ffmpeg_path: str | None = None) -> GPUCapabilities:
    ff = ffmpeg_path or shutil.which("ffmpeg")
    gpus: list[str] = []
    if platform.system() == "Windows":
        gpus = _detect_windows_gpus()

    lower_gpus = " | ".join(gpus).lower()
    has_intel_arc = "arc" in lower_gpus or "intel" in lower_gpus
    has_nvidia = "nvidia" in lower_gpus or "geforce" in lower_gpus or "rtx" in lower_gpus
    has_amd = "radeon" in lower_gpus or "amd" in lower_gpus

    if not ff:
        return GPUCapabilities(
            gpu_names=tuple(gpus),
            has_intel_arc=has_intel_arc,
            has_nvidia=has_nvidia,
            has_amd=has_amd,
            ffmpeg_available=False,
            qsv_encoders=(),
            nvenc_encoders=(),
            amf_encoders=(),
            vt_encoders=(),
            hwaccels=(),
            preferred_proxy_encoder="libx264",
        )

    encoders = _query_ffmpeg_list(ff, "-encoders")
    hwaccels = sorted(_query_ffmpeg_list(ff, "-hwaccels"))

    qsv = tuple(e for e in ("av1_qsv", "h264_qsv", "hevc_qsv") if e in encoders)
    nvenc = tuple(e for e in ("av1_nvenc", "h264_nvenc", "hevc_nvenc") if e in encoders)
    amf = tuple(e for e in ("av1_amf", "h264_amf", "hevc_amf") if e in encoders)
    vt = tuple(e for e in ("h264_videotoolbox", "hevc_videotoolbox") if e in encoders)

    # Choose best hardware encoder based on detected GPU hardware first, falling back to libx264
    preferred = "libx264"
    if has_intel_arc and qsv:
        # Prefer h264_qsv for universal compatibility & Gemini ingestion speed, or av1_qsv when requested
        preferred = "h264_qsv" if "h264_qsv" in qsv else qsv[0]
    elif has_nvidia and nvenc:
        preferred = "h264_nvenc" if "h264_nvenc" in nvenc else nvenc[0]
    elif has_amd and amf:
        preferred = "h264_amf" if "h264_amf" in amf else amf[0]
    elif vt:
        preferred = vt[0]

    return GPUCapabilities(
        gpu_names=tuple(gpus),
        has_intel_arc=has_intel_arc,
        has_nvidia=has_nvidia,
        has_amd=has_amd,
        ffmpeg_available=True,
        qsv_encoders=qsv,
        nvenc_encoders=nvenc,
        amf_encoders=amf,
        vt_encoders=vt,
        hwaccels=tuple(hwaccels),
        preferred_proxy_encoder=preferred,
    )