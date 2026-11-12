"""External media intelligence pipeline (Intel Arc QSV / NVENC / VideoToolbox FFmpeg,
burned-in timecode contact sheets, scene cuts, Scout-model Clip Digests, transcription, and OTIO).
"""

from .digest import (
    analyze_clip_with_scout,
    load_clip_digest,
    pick_scout_provider_and_model,
    save_clip_digest,
)
from .ffmpeg import (
    detect_scene_cuts,
    detect_speech_segments,
    extract_proxy_clip,
    generate_contact_sheet,
    generate_editing_proxy,
    probe_media,
)
from .hwaccel import GPUCapabilities, detect_gpu_capabilities
from .otio_builder import CutSegment, build_timeline_otio

__all__ = [
    "CutSegment",
    "GPUCapabilities",
    "analyze_clip_with_scout",
    "build_timeline_otio",
    "detect_gpu_capabilities",
    "detect_scene_cuts",
    "detect_speech_segments",
    "extract_proxy_clip",
    "generate_contact_sheet",
    "generate_editing_proxy",
    "load_clip_digest",
    "pick_scout_provider_and_model",
    "probe_media",
    "save_clip_digest",
]