# DaVinci Resolve 21.2 Capability Matrix & Engineering Decisions

*Audited against DaVinci Resolve Studio 21.2.0.6 (`DaVinciResolveScript.pyi` & `CHANGELOG.md`, Nov 2026).*

## Full Capability Matrix

| Area | Status | `davag` Operations |
|---|---|---|
| **Project, Bins, Proxies & Metadata** | **Full** | `resolve.status`, `project.*`, `mediapool.*`, `media.generate_resolve_proxy` (QSV/NVENC) |
| **Timeline Create, Append, Duplicate & `/undo`** | **Full** | `timeline.create`, `timeline.duplicate`, `timeline.append_clips` (`record_tc`, `media_type`), `.davag-snap-*` |
| **In-Place Trim, Slip, Move, Gaps & Silence** *(21.1.2+)* | **Full** | `timeline.trim_and_move` (`SetLeftOffset`, `SetRightOffset`, `SetPosition`, `DeleteGaps`, `RippleDeleteSilence`) |
| **Transitions, Fades & Constant Retime** *(21.1+)* | **Full** | `timeline.transitions_and_fades` (`AddTransition`, `SetFades`), `timeline.set_speed` (`SetSpeed`, `Stabilize`, `SmartReframe`) |
| **Markers, VO Teleprompter & Fairlight ADR** | **Full** | `marker.*`, `timeline.vo_script` (`.csv` ADR + on-screen `ST1` `.srt` + live Web `🎬 timeline` VO HUD), `timeline.create_subtitles`, `timeline.import_srt` |
| **JS / CSS / SVG / Canvas Motion Overlays & QR** | **Full** | `motion.render_overlay`, `motion.qr_callout` (Headless Chrome/Edge CDP -> QuickTime PNG or ProRes 4444 Straight-Alpha `.mov` on `Video 2+`) |
| **Color LUTs, CDLs, DRX Stills & AI Auto-Color** *(21.2)* | **High** | `color.apply_lut`, `color.set_cdl`, `color.grade` (`GetCDL`, `ApplyAutoColor`, `ApplyGradeFromDRX`, `ResetAllGrades`) |
| **Fairlight Loudness, Voice Isolation & TTS** *(21.1+)* | **High** | `audio.enhance` (`NormalizeAudioLevel` `-14 LKFS`, `VoiceIsolation`, `DialogueLeveler`, `AnalyzeAudioLevels`, `GenerateSpeech`) + `media.master_vo` |
| **Fusion Compositions & Text+ Titles** | **Full** | `fusion.inspect_comp`, `fusion.set_tool_input`, `resolve.eval` |
| **Deliver Render & YouTube Staging** | **Full** | `render.*`, `youtube.thumbnail`, `youtube.stage`, `youtube.upload`, `youtube.update` |
| **DaVinci Resolve Free Edition** | **Supported** | `otio.build` (`.otio` / `.fcpxml`) + zero-dependency `BridgeTransport` |

## Explicit Vendor API Gaps & Workaround Decisions

Blackmagic Design's `fusionscript` API intentionally omits three low-level interactive UI surfaces across **all** third-party tools (including ResolveMCP / Voyager). Rather than brittle GUI mouse-clicking hacks, we record the following clean engineering decisions:

1. **Primary Color Wheels, Custom Curves & Power Windows:**
   - *Vendor state:* Not exposed on `Graph` or `TimelineItem`.
   - *Our decision:* Use `color.set_cdl` (Slope/Offset/Power/Saturation math), `color.apply_lut` (`.cube`), `color.grade` (`ApplyAutoColor` or `.drx` PowerGrade templates saved once from the Color page), which are 100% deterministic and version-controllable.
2. **Variable Keyframed Speed-Ramp Curves:**
   - *Vendor state:* `SetSpeed` sets constant clip speed % (with `RippleTimeline`), not bezier variable-speed keyframes.
   - *Our decision:* Split clips at ramp points with `timeline.append_clips` + `timeline.set_speed`, or render frame-exact eased animations in `motion.render_overlay`.
3. **Per-Band Fairlight Parametric EQ Curves:**
   - *Vendor state:* Individual EQ band frequencies/Q are not exposed on `TimelineItem`.
   - *Our decision:* Pre-master dialogue/VO deterministically via `media.master_vo` (FFmpeg 80Hz HPF + FFT denoise + compressor + `-14 LUFS` EBU R128 24-bit WAV) and apply Resolve's neural `VoiceIsolation` + `DialogueLeveler` via `audio.enhance`.