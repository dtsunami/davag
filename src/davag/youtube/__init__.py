"""YouTube Delivery Automation: OpenRouter Flux multimodal thumbnail generator,
chapter/caption/manifest staging bundle, and gated YouTube Data API v3 private-draft uploader.
"""

from .qr import build_qr_callout_html, encode_qr_matrix, generate_qr_svg
from .studio import (
    extract_link_markers,
    format_youtube_chapters,
    generate_youtube_thumbnail,
    stage_youtube_package,
    update_youtube_video,
    upload_staged_to_youtube,
)

__all__ = [
    "build_qr_callout_html",
    "encode_qr_matrix",
    "extract_link_markers",
    "format_youtube_chapters",
    "generate_qr_svg",
    "generate_youtube_thumbnail",
    "stage_youtube_package",
    "update_youtube_video",
    "upload_staged_to_youtube",
]