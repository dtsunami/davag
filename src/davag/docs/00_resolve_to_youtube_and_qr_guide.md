# Resolve-to-YouTube Pipeline, In-Video QR Links & Overlays Studio

`davag` treats your **DaVinci Resolve Timeline + Markers + Media Pool** as the **single source of truth**, compiling your edit deterministically into both **in-video 4K motion graphics** and a complete **YouTube Studio release package**.

---

## 1. The 1-to-1 Resolve → YouTube Artifact Map

Every object in your DaVinci Resolve timeline compiles into a specific YouTube artifact inside `youtube_staging/<slug>/`:

| DaVinci Resolve Source | Compiled YouTube Artifact(s) |
|---|---|
| **Active Timeline Name** | Dedicated folder `youtube_staging/<slug>/` + `youtube_manifest.json` (`video_id` & `comment_id` persisted across updates) |
| **`Cyan` (or numbered) Markers** | **YouTube Chapters (`00:00 ...`)** in `DESCRIPTION.md` (enforces `00:00` first chapter & `>=10s` spacing automatically) |
| **`Purple` Link Markers** *(`name` = Topic Title, `note` = `https://...`)* | **1) In-Video Animated QR + Short-URL Overlay** rendered to transparent QuickTime PNG Alpha `.mov` on `Overlays` (`Video 2+`) at that exact timecode<br>**2) Line-1 Above-the-Fold Link** + **Timestamped `Topics & Links:`** list in `DESCRIPTION.md`<br>**3) `COMMENT.md` (`first_comment`)** auto-posted via `commentThreads.insert` when `unlisted` or `public` |
| **Subtitle Track (`English CC`)** | `<slug>_captions.srt` (`00:00:00,000`-aligned) → uploaded automatically via YouTube Captions API |
| **Thumbnail (`*tn*.png` or `/thumbnail`)** | **1) Custom 16:9 YouTube Thumbnail**<br>**2) `8.0s` Card-Ready Dimmed End-Screen Slate** (`<slug>_end_screen.mp4`) appended to `Video 1` |
| **Deliver Preset (`YouTube - 2160p`)** | `<slug>_master.mp4` → streamed via YouTube Resumable Upload API |

---

## 2. How In-Video Scannable QR Code Callouts Work

Over 40% of technical YouTube watch time happens on living-room TVs, tablets, or full-screen monitors where clicking description links is inconvenient. `davag` includes an **offline, zero-dependency ISO/IEC 18004 vector `<svg>` QR generator** (`davag.youtube.qr`) paired with the deterministic Headless Chrome/Edge CDP renderer:

1. **Three Ways to Add an In-Video QR Link Callout:**
   - **In the Web GUI (`🎬 timeline` tab):** Press `Q` (or click **`○ +QR`**) to drop a `Purple` QR marker at hover/playhead, enter a Title (`"Interactive Nyquist Notebook"`) and URL (`https://github.com/...`), preview the live `<svg>` QR card, and click **`🎬 Render QR Overlay to V3`**.
   - **Directly inside DaVinci Resolve (`M` key):** Press `M` on the timeline, set Marker Color to **`Purple`**, set Name to `"Interactive Nyquist Notebook"`, and paste `https://github.com/...` in the Marker Note. When `youtube.stage` runs, it automatically renders and places `6.5s` transparent QuickTime PNG QR overlays for every `Purple` marker!
   - **Via AI Agent (`motion.qr_callout` op):** Ask `davag`: *"Add a QR callout at `01:27:07` linking to `https://github.com/...` titled 'Try the Nyquist Demo'"*.

2. **Why It Scans Reliably from 8+ Feet Away:**
   - Vector `<svg>` `shape-rendering="crispEdges"` with a 3-module pure-white quiet zone (`~280×280 px` on a 4K frame).
   - Paired with a clean monospace display URL (`github.com/user/repo#nyquist`), animated progress bar, and cubic-bezier entrance/exit muxed into lossless **QuickTime PNG Straight Alpha (`.mov`)** (or optional 10-bit ProRes 4444 via `codec: "prores"`) on an unobstructed `Overlays` video track (`Video 3` if `Video 2` holds your Facecam PiP).

---

## 3. Links, Pinned Comments & End-Screen Cards (Without Monetization)

You **do not** need YouTube monetization (Partner Program) to drive viewers to GitHub repos, interactive GitHub Pages demos, or papers:

### A. One-Time Channel Setting (Required for Clickable GitHub URLs!)
In **YouTube Studio → Settings → Channel → Feature eligibility → `3. Advanced features`**, make sure **Advanced features** is **Enabled** (takes ~60 seconds via video selfie verification; requires **0 subscribers and 0 ads/monetization**). Without it, YouTube renders external `https://github.com/...` URLs in descriptions and comments as non-clickable text.

### B. Line-1 Above-the-Fold Description Links (`100%` Automated)
YouTube collapses descriptions after ~2 lines (`~130` characters) behind `...more`. Whenever you pass `primary_url` or place a `Purple` marker, `youtube.stage` puts:
```text
Interactive Code & Resources: https://github.com/your-org/your-repo
```
on **Line 1** so viewers see and click your link right below the video title without expanding the description.

### C. Top Comment Posting & Pinning (`95%` Automated)
- **API auto-post:** Your `~/.davag/youtube_token.json` (`youtube.force-ssl` scope) allows `youtube.upload` and `youtube.update` to post `COMMENT.md` (`first_comment`) automatically via `commentThreads.insert` **whenever `privacy_status` is `"unlisted"` or `"public"`**. *(YouTube's API blocks posting comments while a video is `"private"` — staging as `"unlisted"` keeps the video hidden from search/subscribers while allowing automated comment creation!)*
- **1-click Pin:** Google's Data API v3 has no `pin` endpoint, so when you open Studio to publish, simply click **`⋮ → Pin`** on the comment `davag` already posted.

### D. Card-Ready `8.0s` End-Screen Slate & Studio Cards
- **Why `davag` dims & blurs the thumbnail on the End Screen:** If the final seconds show a bright thumbnail covered in bold text, YouTube's interactive **Video/Playlist** rectangle and circular **Subscribe** button sit right on top of your text. `youtube.stage` automatically renders an **`8.0s` softly blurred, darkened (`-28%` brightness) backdrop** from your thumbnail on `Video 1` (`append_end_screen: true`, `end_screen_sec: 8.0`).
- **What cards to place in YouTube Studio (without monetizing):** Even without monetization, End Screen cards can point to **any YouTube video or playlist** (yours *or* curated deep-dive lectures on the math/CS topics!) plus your circular **Channel Subscribe** button. Once placed on one video, every future upload takes 1 click via **"Import from latest video"**.

### E. Updating Metadata In-Place (`youtube.update`)
Already uploaded your 4K master MP4 and want to add new GitHub links, update chapters, switch `"private"` → `"unlisted"`, and auto-post the first comment **without re-uploading 700 MB**? Run `youtube.stage` (`queue_render: false`) followed by `youtube.update` — it updates the live YouTube video via `PUT /youtube/v3/videos` in `<1 second`.

---

## 4. Using the Unified `🎬 timeline` NLE Studio (Markers, VO Prompter & Overlays)

Click **`🎬 timeline`** in the top navigation bar to inspect, scrub, prompt, and author VO cues, QR callouts, and JS motion overlays visually:

0. **Precision Tape-Measure Timeline, Hotkeys & Drag-to-Adjust:** At the top of `🎬 timeline`, a zoomable/pannable tape-measure ribbon displays geometric stemmed flags (`◇ Cyan` VO/Chapter `M`, `⬡ Mint` JS Overlay `O`, `○ Purple` QR Link `Q`, `△ Yellow` Note `N`), optional `V1`/`V2`/`V3` clip lanes, and the live red playhead. Hovering updates the large 16:9 Program Viewer (`/api/overlays/thumb`, with 1-click **`📸 HD Composite`** from Resolve) and synced VO Teleprompter HUD, and you can **drag markers or overlay clips horizontally** to re-time them directly in DaVinci Resolve!
1. **Inline QR Link Callout Builder:** Select any `🟣 Purple` marker in the synced left accordion (or press `Q` to drop one), edit Title + URL with live `<svg>` QR preview, and click **`🎬 Render QR Overlay to V3`** to render a lightweight QuickTime PNG Alpha `.mov` onto Resolve.
2. **Live HTML / CSS / SVG / JS Motion Sandbox & AI Co-Director:** Select any `🟢 Overlay` item, toggle **Live JS Sandbox** + the `t=0..dur` scrubber over the 16:9 Viewer to debug `window.renderFrame(f, t, dur)` in real time (live-updating via SSE as the AI streams `--davag:...--` code!), and click **`🎬 Render Overlay to Timeline`**.
3. **Synced Browser & Staged YouTube Bundles:** Filter by `All`, `🔵 VO / Ch`, `🟣 QR`, `🟢 Overlays`, or `📦 YouTube`, push VO cues straight to Resolve `ST1` `.srt` & Fairlight ADR `.csv` (`📺 SRT/ADR`), and inspect staged `youtube_manifest.json` release status.