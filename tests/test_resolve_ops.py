"""Comprehensive tests for all `davag.resolve_ops` across MediaPool, Timeline, Markers,
Color/Fusion (`Text+` StyledText), Render queue, and `resolve.eval`.
"""

from __future__ import annotations

from pathlib import Path

from davag.transport import DirectTransport

from .fake_resolve import FakeResolve


async def test_full_editing_workflow_via_resolve_ops(tmp_path: Path) -> None:
    fake = FakeResolve(studio=True)
    tr = DirectTransport(resolve_obj=fake)

    try:
        # 1. MediaPool bin creation + import
        bin_res = await tr.call("mediapool.create_bin", {"bin_path": "Master/Interviews/CamA"})
        assert bin_res["name"] == "CamA"

        imp = await tr.call(
            "mediapool.import",
            {
                "paths": ["/footage/a_roll.mov", "/footage/b_roll.mov"],
                "bin_path": "Master/Interviews/CamA",
            },
        )
        assert imp["imported_count"] == 2
        clip_a_id = imp["clips"][0]["clip_id"]
        clip_b_id = imp["clips"][1]["clip_id"]

        tree = await tr.call("mediapool.tree")
        assert tree["root"]["subfolders"][0]["name"] == "Interviews"

        clips = await tr.call("mediapool.clips", {"recursive": True})
        assert clips["count"] == 2

        # 2. Append frame-accurate subclips onto the active timeline
        app = await tr.call(
            "timeline.append_clips",
            {
                "clips": [
                    {"clip_id": clip_a_id, "start_frame": 24, "end_frame": 124, "track_index": 1},
                    {"clip_id": clip_b_id, "start_frame": 0, "end_frame": 48, "track_index": 2},
                ]
            },
        )
        assert app["appended_count"] == 2
        item_a_id = app["items"][0]["item_id"]

        # 3. Inspect timeline state + set transform properties
        state = await tr.call("timeline.get_state", {"include_properties": True})
        assert state["total_items"] == 2
        assert len(state["tracks"]["video"]) == 2

        await tr.call(
            "timeline.set_clip_props",
            {"item_ids": [item_a_id], "properties": {"ZoomX": 1.15, "ZoomY": 1.15, "Pan": -20.0}},
        )
        state2 = await tr.call("timeline.get_state", {"include_properties": True})
        v1_item = state2["tracks"]["video"][0]["items"][0]
        assert v1_item["properties"]["ZoomX"] == 1.15

        # 4. Insert Fusion Text+ title & update its StyledText!
        title_res = await tr.call(
            "timeline.insert_title_or_generator", {"kind": "title", "name": "Text+"}
        )
        title_id = title_res["item"]["item_id"]

        comp_info = await tr.call("fusion.inspect_comp", {"item_id": title_id})
        assert comp_info["comp_count"] == 1
        assert comp_info["tools"][0]["styled_text"] == "Sample Title"

        await tr.call(
            "fusion.set_tool_input",
            {"item_id": title_id, "inputs": {"StyledText": "Episode 01 — Opening"}},
        )
        comp_after = await tr.call("fusion.inspect_comp", {"item_id": title_id})
        assert comp_after["tools"][0]["styled_text"] == "Episode 01 — Opening"

        # 5. Markers (verifies timeline start_frame offset normalization: 01:00:02:00 -> offset 48)
        m_added = await tr.call(
            "marker.add",
            {"tc": "01:00:02:00", "color": "Green", "name": "Beat 1", "note": "Cut B-roll here"},
        )
        assert m_added["frame_offset"] == 48
        assert m_added["tc"] == "01:00:02:00"

        m_list = await tr.call("marker.list")
        assert m_list["count"] == 1
        assert m_list["markers"][0]["name"] == "Beat 1"

        # 6. Color CDL + LUT + Still frame export
        await tr.call(
            "color.set_cdl",
            {"item_ids": [item_a_id], "slope": "1.05 1.0 0.95", "saturation": "1.1"},
        )
        still = await tr.call(
            "timeline.grab_still",
            {"tc": "01:00:01:00", "output_path": str(tmp_path / "viewer.png")},
        )
        assert Path(still["path"]).is_file()
        assert still["bytes"] > 0

        # 7. Render queue + start
        job = await tr.call(
            "render.queue_add",
            {
                "target_dir": str(tmp_path / "renders"),
                "custom_name": "rough_cut_v1",
                "preset_name": "YouTube 1080p",
            },
        )
        assert job["job_id"] == "job-1"
        r_done = await tr.call("render.start", {"wait": True})
        assert r_done["jobs"][0]["JobStatus"] == "Complete"

        # 8. `resolve.eval` escape hatch
        ev = await tr.call(
            "resolve.eval",
            {"code": "print('proj:', project.GetName())\nresult = timeline.GetTrackCount('video')"},
        )
        assert "proj: Demo Edit" in ev["stdout"]
        assert ev["result"] == 2
    finally:
        await tr.close()