"""YouTube Thumbnail Generation (OpenRouter Flux 3 / Multimodal Image+Prompt) & Staging Pipeline."""

from __future__ import annotations

import base64
import json
import mimetypes
import re
from pathlib import Path
from typing import Any

import aiohttp

from davag.config.env import Config
from davag.transport.base import ResolveTransport

DATA_URI_RE = re.compile(r"^data:image/[a-zA-Z0-9.+-]+;base64,(?P<b64>[A-Za-z0-9+/=\s]+)$")


def _slugify(text: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", text.strip().lower()).strip("-")
    return s[:48] or "youtube-release"


def _sec_to_yt_ts(total_sec: float) -> str:
    sec = max(0, round(total_sec))
    hh = sec // 3600
    mm = (sec % 3600) // 60
    ss = sec % 60
    return f"{hh}:{mm:02d}:{ss:02d}" if hh > 0 else f"{mm:02d}:{ss:02d}"


_URL_RE = re.compile(r"https?://[^\s<>\"']+")


def extract_link_markers(
    markers: list[dict[str, Any]],
    tl_start_frame: int,
    fps: float,
) -> list[dict[str, Any]]:
    """Extract `Purple` (or URL-bearing `note`) link markers mapped to YouTube timestamps & Resolve TCs."""
    links: list[dict[str, Any]] = []
    for m in sorted(markers, key=lambda x: int(x.get("frame", 0))):
        note = str(m.get("note") or "").strip()
        color = str(m.get("color") or "").strip()
        url_match = _URL_RE.search(note) or _URL_RE.search(str(m.get("name") or ""))
        if not url_match:
            continue
        if color.lower() != "purple" and not note.startswith(("http://", "https://", "qr:", "link:")):
            continue
        url = url_match.group(0).rstrip(".,;)")
        frame = int(m.get("frame", 0))
        rel_sec = max(0.0, (frame - tl_start_frame) / max(1.0, fps))
        title = str(m.get("name") or "").strip()
        if title.startswith(("http://", "https://")) or not title:
            title = "Explore Resource & Code"
        links.append(
            {
                "frame": frame,
                "tc": str(m.get("tc") or ""),
                "yt_ts": _sec_to_yt_ts(rel_sec),
                "title": title,
                "url": url,
                "color": color,
            }
        )
    return links


def format_youtube_chapters(markers: list[dict[str, Any]], tl_start_frame: int, fps: float) -> list[str]:
    """Build YouTube-compliant chapter lines (`00:00 Intro`, >=10s spacing), excluding pure `Purple` link markers."""
    if not markers:
        return []
    raw_items: list[tuple[float, str]] = []
    for m in sorted(markers, key=lambda x: int(x.get("frame", 0))):
        if str(m.get("color") or "").lower() == "purple":
            continue
        rel_frames = max(0, int(m.get("frame", 0)) - tl_start_frame)
        sec = rel_frames / max(1.0, fps)
        # Strip leading numbering like "01. " for cleaner YouTube chapter titles
        clean_title = re.sub(r"^\d+[\.\)]\s*", "", str(m.get("name", "")).strip()) or "Chapter"
        raw_items.append((sec, clean_title))

    # YouTube requires the very first chapter to start at exact `00:00` and >=10s between chapters
    chapters: list[tuple[float, str]] = []
    for idx, (sec, title) in enumerate(raw_items):
        eff_sec = 0.0 if idx == 0 and sec < 8.0 else sec
        if idx == 0 and eff_sec > 0.0:
            chapters.append((0.0, "Intro"))
        if chapters and (eff_sec - chapters[-1][0]) < 10.0:
            continue
        chapters.append((eff_sec, title))

    return [f"{_sec_to_yt_ts(sec)} {title}" for sec, title in chapters]


def _encode_data_uri(img_path: Path) -> str:
    mime = mimetypes.guess_type(img_path.name)[0] or "image/png"
    b64 = base64.b64encode(img_path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{b64}"


def _extract_image_from_openrouter_payload(data: dict[str, Any]) -> tuple[bytes | None, str | None]:
    """Extract generated image bytes or URL across OpenRouter's Flux / Gemini image response shapes."""
    choices = data.get("choices") or []
    if not choices:
        return None, None
    msg = choices[0].get("message") or {}

    # Shape 1: `message.images = [{"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}}]`
    for img_obj in msg.get("images") or []:
        url_str = ""
        if isinstance(img_obj, dict):
            iu = img_obj.get("image_url") or img_obj
            url_str = str(iu.get("url", "")) if isinstance(iu, dict) else str(iu)
        elif isinstance(img_obj, str):
            url_str = img_obj
        m = DATA_URI_RE.match(url_str.strip())
        if m:
            return base64.b64decode(m.group("b64")), None
        if url_str.startswith(("http://", "https://")):
            return None, url_str

    # Shape 2: `message.content` array or markdown `![](data:image/...)`
    content = msg.get("content")
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and part.get("type") == "image_url":
                u = str((part.get("image_url") or {}).get("url", ""))
                m = DATA_URI_RE.match(u.strip())
                if m:
                    return base64.b64decode(m.group("b64")), None
                if u.startswith(("http://", "https://")):
                    return None, u
    elif isinstance(content, str):
        m = DATA_URI_RE.match(content.strip())
        if m:
            return base64.b64decode(m.group("b64")), None

    return None, None


async def generate_youtube_thumbnail(
    *,
    prompt: str,
    output_path: str | Path,
    cfg: Config,
    reference_images: list[Path] | None = None,
    model_override: str | None = None,
    aspect_ratio: str = "16:9",
) -> dict[str, Any]:
    """Generate a 16:9 YouTube thumbnail via OpenRouter (Flux 3 / Flux 2 Pro / multimodal image+prompt).

    Supports feeding 1+ reference images (e.g. a grabbed Resolve viewer frame + webcam face frame)
    straight into OpenRouter's `messages` array alongside the text prompt!
    """
    or_key = cfg.keys.get("openrouter")
    if not or_key:
        raise RuntimeError(
            "OPENROUTER_API_KEY is required for OpenRouter Flux thumbnail generation (`/keys openrouter <key>`)."
        )

    model_slug = (model_override or cfg.image_model or "black-forest-labs/flux.2-pro").strip()
    dst = Path(output_path).resolve()
    dst.parent.mkdir(parents=True, exist_ok=True)

    content_parts: list[dict[str, Any]] = []
    for ref in reference_images or []:
        if ref.is_file():
            content_parts.append({"type": "image_url", "image_url": {"url": _encode_data_uri(ref)}})
    content_parts.append({"type": "text", "text": prompt})

    req_body: dict[str, Any] = {
        "model": model_slug,
        "modalities": ["image", "text"],
        "image_config": {"aspect_ratio": aspect_ratio},
        "messages": [{"role": "user", "content": content_parts}],
    }

    headers = {
        "Authorization": f"Bearer {or_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/davag",
        "X-Title": "davag",
    }

    async with aiohttp.ClientSession() as http:
        async with http.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers=headers,
            json=req_body,
            timeout=aiohttp.ClientTimeout(total=180),
        ) as resp:
            raw_json = await resp.json()
            if resp.status >= 400:
                err_detail = raw_json.get("error", raw_json)
                raise RuntimeError(f"OpenRouter {model_slug} HTTP {resp.status}: {err_detail}")

        img_bytes, remote_url = _extract_image_from_openrouter_payload(raw_json)
        if img_bytes is None and remote_url:
            async with http.get(remote_url, timeout=aiohttp.ClientTimeout(total=60)) as dl:
                dl.raise_for_status()
                img_bytes = await dl.read()

    if not img_bytes:
        raise RuntimeError(
            f"OpenRouter model '{model_slug}' did not return an image payload (verify model supports image output)."
        )

    dst.write_bytes(img_bytes)
    return {
        "path": str(dst),
        "bytes": len(img_bytes),
        "model": model_slug,
        "reference_count": len(reference_images or []),
        "aspect_ratio": aspect_ratio,
    }


def _render_thumbnail_end_screen_clip(
    *,
    thumb_file: Path,
    output_mp4: Path,
    width: int,
    height: int,
    fps: float,
    duration_sec: float,
    dim_for_cards: bool = True,
) -> int:
    """Render a frame-exact H.264 `.mp4` end-screen slate from `thumb_file`.

    Softly blurs & dims the thumbnail (`brightness -0.28`) so YouTube Studio's interactive
    Video/Playlist + Subscribe End Screen cards pop cleanly without colliding with bold text!
    """
    import shutil
    import subprocess

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg not found on PATH for end-screen generation")

    total_frames = max(15, round(duration_sec * fps))
    eff_dur = total_frames / fps
    fade_in = min(0.35, eff_dur * 0.2)
    fade_out_start = max(fade_in, eff_dur - min(0.45, eff_dur * 0.2))
    backdrop_fx = ",boxblur=16:2,eq=brightness=-0.28:saturation=0.85" if dim_for_cards else ""
    vf = (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black{backdrop_fx},format=yuv420p,"
        f"fade=t=in:st=0:d={fade_in:.2f},fade=t=out:st={fade_out_start:.2f}:d={eff_dur - fade_out_start:.2f}"
    )
    output_mp4.parent.mkdir(parents=True, exist_ok=True)
    res = subprocess.run(
        [
            ffmpeg,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-loop",
            "1",
            "-framerate",
            f"{fps:.4f}",
            "-i",
            str(thumb_file),
            "-vf",
            vf,
            "-frames:v",
            str(total_frames),
            "-c:v",
            "libx264",
            "-crf",
            "16",
            "-preset",
            "veryfast",
            "-pix_fmt",
            "yuv420p",
            str(output_mp4),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if res.returncode != 0 or not output_mp4.is_file() or output_mp4.stat().st_size == 0:
        raise RuntimeError(f"End-screen render failed: {res.stderr.strip()}")
    return total_frames


async def stage_youtube_package(
    *,
    transport: ResolveTransport,
    staging_root: Path,
    title: str,
    description_intro: str = "",
    tags: list[str] | None = None,
    privacy_status: str = "private",
    category_id: str = "28",  # 28 = Science & Technology
    thumbnail_path: str | None = None,
    primary_url: str | None = None,
    first_comment: str | None = None,
    render_qr_markers: bool = True,
    append_end_screen: bool = True,
    end_screen_sec: float = 8.0,
    queue_render: bool = True,
    start_render: bool = False,
) -> dict[str, Any]:
    """Compile the active DaVinci Resolve timeline & markers into a complete YouTube release bundle."""
    import asyncio
    import shutil

    from davag.media_intel.motion import find_headless_browser, render_html_svg_overlay

    from .qr import build_qr_callout_html

    st = await transport.call("resolve.status", timeout=4.0)
    proj = st.get("project") or {}
    tl = st.get("timeline") or {}
    if not tl:
        raise RuntimeError("No active timeline in DaVinci Resolve to stage for YouTube.")

    fps = float(proj.get("fps") or 30.0)
    width = int(proj.get("width") or 1920)
    height = int(proj.get("height") or 1080)
    tl_state = await transport.call("timeline.get_state", {"include_properties": False})
    mk_res = await transport.call("marker.list")
    all_markers = mk_res.get("markers") or []
    tl_start = int(tl_state.get("start_frame", 0))

    slug = _slugify(title or tl.get("name", "video"))
    pkg_dir = (staging_root / slug).resolve()
    pkg_dir.mkdir(parents=True, exist_ok=True)

    prev_manifest: dict[str, Any] = {}
    manifest_file = pkg_dir / "youtube_manifest.json"
    if manifest_file.is_file():
        try:
            prev_manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
        except ValueError:
            prev_manifest = {}

    staged_thumb = pkg_dir / "thumbnail.png"
    if thumbnail_path:
        src_th = Path(thumbnail_path).resolve()
        if src_th.is_file() and src_th != staged_thumb.resolve():
            shutil.copy2(src_th, staged_thumb)
    elif not staged_thumb.is_file():
        root_thumb = (staging_root / "thumbnail.png").resolve()
        if root_thumb.is_file():
            shutil.copy2(root_thumb, staged_thumb)
        else:
            mp_res = await transport.call("mediapool.clips", {"recursive": True})
            for c in mp_res.get("clips") or []:
                cname = str(c.get("name") or "").lower()
                cfp = str(c.get("file_path") or "")
                if (
                    cfp
                    and Path(cfp).suffix.lower() in (".png", ".jpg", ".jpeg", ".webp")
                    and any(k in cname for k in ("thumb", "_tn", "tn_"))
                    and Path(cfp).is_file()
                ):
                    shutil.copy2(cfp, staged_thumb)
                    break

    # 1. Extract `Purple` Link Markers & `Cyan`/standard Chapter Markers
    link_markers = extract_link_markers(all_markers, tl_start, fps)
    chapter_lines = format_youtube_chapters(all_markers, tl_start, fps)
    chapters_block = "\n".join(chapter_lines)

    qr_overlays_placed: list[dict[str, Any]] = []
    if render_qr_markers and link_markers:
        has_browser = True
        try:
            find_headless_browser()
        except RuntimeError:
            has_browser = False

        if has_browser:
            ov_dir = staging_root.parent / "overlays"
            ov_dir.mkdir(parents=True, exist_ok=True)
            for idx_lm, lm in enumerate(link_markers):
                qr_slug = f"qr_{idx_lm + 1:02d}_{_slugify(lm['title'])[:28]}"
                qr_mov = ov_dir / f"{qr_slug}.mov"
                qr_html = build_qr_callout_html(title=lm["title"], url=lm["url"])
                ov_res = await render_html_svg_overlay(
                    html_or_svg=qr_html,
                    output_path=qr_mov,
                    width=width,
                    height=height,
                    fps=fps,
                    duration_sec=6.5,
                )
                imp_qr = await transport.call("mediapool.import", {"paths": [ov_res["path"]], "bin_path": "Overlays"})
                imp_qr_clips = imp_qr.get("clips") or []
                if imp_qr_clips:
                    v_tracks = tl_state.get("tracks", {}).get("video", [])
                    target_tr = 2
                    ov_f = int(ov_res["frames"])
                    rec_f = int(lm["frame"])
                    while target_tr <= len(v_tracks):
                        tr_items = v_tracks[target_tr - 1].get("items") or []
                        stale_qr = [
                            it["item_id"]
                            for it in tr_items
                            if it.get("item_id")
                            and str(it.get("name") or "").startswith("qr_")
                            and abs(int(it.get("record_start", 0)) - rec_f) <= 5
                        ]
                        if stale_qr:
                            await transport.call("timeline.delete_clips", {"item_ids": stale_qr, "ripple": False})
                            tl_state = await transport.call("timeline.get_state", {"include_properties": False})
                            v_tracks = tl_state.get("tracks", {}).get("video", [])
                            tr_items = v_tracks[target_tr - 1].get("items") or []
                        collision = any(
                            int(it.get("record_start", 0)) < rec_f + ov_f and int(it.get("record_end", 0)) > rec_f
                            for it in tr_items
                        )
                        if not collision:
                            break
                        target_tr += 1
                    while len(v_tracks) < target_tr:
                        await transport.call("timeline.track_state", {"track_type": "video", "add_track": True, "name": "Overlays"})
                        tl_state = await transport.call("timeline.get_state", {"include_properties": False})
                        v_tracks = tl_state.get("tracks", {}).get("video", [])
                    app_qr = await transport.call(
                        "timeline.append_clips",
                        {
                            "clips": [
                                {
                                    "clip_id": imp_qr_clips[0]["clip_id"],
                                    "start_frame": 0,
                                    "end_frame": ov_f - 1,
                                    "track_index": target_tr,
                                    "record_frame": rec_f,
                                    "media_type": 1,
                                }
                            ]
                        },
                    )
                    tl_state = await transport.call("timeline.get_state", {"include_properties": False})
                    placed_qr = (app_qr.get("items") or [{}])[0]
                    qr_overlays_placed.append(
                        {
                            "title": lm["title"],
                            "url": lm["url"],
                            "tc": lm["tc"],
                            "track": target_tr,
                            "path": ov_res["path"],
                            "item_id": placed_qr.get("item_id"),
                        }
                    )

    # 2. Export 00:00:00-aligned captions `.srt` if timeline has non-purple markers
    srt_file: str | None = None
    if any(str(m.get("color") or "").lower() != "purple" for m in all_markers):
        vo_res = await transport.call(
            "timeline.vo_script",
            {"output_dir": str(pkg_dir), "prefix": slug, "import_srt_track": False},
        )
        srt_file = vo_res.get("youtube_srt")

    # 3. Assemble Above-the-Fold Links + Intro + Topics & Links + Chapters in `DESCRIPTION.md` & `COMMENT.md`
    top_url = (primary_url or (link_markers[0]["url"] if link_markers else "")).strip()
    desc_parts: list[str] = []
    if top_url and top_url not in (description_intro or ""):
        desc_parts.append(f"Interactive Code & Resources: {top_url}")
    if description_intro and description_intro.strip():
        desc_parts.append(description_intro.strip())
    if link_markers:
        links_lines = [f"{lm['yt_ts']} — {lm['title']}: {lm['url']}" for lm in link_markers]
        desc_parts.append("Topics & Links:\n" + "\n".join(links_lines))
    if chapters_block:
        desc_parts.append(f"Chapters:\n{chapters_block}")
    full_description = "\n\n".join(desc_parts)
    (pkg_dir / "DESCRIPTION.md").write_text(f"# {title}\n\n{full_description}\n", encoding="utf-8")

    eff_comment = (first_comment or "").strip()
    if not eff_comment and (top_url or link_markers):
        c_lines = ["Code, Interactive Notebooks & Further Reading:"]
        if top_url:
            c_lines.append(f"• Main Repo / Demo: {top_url}")
        for lm in link_markers:
            if lm["url"] != top_url or len(link_markers) > 1:
                c_lines.append(f"• [{lm['yt_ts']}] {lm['title']}: {lm['url']}")
        eff_comment = "\n".join(c_lines)
    if eff_comment:
        (pkg_dir / "COMMENT.md").write_text(eff_comment + "\n", encoding="utf-8")

    # 4. Append Thumbnail as Final End-Screen Slate on `Video 1` by default (5.0s for YouTube End Cards)
    end_screen_info: dict[str, Any] | None = None
    if append_end_screen and end_screen_sec > 0 and staged_thumb.is_file():
        v_tracks = tl_state.get("tracks", {}).get("video", [])
        if v_tracks:
            v1_items = v_tracks[0].get("items") or []
            stale_end_ids = [
                it["item_id"]
                for it in v1_items
                if it.get("item_id") and str(it.get("name") or "").endswith("_end_screen.mp4")
            ]
            if stale_end_ids:
                await transport.call("timeline.delete_clips", {"item_ids": stale_end_ids, "ripple": False})
                tl_state = await transport.call("timeline.get_state", {"include_properties": False})

        end_mp4 = pkg_dir / f"{slug}_end_screen.mp4"
        end_frames = await asyncio.to_thread(
            _render_thumbnail_end_screen_clip,
            thumb_file=staged_thumb,
            output_mp4=end_mp4,
            width=width,
            height=height,
            fps=fps,
            duration_sec=float(end_screen_sec),
        )
        imp = await transport.call("mediapool.import", {"paths": [str(end_mp4)], "bin_path": "Overlays"})
        imp_clips = imp.get("clips") or []
        if imp_clips:
            end_cid = imp_clips[0]["clip_id"]
            rec_frame = int(tl_state.get("end_frame", tl_start))
            app_res = await transport.call(
                "timeline.append_clips",
                {
                    "clips": [
                        {
                            "clip_id": end_cid,
                            "start_frame": 0,
                            "end_frame": end_frames - 1,
                            "track_index": 1,
                            "record_frame": rec_frame,
                            "media_type": 1,
                        }
                    ]
                },
            )
            placed = (app_res.get("items") or [{}])[0]
            tl_state = await transport.call("timeline.get_state", {"include_properties": False})
            end_screen_info = {
                "path": str(end_mp4),
                "frames": end_frames,
                "duration_sec": round(end_frames / fps, 2),
                "item_id": placed.get("item_id"),
                "record_start_tc": placed.get("record_start_tc"),
            }

    # 5. Optionally queue (and optionally start) H.264/MP4 YouTube Master render in Resolve
    video_master = pkg_dir / f"{slug}_master.mp4"
    render_job_id: str | None = None
    if queue_render:
        pr_res = await transport.call("render.presets")
        avail_presets = pr_res.get("presets") or []
        chosen_preset = next(
            (cand for cand in ("YouTube - 2160p", "YouTube - 1080p", "H.264 Master") if cand in avail_presets),
            None,
        )
        q_params: dict[str, Any] = {
            "target_dir": str(pkg_dir),
            "custom_name": f"{slug}_master",
            "select_all_frames": True,
        }
        if chosen_preset:
            q_params["preset_name"] = chosen_preset
        else:
            q_params["format"] = "mp4"
            q_params["codec"] = "H264"
        q_res = await transport.call("render.queue_add", q_params)
        render_job_id = q_res.get("job_id")
        if start_render and render_job_id:
            await transport.call("render.start", {"job_ids": [render_job_id], "wait": True})

    thumb_final = str(staged_thumb)
    manifest = {
        "title": title,
        "slug": slug,
        "timeline": tl.get("name"),
        "duration_sec": round(int(tl_state.get("duration_frames", 0)) / fps, 2),
        "privacy_status": privacy_status if privacy_status in ("private", "unlisted", "public") else "private",
        "category_id": str(category_id),
        "tags": tags or [],
        "primary_url": top_url or None,
        "links": link_markers,
        "qr_overlays": qr_overlays_placed,
        "description": full_description,
        "first_comment": eff_comment or None,
        "chapters": chapter_lines,
        "video_path": str(video_master),
        "render_job_id": render_job_id,
        "thumbnail_path": thumb_final if Path(thumb_final).is_file() else None,
        "end_screen": end_screen_info,
        "captions_srt": srt_file,
        "video_id": prev_manifest.get("video_id"),
        "watch_url": prev_manifest.get("watch_url"),
        "studio_url": prev_manifest.get("studio_url"),
        "comment_id": prev_manifest.get("comment_id"),
    }
    manifest_file.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return {
        "staging_dir": str(pkg_dir),
        "manifest_path": str(manifest_file),
        "description_path": str(pkg_dir / "DESCRIPTION.md"),
        **manifest,
    }


async def upload_staged_to_youtube(
    *,
    manifest_path: str | Path,
    home: Path,
) -> dict[str, Any]:
    """Upload a staged `youtube_manifest.json` bundle to YouTube Data API v3 as a `private` draft.

    Uses `~/.davag/youtube_token.json` (`client_id`, `client_secret`, `refresh_token`) via OAuth2
    refresh flow + YouTube resumable upload protocol (`aiohttp`, zero extra SDK dependencies).
    """
    m_file = Path(manifest_path).resolve()
    if not m_file.is_file():
        raise FileNotFoundError(f"YouTube manifest not found: {m_file}")
    manifest = json.loads(m_file.read_text(encoding="utf-8"))

    video_file = Path(str(manifest.get("video_path", "")))
    if not video_file.is_file():
        raise FileNotFoundError(
            f"Rendered master video not found at {video_file} — finish the Resolve render job first (`render.start`)."
        )

    tok_file = home / "youtube_token.json"
    if not tok_file.is_file() and (home / "youtube_token.json.json").is_file():
        tok_file = home / "youtube_token.json.json"
    if not tok_file.is_file():
        raise RuntimeError(
            f"YouTube OAuth token not configured at {home / 'youtube_token.json'}. "
            "Run `/youtube-auth <path-to-google-client-secret.json>` for one-click browser login, "
            f"or drag the staged bundle ({m_file.parent}) straight into YouTube Studio!"
        )

    creds = json.loads(tok_file.read_text(encoding="utf-8"))
    if "installed" in creds and "refresh_token" not in creds:
        creds = await run_youtube_oauth_flow(home=home, client_secret_path=tok_file)
    async with aiohttp.ClientSession() as http:
        # 1. Exchange refresh_token for short-lived access_token
        async with http.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": creds["client_id"],
                "client_secret": creds["client_secret"],
                "refresh_token": creds["refresh_token"],
                "grant_type": "refresh_token",
            },
        ) as tr:
            tok_data = await tr.json()
            if tr.status >= 400 or "access_token" not in tok_data:
                raise RuntimeError(f"YouTube OAuth refresh failed: {tok_data}")
            access_tok = tok_data["access_token"]

        auth_hdr = {"Authorization": f"Bearer {access_tok}"}
        meta_body = {
            "snippet": {
                "title": manifest["title"][:100],
                "description": manifest.get("description", "")[:5000],
                "tags": manifest.get("tags", []),
                "categoryId": str(manifest.get("category_id", "28")),
            },
            "status": {
                "privacyStatus": manifest.get("privacy_status", "private"),
                "selfDeclaredMadeForKids": False,
            },
        }

        # 2. Initiate resumable video upload
        vid_size = video_file.stat().st_size
        init_headers = {
            **auth_hdr,
            "Content-Type": "application/json; charset=UTF-8",
            "X-Upload-Content-Length": str(vid_size),
            "X-Upload-Content-Type": "video/mp4",
        }
        async with http.post(
            "https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&part=snippet,status",
            headers=init_headers,
            json=meta_body,
        ) as init_resp:
            if init_resp.status >= 400:
                raise RuntimeError(f"YouTube upload init failed ({init_resp.status}): {await init_resp.text()}")
            upload_url = init_resp.headers.get("Location")
            if not upload_url:
                raise RuntimeError("YouTube upload init returned no resumable Location header.")

        # 3. Stream video file body in non-blocking 4 MB chunks
        import asyncio

        async def _file_chunks(p: Path, chunk_size: int = 4 * 1024 * 1024):
            offset = 0
            while offset < vid_size:
                chunk = await asyncio.to_thread(
                    lambda off=offset: p.open("rb").seek(off) and b""  # pragma: no cover
                )
                _ = chunk

                def _read_slice(off: int = offset) -> bytes:
                    with p.open("rb") as f_in:
                        f_in.seek(off)
                        return f_in.read(chunk_size)

                buf = await asyncio.to_thread(_read_slice)
                if not buf:
                    break
                offset += len(buf)
                yield buf

        async with http.put(
            upload_url,
            headers={"Content-Length": str(vid_size), "Content-Type": "video/mp4"},
            data=_file_chunks(video_file),
            timeout=aiohttp.ClientTimeout(total=3600),
        ) as up_resp:
            vid_json = await up_resp.json()
            if up_resp.status >= 400:
                raise RuntimeError(f"YouTube video upload failed: {vid_json}")
            video_id = vid_json["id"]

        # 4. Upload custom thumbnail if staged
        thumb_uploaded = False
        thumb_p = Path(str(manifest.get("thumbnail_path") or ""))
        if thumb_p.is_file():
            thumb_bytes = await asyncio.to_thread(thumb_p.read_bytes)
            async with http.post(
                f"https://www.googleapis.com/upload/youtube/v3/thumbnails/set?videoId={video_id}&uploadType=media",
                headers={**auth_hdr, "Content-Type": "image/png"},
                data=thumb_bytes,
            ) as th_resp:
                thumb_uploaded = th_resp.status < 300

        # 5. Upload English `.srt` captions track if staged and non-empty
        captions_uploaded = False
        srt_p = Path(str(manifest.get("captions_srt") or ""))
        if srt_p.is_file() and srt_p.stat().st_size > 16:
            srt_bytes = await asyncio.to_thread(srt_p.read_bytes)
            cap_meta = json.dumps(
                {"snippet": {"videoId": video_id, "language": "en", "name": "English", "isDraft": False}}
            ).encode("utf-8")
            boundary = "davag_yt_caption_boundary"
            mp_body = (
                f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n".encode()
                + cap_meta
                + f"\r\n--{boundary}\r\nContent-Type: application/x-subrip\r\n\r\n".encode()
                + srt_bytes
                + f"\r\n--{boundary}--\r\n".encode()
            )
            async with http.post(
                "https://www.googleapis.com/upload/youtube/v3/captions?part=snippet&uploadType=multipart",
                headers={**auth_hdr, "Content-Type": f"multipart/related; boundary={boundary}"},
                data=mp_body,
            ) as cap_resp:
                captions_uploaded = cap_resp.status < 300

        # 6. Auto-post top-level comment if `first_comment` is staged and privacy is `unlisted` or `public`
        comment_id: str | None = manifest.get("comment_id")
        eff_priv = str(manifest.get("privacy_status", "private"))
        first_cmt = str(manifest.get("first_comment") or "").strip()
        if first_cmt and not comment_id and eff_priv in ("unlisted", "public"):
            async with http.post(
                "https://www.googleapis.com/youtube/v3/commentThreads?part=snippet",
                headers={**auth_hdr, "Content-Type": "application/json; charset=UTF-8"},
                json={
                    "snippet": {
                        "videoId": video_id,
                        "topLevelComment": {"snippet": {"textOriginal": first_cmt}},
                    }
                },
            ) as cmt_resp:
                if cmt_resp.status < 300:
                    cmt_json = await cmt_resp.json()
                    comment_id = cmt_json.get("id")

    studio_url = f"https://studio.youtube.com/video/{video_id}/edit"
    watch_url = f"https://youtu.be/{video_id}"
    manifest["video_id"] = video_id
    manifest["studio_url"] = studio_url
    manifest["watch_url"] = watch_url
    manifest["comment_id"] = comment_id
    m_file.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    return {
        "video_id": video_id,
        "studio_url": studio_url,
        "watch_url": watch_url,
        "privacy_status": eff_priv,
        "thumbnail_uploaded": thumb_uploaded,
        "captions_uploaded": captions_uploaded,
        "comment_id": comment_id,
    }


async def update_youtube_video(
    *,
    manifest_path: str | Path,
    home: Path,
    video_id: str | None = None,
    privacy_status: str | None = None,
    post_first_comment: bool = True,
) -> dict[str, Any]:
    """Update title, description links/chapters, tags, privacy status, thumbnail & first comment on an existing YouTube video."""
    import asyncio

    m_file = Path(manifest_path).resolve()
    manifest = json.loads(m_file.read_text(encoding="utf-8"))
    vid_id = (video_id or manifest.get("video_id") or "").strip()
    if not vid_id:
        raise ValueError("youtube.update requires 'video_id' (either in youtube_manifest.json or passed explicitly).")

    tok_file = home / "youtube_token.json"
    if not tok_file.is_file() and (home / "youtube_token.json.json").is_file():
        tok_file = home / "youtube_token.json.json"
    creds = json.loads(tok_file.read_text(encoding="utf-8"))
    if "installed" in creds and "refresh_token" not in creds:
        creds = await run_youtube_oauth_flow(home=home, client_secret_path=tok_file)

    eff_priv = privacy_status or manifest.get("privacy_status", "private")
    async with aiohttp.ClientSession() as http:
        async with http.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": creds["client_id"],
                "client_secret": creds["client_secret"],
                "refresh_token": creds["refresh_token"],
                "grant_type": "refresh_token",
            },
        ) as tr:
            tok_data = await tr.json()
            if tr.status >= 400 or "access_token" not in tok_data:
                raise RuntimeError(f"YouTube OAuth refresh failed: {tok_data}")
            access_tok = tok_data["access_token"]

        auth_hdr = {"Authorization": f"Bearer {access_tok}"}
        put_body = {
            "id": vid_id,
            "snippet": {
                "title": manifest["title"][:100],
                "description": manifest.get("description", "")[:5000],
                "tags": manifest.get("tags", []),
                "categoryId": str(manifest.get("category_id", "28")),
            },
            "status": {
                "privacyStatus": eff_priv,
                "selfDeclaredMadeForKids": False,
            },
        }
        async with http.put(
            "https://www.googleapis.com/youtube/v3/videos?part=snippet,status",
            headers={**auth_hdr, "Content-Type": "application/json; charset=UTF-8"},
            json=put_body,
        ) as up_resp:
            if up_resp.status >= 400:
                raise RuntimeError(f"YouTube metadata update failed ({up_resp.status}): {await up_resp.text()}")

        thumb_uploaded = False
        thumb_p = Path(str(manifest.get("thumbnail_path") or ""))
        if thumb_p.is_file():
            thumb_bytes = await asyncio.to_thread(thumb_p.read_bytes)
            async with http.post(
                f"https://www.googleapis.com/upload/youtube/v3/thumbnails/set?videoId={vid_id}&uploadType=media",
                headers={**auth_hdr, "Content-Type": "image/png"},
                data=thumb_bytes,
            ) as th_resp:
                thumb_uploaded = th_resp.status < 300

        comment_id = manifest.get("comment_id")
        first_cmt = str(manifest.get("first_comment") or "").strip()
        if post_first_comment and first_cmt and not comment_id and eff_priv in ("unlisted", "public"):
            async with http.post(
                "https://www.googleapis.com/youtube/v3/commentThreads?part=snippet",
                headers={**auth_hdr, "Content-Type": "application/json; charset=UTF-8"},
                json={
                    "snippet": {
                        "videoId": vid_id,
                        "topLevelComment": {"snippet": {"textOriginal": first_cmt}},
                    }
                },
            ) as cmt_resp:
                if cmt_resp.status < 300:
                    comment_id = (await cmt_resp.json()).get("id")

    manifest["video_id"] = vid_id
    manifest["privacy_status"] = eff_priv
    manifest["studio_url"] = f"https://studio.youtube.com/video/{vid_id}/edit"
    manifest["watch_url"] = f"https://youtu.be/{vid_id}"
    manifest["comment_id"] = comment_id
    m_file.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    return {
        "video_id": vid_id,
        "studio_url": manifest["studio_url"],
        "watch_url": manifest["watch_url"],
        "privacy_status": eff_priv,
        "thumbnail_uploaded": thumb_uploaded,
        "comment_id": comment_id,
    }


async def run_youtube_oauth_flow(
    *,
    home: Path,
    client_secret_path: Path | None = None,
) -> dict[str, Any]:
    """Run a one-time local loopback Google OAuth2 consent flow in the user's browser and save `~/.davag/youtube_token.json`."""
    import asyncio
    import urllib.parse
    import webbrowser

    from aiohttp import web

    dst_tok = home / "youtube_token.json"
    candidates = [
        p
        for p in (
            client_secret_path,
            dst_tok,
            home / "youtube_token.json.json",
        )
        if p is not None and Path(p).is_file()
    ]
    if not candidates:
        raise FileNotFoundError(
            f"Pass your downloaded Google Desktop OAuth JSON (`/youtube-auth C:\\path\\to\\client_secret.json`) "
            f"or place it at {dst_tok}."
        )

    raw = json.loads(Path(candidates[0]).read_text(encoding="utf-8"))
    cfg = raw.get("installed") or raw.get("web") or raw
    client_id = cfg.get("client_id")
    client_secret = cfg.get("client_secret")
    if not client_id or not client_secret:
        raise ValueError("OAuth JSON is missing 'client_id' or 'client_secret'.")

    code_fut: asyncio.Future[str] = asyncio.get_running_loop().create_future()

    async def _cb(req: web.Request) -> web.Response:
        if "error" in req.query:
            if not code_fut.done():
                code_fut.set_exception(RuntimeError(f"Google OAuth error: {req.query['error']}"))
            return web.Response(text=f"OAuth failed: {req.query['error']}", status=400)
        code = req.query.get("code", "")
        if code and not code_fut.done():
            code_fut.set_result(code)
        return web.Response(
            text="<h2>davag YouTube OAuth authorized!</h2><p>You can close this tab and return to davag.</p>",
            content_type="text/html",
        )

    app = web.Application()
    app.router.add_get("/", _cb)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = int(site._server.sockets[0].getsockname()[1])  # type: ignore[union-attr]
    redirect_uri = f"http://127.0.0.1:{port}"

    scopes = "https://www.googleapis.com/auth/youtube.upload https://www.googleapis.com/auth/youtube.force-ssl"
    auth_url = (cfg.get("auth_uri") or "https://accounts.google.com/o/oauth2/v2/auth") + "?" + urllib.parse.urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": scopes,
            "access_type": "offline",
            "prompt": "consent",
        }
    )

    try:
        webbrowser.open(auth_url)
        auth_code = await asyncio.wait_for(code_fut, timeout=240.0)
    finally:
        await runner.cleanup()

    async with aiohttp.ClientSession() as http, http.post(
        cfg.get("token_uri") or "https://oauth2.googleapis.com/token",
        data={
            "code": auth_code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        },
    ) as tr:
        tok_data = await tr.json()
        if tr.status >= 400 or "refresh_token" not in tok_data:
            raise RuntimeError(f"OAuth token exchange failed: {tok_data}")

    saved = {
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": tok_data["refresh_token"],
        "token_path": str(dst_tok),
    }
    home.mkdir(parents=True, exist_ok=True)
    dst_tok.write_text(json.dumps(saved, indent=2) + "\n", encoding="utf-8")
    return saved