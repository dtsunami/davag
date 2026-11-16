"""5-Tier Role-Based Tool Surfaces (`davag.agent.roles`).

Lets the operator scope what `davag` can see and execute — from a strictly read-only
`reviewer` up to an all-stage `master` editor:

  1. `reviewer` — Read-only Resolve + Media Analysis (`contact_sheet`, `analyze_clip`,
                  `transcribe`, `grab_still`, `fs.read`, `fs.list`). Zero write/mutate ops.
  2. `logger`   — Assistant Logger / Story Producer: `reviewer` + `marker.*`,
                  `timeline.vo_script`, `media.save_digest`, `media.generate_resolve_proxy`,
                  `mediapool.link_proxy`. Can log chapters, prep proxies & VO cues; cannot alter cuts.
  3. `editor`   — Rough & Fine Cut Editor: `logger` + non-destructive `timeline.*`,
                  `mediapool.create_bin`/`import`, `motion.render_overlay`, `audio.enhance`,
                  `media.master_vo`, `otio.build`, `fs.write`/`fs.replace`.
                  Blocks `destructive` ops, `render.start`, `youtube.upload`, `resolve.eval`, & `shell`.
  4. `finisher` — Color, Fusion, Deliver & YouTube Stager: `editor` + `color.*`, `fusion.*`,
                  `render.*`, `youtube.thumbnail`, `youtube.stage`. Still blocks `resolve.eval` & `shell`.
  5. `master`   — All-Stage Master Editor (default): full tool surface (`resolve.eval`, `shell`,
                  `youtube.upload`, and `destructive` ops gated by operator approval).
"""

from __future__ import annotations

from dataclasses import dataclass

ROLES: tuple[str, ...] = ("reviewer", "logger", "editor", "finisher", "master")


@dataclass(frozen=True)
class RoleSpec:
    name: str
    title: str
    summary: str


ROLE_SPECS: dict[str, RoleSpec] = {
    "reviewer": RoleSpec(
        "reviewer",
        "Read-Only Reviewer & QC",
        "Inspects timelines, markers, viewer frames, contact sheets, transcripts & Scout digests. Zero mutations.",
    ),
    "logger": RoleSpec(
        "logger",
        "Story Logger & VO Producer",
        "Reviewer + markers, VO teleprompter scripts, Clip Digests, and QSV proxy linking. Cannot alter cuts.",
    ),
    "editor": RoleSpec(
        "editor",
        "Timeline & Motion Overlay Editor",
        "Logger + timeline assembly/trimming, transitions, subtitles, JS/SVG motion overlays, and audio mastering.",
    ),
    "finisher": RoleSpec(
        "finisher",
        "Color, Fusion & YouTube Delivery Finisher",
        "Editor + Color LUT/CDL/AutoColor, Fusion titles, Deliver renders, Flux thumbnails, and YouTube staging.",
    ),
    "master": RoleSpec(
        "master",
        "All-Stage Master Editor",
        "Full capability surface including gated `resolve.eval`, `shell`, `youtube.upload`, and `destructive` ops.",
    ),
}

READ_ONLY_EXTRA_OPS = frozenset(
    {
        "fs.read",
        "fs.list",
        "media.probe",
        "media.contact_sheet",
        "media.scene_cuts",
        "media.detect_silence",
        "media.proxy_clip",
        "media.analyze_clip",
        "media.transcribe",
    }
)

LOGGER_MUTATE_OPS = frozenset(
    {
        "marker.add",
        "marker.delete",
        "timeline.playhead",
        "timeline.vo_script",
        "media.save_digest",
        "media.generate_resolve_proxy",
        "mediapool.link_proxy",
    }
)

EDITOR_MUTATE_OPS = LOGGER_MUTATE_OPS | frozenset(
    {
        "project.save",
        "mediapool.create_bin",
        "mediapool.import",
        "timeline.select",
        "timeline.create",
        "timeline.duplicate",
        "timeline.append_clips",
        "timeline.insert_title_or_generator",
        "timeline.set_clip_props",
        "timeline.set_clip_state",
        "timeline.import_timeline_file",
        "timeline.track_state",
        "timeline.create_subtitles",
        "timeline.import_srt",
        "timeline.trim_and_move",
        "timeline.transitions_and_fades",
        "timeline.set_speed",
        "audio.enhance",
        "media.master_vo",
        "motion.render_overlay",
        "motion.qr_callout",
        "otio.build",
        "fs.write",
        "fs.replace",
    }
)

FINISHER_MUTATE_OPS = EDITOR_MUTATE_OPS | frozenset(
    {
        "color.apply_lut",
        "color.set_cdl",
        "color.grade",
        "fusion.set_tool_input",
        "render.queue_add",
        "render.start",
        "render.status",
        "youtube.thumbnail",
        "youtube.stage",
    }
)


def is_op_allowed_for_role(role: str, op_name: str, resolve_tier: str | None = None) -> bool:
    """Return True if `op_name` (with optional `resolve_ops` tier) is permitted under `role`."""
    r = role.strip().lower() or "master"
    if r == "master":
        return True

    # Read-tier ops are permitted in every role (`reviewer` and above)
    if resolve_tier == "read" or op_name in READ_ONLY_EXTRA_OPS:
        return True

    if r == "reviewer":
        return False
    if r == "logger":
        return op_name in LOGGER_MUTATE_OPS
    if r == "editor":
        return op_name in EDITOR_MUTATE_OPS
    if r == "finisher":
        return op_name in FINISHER_MUTATE_OPS
    return False