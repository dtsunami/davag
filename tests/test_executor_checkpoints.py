"""Tests for two-phase batch planning, Resolve `.davag-snap` timeline checkpoints, `/undo`,
segregated approval gates, OTIO timeline generation, and FFmpeg silence/proxy tools.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from davag.agent.executor import ApprovalDecision, OpCall, execute_batch
from davag.agent.journal import Journal
from davag.agent.sandbox import Sandbox
from davag.transport import DirectTransport

from .fake_resolve import FakeResolve


async def test_batch_plan_rejects_atomically_before_any_mutation(tmp_path: Path) -> None:
    fake = FakeResolve()
    tr = DirectTransport(resolve_obj=fake)
    sandbox = Sandbox(tmp_path)
    journal = Journal(tmp_path / "session")

    # Op 0 is valid, Op 1 escapes sandbox -> Op 0 must NOT execute!
    outcome = await execute_batch(
        [
            OpCall(0, "fs.write", {"path": "notes.md", "text": "hello"}),
            OpCall(1, "fs.read", {"path": "../outside_secret.txt"}),
        ],
        sandbox=sandbox,
        journal=journal,
        transport=tr,
        cache_dir=tmp_path / "cache",
    )
    assert outcome.status == "plan-failed"
    assert not (tmp_path / "notes.md").exists()
    await tr.close()


async def test_resolve_checkpoint_and_undo_restores_timeline(tmp_path: Path) -> None:
    fake = FakeResolve()
    tr = DirectTransport(resolve_obj=fake)
    sandbox = Sandbox(tmp_path)
    journal = Journal(tmp_path / "session")

    # Seed one clip in MediaPool
    imp = await tr.call("mediapool.import", {"paths": ["/clips/intro.mp4"]})
    clip_id = imp["clips"][0]["clip_id"]

    # Mutating batch appends clip -> executor automatically creates a `.davag-snap-*` checkpoint!
    outcome = await execute_batch(
        [OpCall(0, "timeline.append_clips", {"clips": [{"clip_id": clip_id}]})],
        sandbox=sandbox,
        journal=journal,
        transport=tr,
        cache_dir=tmp_path / "cache",
    )
    assert outcome.status == "ok"
    assert outcome.checkpoint is not None
    assert outcome.checkpoint.snapshot_name.startswith(".davag-snap-")

    state_after = await tr.call("timeline.get_state")
    assert state_after["total_items"] == 1

    # Now `/undo` restores the pre-batch empty timeline under its original name "Main Cut"!
    undo_res = await journal.undo_last_resolve(tr)
    assert undo_res["restored_timeline"] == "Main Cut"

    state_restored = await tr.call("timeline.get_state")
    assert state_restored["name"] == "Main Cut"
    assert state_restored["total_items"] == 0
    await tr.close()


async def test_gated_ops_segregated_and_require_approval(tmp_path: Path) -> None:
    fake = FakeResolve()
    tr = DirectTransport(resolve_obj=fake)
    sandbox = Sandbox(tmp_path)
    journal = Journal(tmp_path / "session")

    prompted_ops: list[str] = []

    async def deny_eval(call: OpCall) -> ApprovalDecision:
        prompted_ops.append(call.op)
        return ApprovalDecision(allow=False, reason="nope")

    # Safe fs.write commits in Pass 1; gated `resolve.eval` is denied in Pass 2
    outcome = await execute_batch(
        [
            OpCall(0, "fs.write", {"path": "safe.txt", "text": "committed"}),
            OpCall(1, "resolve.eval", {"code": "result = 42"}),
        ],
        sandbox=sandbox,
        journal=journal,
        transport=tr,
        cache_dir=tmp_path / "cache",
        yolo=False,
        approve=deny_eval,
    )
    assert outcome.status == "denied"
    assert prompted_ops == ["resolve.eval"]
    assert (tmp_path / "safe.txt").read_text(encoding="utf-8") == "committed"
    await tr.close()


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
async def test_ffmpeg_silence_detection_proxy_qsv_and_otio_build(tmp_path: Path) -> None:
    """Generates a 3-second synthetic clip (1s tone, 1s silence, 1s tone), runs FFprobe,
    silence->speech frame detection, hardware/QSV proxy transcode, and OTIO export.
    """
    ffmpeg = shutil.which("ffmpeg")
    assert ffmpeg is not None
    sample_mp4 = tmp_path / "test_speech.mp4"

    import asyncio

    # 3s video @ 24fps: audio has tone on [0..1s], silence on [1..2s], tone on [2..3s]
    await asyncio.to_thread(
        subprocess.run,
        [
            ffmpeg,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=640x360:rate=24:duration=3",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=3",
            "-af",
            "volume=enable='between(t,1.0,2.0)':volume=0",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(sample_mp4),
        ],
        check=True,
    )

    sandbox = Sandbox(tmp_path)
    journal = Journal(tmp_path / "session")

    outcome = await execute_batch(
        [
            OpCall(0, "media.probe", {"path": "test_speech.mp4"}),
            OpCall(
                1,
                "media.detect_silence",
                {"path": "test_speech.mp4", "min_silence_sec": 0.4, "pad_sec": 0.05},
            ),
            OpCall(
                2,
                "media.proxy_clip",
                {"path": "test_speech.mp4", "output_path": "proxy.mp4", "prefer_av1": True},
            ),
            OpCall(
                3,
                "otio.build",
                {
                    "timeline_name": "Auto Rough Cut",
                    "fps": 24.0,
                    "output_path": "rough_cut.otio",
                    "video_segments": [
                        {"name": "Part 1", "media_path": "test_speech.mp4", "start_frame": 0, "duration_frames": 24},
                        {"name": "Part 2", "media_path": "test_speech.mp4", "start_frame": 48, "duration_frames": 24},
                    ],
                },
            ),
        ],
        sandbox=sandbox,
        journal=journal,
        transport=None,
        cache_dir=tmp_path / "cache",
    )

    assert outcome.status == "ok", outcome.errors
    probe_res = outcome.results[0]["result"]
    assert probe_res["video"]["width"] == 640
    assert abs(probe_res["duration_sec"] - 3.0) < 0.2

    speech_res = outcome.results[1]["result"]
    assert speech_res["silence_count"] == 1
    assert len(speech_res["speech_segments"]) == 2

    proxy_res = outcome.results[2]["result"]
    assert (tmp_path / "proxy.mp4").is_file()
    assert proxy_res["bytes"] > 0
    assert len(outcome.attached_media) == 1
    assert outcome.attached_media[0]["kind"] == "video"

    otio_res = outcome.results[3]["result"]
    assert (tmp_path / "rough_cut.otio").is_file()
    assert otio_res["clip_count"] == 2

    # Also test Contact Sheet + Resolve Proxy Auto-Link + Digest Grounding on an outside-cwd Media Pool clip!
    fake = FakeResolve()
    tr = DirectTransport(resolve_obj=fake)
    outside_dir = tmp_path.parent / f"{tmp_path.name}_external_media"
    outside_dir.mkdir(exist_ok=True)
    ext_clip = outside_dir / "cam_external.mp4"
    shutil.copy2(sample_mp4, ext_clip)

    try:
        imp = await tr.call("mediapool.import", {"paths": [str(ext_clip)]})
        cid = imp["clips"][0]["clip_id"]

        out2 = await execute_batch(
            [
                OpCall(0, "media.contact_sheet", {"clip_id": cid, "cols": 2, "rows": 2}),
                OpCall(1, "media.scene_cuts", {"clip_id": cid}),
                OpCall(2, "media.generate_resolve_proxy", {"clip_id": cid, "height": 360}),
                OpCall(3, "media.save_digest", {"clip_id": cid, "summary": "Test bars with 1s tone break."}),
                OpCall(4, "timeline.track_state", {"track_type": "video", "add_track": True, "name": "B-Roll", "locked": True}),
                OpCall(5, "timeline.create_subtitles", {"max_chars": 42}),
            ],
            sandbox=sandbox,
            journal=journal,
            transport=tr,
            cache_dir=tmp_path / "cache",
        )
        assert out2.status == "ok", out2.errors
        assert Path(out2.results[0]["result"]["path"]).is_file()
        assert out2.results[2]["result"]["linked_in_resolve"] is True
        assert out2.results[4]["result"]["locked"] is True
        assert out2.results[5]["result"]["created"] is True

        # Verify snapshot_resolve_grounding automatically includes the saved [Cached Digest] & proxy=linked!
        from davag.agent.grounding import snapshot_resolve_grounding

        snap_txt = await snapshot_resolve_grounding(tr, cache_dir=tmp_path / "cache")
        assert "proxy=linked" in snap_txt
        assert "Test bars with 1s tone break." in snap_txt
    finally:
        await tr.close()
        shutil.rmtree(outside_dir, ignore_errors=True)