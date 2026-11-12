"""Pure-Python OpenTimelineIO (`.otio` JSON) & Final Cut Pro XML (`.fcpxml`) timeline builder.

Why pure Python instead of requiring `opentimelineio`'s C++ pybind11 wheel:
- `.otio` is a clean, documented JSON schema (`Timeline.1`, `Stack.1`, `Track.1`, `Clip.2`,
  `ExternalReference.1`, `TimeRange.1`, `RationalTime.1`) and `.fcpxml` is standard XML.
- DaVinci Resolve 18.5–21+ imports both `.otio` and `.fcpxml` natively via
  `MediaPool.ImportTimelineFromFile`.
- Zero C++ build issues across Python 3.11–3.14+ on Windows, macOS, and Linux!
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class CutSegment:
    name: str
    media_path: str
    start_frame: int
    duration_frames: int
    source_fps: float = 24.0
    track_index: int = 1  # 1-based


def _rational_time(value: float, rate: float) -> dict[str, Any]:
    return {
        "OTIO_SCHEMA": "RationalTime.1",
        "rate": float(rate),
        "value": float(value),
    }


def _time_range(start_frame: int, duration_frames: int, rate: float) -> dict[str, Any]:
    return {
        "OTIO_SCHEMA": "TimeRange.1",
        "duration": _rational_time(duration_frames, rate),
        "start_time": _rational_time(start_frame, rate),
    }


def _build_otio_dict(
    *,
    timeline_name: str,
    fps: float,
    video_segments: list[CutSegment],
    audio_segments: list[CutSegment],
) -> tuple[dict[str, Any], int, int]:
    tracks_json: list[dict[str, Any]] = []

    def _add_tracks(segs: list[CutSegment], kind: str) -> int:
        by_track: dict[int, list[CutSegment]] = {}
        for s in segs:
            by_track.setdefault(max(1, int(s.track_index)), []).append(s)

        for tidx in sorted(by_track.keys()):
            children: list[dict[str, Any]] = []
            for seg in by_track[tidx]:
                uri = Path(seg.media_path).resolve().as_uri()
                children.append(
                    {
                        "OTIO_SCHEMA": "Clip.2",
                        "name": seg.name,
                        "metadata": {},
                        "effects": [],
                        "markers": [],
                        "enabled": True,
                        "source_range": _time_range(
                            seg.start_frame, seg.duration_frames, seg.source_fps
                        ),
                        "active_media_reference_key": "DEFAULT_MEDIA",
                        "media_references": {
                            "DEFAULT_MEDIA": {
                                "OTIO_SCHEMA": "ExternalReference.1",
                                "name": seg.name,
                                "available_range": None,
                                "available_image_bounds": None,
                                "metadata": {},
                                "target_url": uri,
                            }
                        },
                    }
                )
            tracks_json.append(
                {
                    "OTIO_SCHEMA": "Track.1",
                    "name": f"{kind[0].upper()}{tidx}",
                    "kind": kind,
                    "metadata": {},
                    "effects": [],
                    "markers": [],
                    "enabled": True,
                    "source_range": None,
                    "children": children,
                }
            )
        return len(by_track)

    v_tracks = _add_tracks(video_segments, "Video")
    a_tracks = _add_tracks(audio_segments, "Audio")

    doc = {
        "OTIO_SCHEMA": "Timeline.1",
        "name": timeline_name,
        "global_start_time": _rational_time(round(3600 * fps), fps),
        "metadata": {"generator": "davag"},
        "tracks": {
            "OTIO_SCHEMA": "Stack.1",
            "name": "tracks",
            "metadata": {},
            "effects": [],
            "markers": [],
            "enabled": True,
            "source_range": None,
            "children": tracks_json,
        },
    }
    return doc, v_tracks, a_tracks


def _build_fcpxml_string(
    *,
    timeline_name: str,
    fps: float,
    video_segments: list[CutSegment],
) -> str:
    fps_int = max(1, round(fps))
    frame_dur = f"100/{fps_int * 100}s"

    root = ET.Element("fcpxml", {"version": "1.9"})
    resources = ET.SubElement(root, "resources")
    ET.SubElement(
        resources,
        "format",
        {"id": "r1", "name": f"FFVideoFormat1080p{fps_int}", "frameDuration": frame_dur, "width": "1920", "height": "1080"},
    )

    asset_ids: dict[str, str] = {}
    for seg in video_segments:
        uri = Path(seg.media_path).resolve().as_uri()
        if uri not in asset_ids:
            aid = f"r{len(asset_ids) + 2}"
            asset_ids[uri] = aid
            asset = ET.SubElement(
                resources,
                "asset",
                {
                    "id": aid,
                    "name": Path(seg.media_path).name,
                    "start": "0s",
                    "hasVideo": "1",
                    "hasAudio": "1",
                    "format": "r1",
                },
            )
            ET.SubElement(asset, "media-rep", {"kind": "original-media", "src": uri})

    library = ET.SubElement(root, "library")
    event = ET.SubElement(library, "event", {"name": "davag"})
    project = ET.SubElement(event, "project", {"name": timeline_name})
    total_frames = sum(s.duration_frames for s in video_segments)
    sequence = ET.SubElement(
        project,
        "sequence",
        {
            "format": "r1",
            "tcStart": "3600s",
            "tcFormat": "NDF",
            "duration": f"{total_frames * 100}/{fps_int * 100}s",
        },
    )
    spine = ET.SubElement(sequence, "spine")

    offset_frames = 3600 * fps_int
    for seg in video_segments:
        uri = Path(seg.media_path).resolve().as_uri()
        aid = asset_ids[uri]
        ET.SubElement(
            spine,
            "asset-clip",
            {
                "name": seg.name,
                "ref": aid,
                "offset": f"{offset_frames * 100}/{fps_int * 100}s",
                "start": f"{seg.start_frame * 100}/{fps_int * 100}s",
                "duration": f"{seg.duration_frames * 100}/{fps_int * 100}s",
                "format": "r1",
            },
        )
        offset_frames += seg.duration_frames

    ET.indent(root, space="  ")
    return '<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE fcpxml>\n' + ET.tostring(
        root, encoding="unicode"
    )


def build_timeline_otio(
    *,
    timeline_name: str,
    fps: float,
    video_segments: list[CutSegment],
    audio_segments: list[CutSegment] | None = None,
    output_path: str | Path,
) -> dict[str, Any]:
    """Write a Resolve-importable `.otio` (or `.fcpxml` if suffix is `.fcpxml`) timeline file."""
    dst = Path(output_path).resolve()
    dst.parent.mkdir(parents=True, exist_ok=True)
    a_segs = audio_segments or []

    if dst.suffix.lower() in (".fcpxml", ".xml"):
        xml_str = _build_fcpxml_string(
            timeline_name=timeline_name,
            fps=fps,
            video_segments=video_segments,
        )
        dst.write_text(xml_str, encoding="utf-8")
        v_tracks, a_tracks = (1 if video_segments else 0), 0
    else:
        doc, v_tracks, a_tracks = _build_otio_dict(
            timeline_name=timeline_name,
            fps=fps,
            video_segments=video_segments,
            audio_segments=a_segs,
        )
        dst.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")

    return {
        "path": str(dst),
        "format": dst.suffix.lower().lstrip(".") or "otio",
        "timeline_name": timeline_name,
        "fps": fps,
        "video_tracks": v_tracks,
        "audio_tracks": a_tracks,
        "clip_count": len(video_segments) + len(a_segs),
    }