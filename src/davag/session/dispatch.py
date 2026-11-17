"""Unified request dispatcher shared by both the Terminal TUI REPL and the Web GUI Sidecar.

Mirrors `davai/src/session/dispatch.js`:
- Typing while `agent.busy` queues a live **steer** instead of starting a second overlapping run.
- Slash commands (`/context`, `/tps`, `/resolve`, `/yolo`, etc.) execute immediately even mid-run!
- Expands `[[paste#N]]`, extracts `@image`, `@video`, and `@youtube` attachments, publishes
  transcript entries onto `SessionBus` (tagged with `origin` so TUI & Web mirror each other),
  and launches `session.agent.run()`.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from davag.context.media import (
    extract_image_refs,
    extract_video_refs,
    load_image_part,
    load_video_part,
    youtube_ref,
)
from davag.ui.commands import handle_command

from .bootstrap import Session


def create_dispatcher(
    *,
    session: Session,
    local_push: Callable[[dict[str, Any]], None] | None = None,
    on_exit: Callable[[], None] | None = None,
) -> Callable[[str, str], Any]:
    """Return `submit(raw_text, origin='tui')` coroutine."""

    def _push(entry: dict[str, Any], origin: str = "tui") -> None:
        tagged = {**entry, "origin": origin}
        session.bus.publish("transcript", tagged)
        if local_push is not None:
            local_push(tagged)

    async def submit(raw: str, origin: str = "tui") -> None:
        text = str(raw or "").strip()
        if not text:
            return

        # Typed while a model turn is in flight -> steer at next step boundary!
        if (
            session.agent.busy
            and not text.startswith("/")
            and text != "sh"
            and not text.startswith("sh ")
            and session.agent.steer(text)
        ):
                session.history.add(text)
                _push({"type": "user", "text": f"[steer] {text}"}, origin=origin)
                return

        cmd_res = await handle_command(
            text,
            session=session,
            push=lambda e: _push(e, origin=origin),
            exit_app=on_exit or (lambda: None),
        )
        if cmd_res.get("handled"):
            return

        prompt = str(cmd_res.get("replay_input") or text)
        session.history.add(prompt)

        # 1. Expand [[paste#N]]
        expanded, referenced = session.pastes.expand(prompt)
        for p in referenced:
            session.ledger.add(
                seg_type="paste",
                label=f"paste#{p.n} ({p.lines} lines)",
                role="user",
                text=f"[paste#{p.n}]\n{p.text}",
            )

        # 2. Extract @image refs
        without_images, images = extract_image_refs(expanded, session.sandbox)
        for img_ref in images:
            if not session.cfg.model.vision:
                _push(
                    {
                        "type": "warning",
                        "message": f"{session.cfg.model.id} has no vision support — {img_ref} skipped.",
                    },
                    origin=origin,
                )
                continue
            try:
                img = load_image_part(img_ref, session.sandbox)
                session.ledger.add(
                    seg_type="media",
                    label=f"image {img['name']} ({img['bytes'] // 1024}kB)",
                    role="user",
                    image=img,
                )
                _push(
                    {"type": "notice", "message": f"attached {img['name']} (~{img['tokens']} tokens)"},
                    origin=origin,
                )
            except Exception as err:  # noqa: BLE001
                _push({"type": "warning", "message": str(err)}, origin=origin)

        # 3. Extract @video and @youtube refs
        without_media, videos, youtube_urls = extract_video_refs(without_images, session.sandbox)
        for yt_url in youtube_urls:
            if not session.cfg.model.video:
                _push(
                    {
                        "type": "warning",
                        "message": f"{session.cfg.model.id} does not accept video — switch `/model gemini` to watch {yt_url}.",
                    },
                    origin=origin,
                )
                continue
            yt = youtube_ref(yt_url)
            session.ledger.add(seg_type="media", label=f"youtube {yt['uri']}", role="user", video=yt)
            _push(
                {
                    "type": "notice",
                    "message": f"attached {yt['uri']} (~{yt['tokens']:,} tokens, assuming {yt['minutes']} min)",
                },
                origin=origin,
            )

        for vid_ref in videos:
            try:
                from davag.media_intel.ffmpeg import probe_media

                v_info = probe_media(vid_ref)
                dur_s = float(v_info.get("duration_sec") or 0.0)
                # For long videos (>90s), allowlist the path for fast contact-sheet/transcribe/import
                # rather than burning 150k+ raw video tokens upfront!
                if dur_s > 90.0 or not session.cfg.model.video:
                    _push(
                        {
                            "type": "notice",
                            "message": f"authorized media path {vid_ref} ({dur_s:.1f}s — ready for media.contact_sheet / transcribe / mediapool.import)",
                        },
                        origin=origin,
                    )
                    continue

                vid = load_video_part(vid_ref, session.sandbox)
                session.ledger.add(
                    seg_type="media",
                    label=f"video {vid['name']} ({vid['delivery']})",
                    role="user",
                    video=vid,
                )
                _push(
                    {
                        "type": "notice",
                        "message": f"attached {vid['name']} via {vid['delivery']} (~{vid['tokens']:,} tokens)",
                    },
                    origin=origin,
                )
            except Exception as err:  # noqa: BLE001
                _push({"type": "warning", "message": str(err)}, origin=origin)

        if session.transport is None:
            await session.maybe_auto_connect_resolve()
        _push({"type": "user", "text": without_media}, origin=origin)
        await session.agent.run(without_media)

    return submit