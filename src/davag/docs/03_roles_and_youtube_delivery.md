# Capability Roles & YouTube Delivery Automation

## 1. Five-Tier Role Surfaces (`--role` / `/role` / Top-Bar Selector)

Scope what `davag` is permitted to see in its system prompt **and** what `executor.plan_batch` will allow to execute:

| Role | Allowed Capabilities | Blocked |
|---|---|---|
| **`reviewer`** | Read-only Resolve inspection (`timeline.get_state`, `marker.list`, `grab_still`), `media.contact_sheet`, `media.analyze_clip`, `media.transcribe`, `fs.read` | **All** timeline, bin, marker, color, render, and file mutations |
| **`logger`** | Everything in `reviewer` + `marker.add`/`delete`, `timeline.vo_script`, `media.save_digest`, `media.generate_resolve_proxy`, `mediapool.link_proxy` | Cutting/modifying timelines, Color, Fusion, Deliver, file writes |
| **`editor`** | Everything in `logger` + `timeline.*` assembly/trimming/transitions/speed/subtitles, `motion.render_overlay`, `audio.enhance`, `media.master_vo`, `otio.build`, `fs.write`/`replace` | `destructive` deletes, Deliver renders, `youtube.upload`, `resolve.eval`, `shell` |
| **`finisher`** | Everything in `editor` + `color.*`, `fusion.*`, `render.*`, `youtube.thumbnail`, `youtube.stage` | `resolve.eval`, `shell`, `youtube.upload`, `destructive` deletes |
| **`master`** | Full capability surface (default). `resolve.eval`, `shell`, `youtube.upload`, and `destructive` ops still pause for explicit operator `[y/c/n]` approval unless `--yolo`. | None |

Switch anytime via CLI (`davag --role reviewer`), REPL (`/role editor`), `.env` (`DAVAG_ROLE=editor`), or the **Role** dropdown in the Web GUI header!

---

## 2. OpenRouter Flux 3 / Multimodal Thumbnail Generation (`youtube.thumbnail` / `/thumbnail`)

OpenRouter exposes multimodal image-generation models (`black-forest-labs/flux.2-pro`, `black-forest-labs/flux-3`, `google/gemini-2.5-flash-image-preview`) via `/api/v1/chat/completions` with `modalities: ["image", "text"]`.

### Configure Image Model & Key

```ini
# In ~/.davag/.env
OPENROUTER_API_KEY=sk-or-v1-...
DAVAG_IMAGE_MODEL=black-forest-labs/flux.2-pro
```

### Usage

- **Quick CLI / Web Command** (automatically grabs your current Resolve viewer frame as visual reference!):
  ```text
  /thumbnail Bold YouTube tech thumbnail: shocked developer looking at "$0.00 CREDIT LEFT" red terminal alert vs clean interactive math graph, high contrast, 16:9
  ```
- **Agent `dr_ops` Call with Multiple Reference Images:**
  ```json
  {
    "op": "youtube.thumbnail",
    "grab_tc": "01:07:04:25",
    "reference_images": ["grab_current"],
    "model": "black-forest-labs/flux.2-pro",
    "prompt": "High-contrast 16:9 YouTube thumbnail featuring this exact terminal error screen on the left, bold yellow text '64K TOKENS BURNED?!', dramatic rim lighting"
  }
  ```

---

## 3. YouTube Staging & Private-Draft Upload (`youtube.stage` & `youtube.upload`)

1. **Stage the Complete Release Package (`/youtube-stage <Title>` or `youtube.stage`):**
   Creates `youtube_staging/<slug>/` containing:
   - `youtube_manifest.json` — title, tags, category (`28` Science & Tech), `privacy_status: "private"`, video/thumbnail/SRT paths.
   - `DESCRIPTION.md` — intro copy + **YouTube-compliant Chapters** generated automatically from your Resolve timeline markers (enforcing `00:00` start and `>=10s` spacing).
   - `<slug>_captions.srt` — `00:00:00,000`-aligned caption file.
   - **5-second Thumbnail Final Screen (`append_end_screen: true` by default):** Automatically renders a resolution/FPS-matched 5s end-screen slate from your thumbnail (with smooth fade-in/out) and appends it to the end of `Video 1` before queueing the render, ready for YouTube Studio End Screen cards.
   - Queued H.264/MP4 Deliver render job in Resolve (`render_job_id`).
2. **Optional Direct Upload as a `private` YouTube Studio Draft (`youtube.upload`):**
   - Place OAuth2 refresh credentials in `~/.davag/youtube_token.json` (`{"client_id": "...", "client_secret": "...", "refresh_token": "..."}`).
   - `youtube.upload` is **always approval-gated**: once approved, it streams the rendered MP4 via YouTube Data API v3's resumable upload endpoint as `"privacyStatus": "private"` and attaches `thumbnail.png`, returning your `https://studio.youtube.com/video/<id>/edit` link for final human review.