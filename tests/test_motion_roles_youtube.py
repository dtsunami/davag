"""Tests for 5-tier Capability Roles, deterministic HTML/SVG/JS motion overlays, YouTube staging, and `.davagignore`."""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from davag.agent.executor import plan_batch
from davag.agent.parser import OpCall
from davag.agent.prompt import _build_protocol_default
from davag.agent.sandbox import Sandbox
from davag.media_intel.ffmpeg import probe_media
from davag.media_intel.motion import find_headless_browser, render_html_svg_overlay
from davag.youtube.studio import _extract_image_from_openrouter_payload, format_youtube_chapters


def test_quoted_at_refs_still_contact_sheet_and_zero_byte_guard(tmp_path: Path) -> None:
    import shutil
    import subprocess

    from davag.context.media import extract_image_refs, extract_video_refs, load_image_part
    from davag.media_intel.ffmpeg import generate_contact_sheet

    ext_dir = tmp_path / "external folder with spaces"
    ext_dir.mkdir()
    ws_dir = tmp_path / "workspace"
    ws_dir.mkdir()
    sb = Sandbox(ws_dir)

    ffmpeg = shutil.which("ffmpeg")
    assert ffmpeg is not None
    still_png = ext_dir / "thumb 2a.png"
    subprocess.run(
        [ffmpeg, "-y", "-f", "lavfi", "-i", "color=c=navy:s=1280x720:d=0.04", "-frames:v", "1", str(still_png)],
        check=True,
        capture_output=True,
    )
    vid_mp4 = ext_dir / "2026-11-13 11-49-02.mp4"
    subprocess.run(
        [ffmpeg, "-y", "-f", "lavfi", "-i", "testsrc=size=320x180:rate=15:duration=1.0", "-c:v", "libx264", str(vid_mp4)],
        check=True,
        capture_output=True,
    )

    # 1. Quoted `@"..."` outside `cwd` is matched AND allowlisted on Sandbox!
    prompt = f'Integrate @"{still_png}" and @"{vid_mp4}"'
    txt1, imgs = extract_image_refs(prompt, sb)
    txt2, vids, _ = extract_video_refs(txt1, sb)
    assert len(imgs) == 1 and len(vids) == 1
    assert "[attached image:" in txt2 and "[video:" in txt2
    assert sb.resolve_for_read(vid_mp4) == vid_mp4.resolve()

    # 2. `generate_contact_sheet` on a still `.png` produces non-empty image (never 0 bytes!)
    sheet = generate_contact_sheet(still_png, cols=1, rows=1)
    assert sheet["bytes"] > 100
    loaded = load_image_part(sheet["path"])
    assert loaded["bytes"] > 100

    # 3. `load_image_part` rejects 0-byte files cleanly before they ever reach Gemini!
    empty_png = ws_dir / "empty.png"
    empty_png.write_bytes(b"")
    with pytest.raises(ValueError, match="0 bytes"):
        load_image_part(empty_png)

    # 4. Verify `Ledger` + empty-image eviction uses `seg.part` cleanly
    from davag.context.ledger import Ledger

    led = Ledger()
    led.add(seg_type="media", label="thumb", role="user", image=loaded)
    bad_seg = led.add(seg_type="media", label="empty", role="user", image={"media_type": "image/png", "data": "", "bytes": 0, "tokens": 1})
    for seg in list(led.segments):
        if seg.part.get("type") == "image" and not seg.part.get("data"):
            led.drop(seg.id)
    assert len(led.segments) == 1
    assert led.segments[0].id != bad_seg.id

    from davag.providers.base import ProviderError

    pe = ProviderError("Gemini stream timed out", provider="gemini", status=408, retryable=True)
    assert pe.retryable and pe.status == 408


def test_davagignore_and_daignore_exclude_grounding_clutter(tmp_path: Path) -> None:
    (tmp_path / ".daignore").write_text("davai/\n", encoding="utf-8")
    (tmp_path / ".davagignore").write_text("tests/\n", encoding="utf-8")
    (tmp_path / "davai").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "edits").mkdir()

    sb = Sandbox(tmp_path)
    assert sb.is_ignored(tmp_path / "davai" / "foo.js")
    assert sb.is_ignored(tmp_path / "tests" / "test_a.py")
    assert not sb.is_ignored(tmp_path / "edits" / "cut.otio")


def test_five_tier_role_surfaces_and_plan_batch_enforcement(tmp_path: Path) -> None:
    sb = Sandbox(tmp_path)

    # 1. `reviewer` allows reads & analysis, blocks markers & timeline edits
    err_rev_ok = plan_batch(
        [OpCall(index=0, op="timeline.get_state", params={})],
        sandbox=sb,
        has_transport=True,
        role="reviewer",
    )
    assert err_rev_ok == []

    err_rev_block = plan_batch(
        [OpCall(index=0, op="marker.add", params={"tc": "01:00:00:00", "name": "x"})],
        sandbox=sb,
        has_transport=True,
        role="reviewer",
    )
    assert len(err_rev_block) == 1
    assert 'not permitted under active role "reviewer"' in err_rev_block[0]["message"]

    # 2. `logger` allows `marker.add` and `timeline.vo_script`, blocks `timeline.create`
    assert (
        plan_batch(
            [OpCall(index=0, op="marker.add", params={"tc": "01:00:00:00", "name": "x"})],
            sandbox=sb,
            has_transport=True,
            role="logger",
        )
        == []
    )
    assert len(
        plan_batch(
            [OpCall(index=0, op="timeline.create", params={"name": "Cut"})],
            sandbox=sb,
            has_transport=True,
            role="logger",
        )
    ) == 1

    # 3. `editor` allows `timeline.create`, `timeline.select` & `motion.render_overlay`, blocks `render.start` & `shell`
    assert (
        plan_batch(
            [
                OpCall(index=0, op="timeline.select", params={"name": "Cut"}),
                OpCall(index=1, op="motion.render_overlay", params={"name": "ov", "html": "<svg></svg>"}),
            ],
            sandbox=sb,
            has_transport=True,
            role="editor",
        )
        == []
    )
    assert len(
        plan_batch(
            [OpCall(index=0, op="render.start", params={})],
            sandbox=sb,
            has_transport=True,
            role="editor",
        )
    ) == 1

    # 4. System prompt protocol dynamically strips disallowed ops for `reviewer`
    rev_proto = _build_protocol_default("reviewer")
    assert "`timeline.get_state`" in rev_proto
    assert "`timeline.delete_clips`" not in rev_proto
    assert "`shell`" not in rev_proto


@pytest.mark.asyncio
async def test_youtube_stage_appends_thumbnail_end_screen_by_default(tmp_path: Path) -> None:
    import asyncio
    import shutil
    import subprocess

    from davag.transport import DirectTransport
    from davag.youtube.studio import stage_youtube_package

    from .fake_resolve import FakeResolve

    ffmpeg = shutil.which("ffmpeg")
    assert ffmpeg is not None
    thumb = tmp_path / "fable_tn_2a.png"
    await asyncio.to_thread(
        subprocess.run,
        [ffmpeg, "-y", "-f", "lavfi", "-i", "color=c=darkred:s=1280x720:d=0.04", "-frames:v", "1", str(thumb)],
        check=True,
        capture_output=True,
    )

    fake = FakeResolve()
    transport = DirectTransport(resolve_obj=fake)
    await transport.call("mediapool.import", {"paths": [str(thumb)]})
    await transport.call("timeline.create", {"name": "Main Edit"})
    clips = (await transport.call("mediapool.clips"))["clips"]
    await transport.call(
        "timeline.append_clips",
        {"clips": [{"clip_id": clips[0]["clip_id"], "start_frame": 0, "end_frame": 120, "track_index": 1}]},
    )

    pkg1 = await stage_youtube_package(
        transport=transport,
        staging_root=tmp_path / "youtube_staging",
        title="My Video",
        thumbnail_path=str(thumb),
        end_screen_sec=2.0,
    )
    assert pkg1["end_screen"] is not None
    assert Path(pkg1["end_screen"]["path"]).is_file()
    st1 = await transport.call("timeline.get_state")
    v1_names_1 = [it["name"] for it in st1["tracks"]["video"][0]["items"]]
    assert any(n.endswith("_end_screen.mp4") for n in v1_names_1)
    count_after_first = len(v1_names_1)

    # Re-running `stage_youtube_package` replaces the prior end-screen item idempotently (no duplicate slates!)
    pkg2 = await stage_youtube_package(
        transport=transport,
        staging_root=tmp_path / "youtube_staging",
        title="My Video",
        thumbnail_path=str(thumb),
        end_screen_sec=2.0,
    )
    assert pkg2["end_screen"] is not None
    st2 = await transport.call("timeline.get_state")
    assert len(st2["tracks"]["video"][0]["items"]) == count_after_first


@pytest.mark.asyncio
async def test_ephemeral_live_timeline_state_and_extend_end_frames() -> None:
    from davag.agent.grounding import format_live_timeline_state
    from davag.transport import DirectTransport

    from .fake_resolve import FakeResolve

    fake = FakeResolve(version="21.2.0")
    tr = DirectTransport(resolve_obj=fake)
    await tr.call("mediapool.import", {"paths": ["clip_a.mp4"]})
    await tr.call("timeline.create", {"name": "Live State Cut"})
    clips = (await tr.call("mediapool.clips"))["clips"]
    app = await tr.call(
        "timeline.append_clips",
        {"clips": [{"clip_id": clips[0]["clip_id"], "start_frame": 10, "end_frame": 110, "track_index": 1}]},
    )
    item_id = app["items"][0]["item_id"]
    await tr.call("timeline.set_clip_props", {"item_ids": [item_id], "properties": {"ZoomX": 0.25, "Pan": -400.0}})

    live_txt = await format_live_timeline_state(tr)
    assert live_txt is not None
    assert "[Live Timeline State" in live_txt
    assert f"id={item_id}" in live_txt
    assert '"ZoomX":0.25' in live_txt
    assert '"AnchorPointX"' not in live_txt  # Default properties stripped!


@pytest.mark.asyncio
async def test_qr_svg_purple_link_markers_and_overlays_gui_endpoints(tmp_path: Path) -> None:
    import aiohttp

    from davag.config.env import load_config
    from davag.server.http import start_web_server
    from davag.session.bootstrap import bootstrap_session
    from davag.transport import DirectTransport
    from davag.youtube.qr import build_qr_callout_html, encode_qr_matrix, generate_qr_svg
    from davag.youtube.studio import stage_youtube_package

    from .fake_resolve import FakeResolve

    # 1. Pure-Python ISO/IEC 18004 QR matrix & SVG verification
    url = "https://github.com/example/labjs-notebooks#nyquist"
    mat = encode_qr_matrix(url)
    n = len(mat)
    assert n >= 29 and len(mat[0]) == n
    for r0, c0 in ((0, 0), (0, n - 7), (n - 7, 0)):
        assert all(mat[r0][c0 + k] == 1 for k in range(7))
        assert all(mat[r0 + 6][c0 + k] == 1 for k in range(7))
        assert mat[r0 + 3][c0 + 3] == 1
    svg = generate_qr_svg(url)
    assert svg.startswith("<svg ") and "<path d=\"M" in svg
    callout_html = build_qr_callout_html(title="Nyquist Notebook", url=url)
    assert "window.renderFrame" in callout_html and "github.com/example/labjs-notebooks#nyquist" in callout_html

    # 2. Purple Link Marker -> Line-1 Above-the-Fold Description + Topics & Links + COMMENT.md + Chapter separation
    fake = FakeResolve(version="21.2.0")
    tr = DirectTransport(resolve_obj=fake)
    await tr.call("mediapool.import", {"paths": ["screen.mp4"]})
    await tr.call("timeline.create", {"name": "LabJS Demo"})
    clips = (await tr.call("mediapool.clips"))["clips"]
    await tr.call(
        "timeline.append_clips",
        {"clips": [{"clip_id": clips[0]["clip_id"], "start_frame": 0, "end_frame": 200, "track_index": 1}]},
    )
    await tr.call("marker.add", {"tc": "01:00:00:00", "color": "Cyan", "name": "Intro"})
    await tr.call("marker.add", {"tc": "01:00:03:00", "color": "Purple", "name": "Nyquist Interactive Demo", "note": url})
    await tr.call("marker.add", {"tc": "01:00:12:00", "color": "Cyan", "name": "Sum-Check Protocol"})

    pkg = await stage_youtube_package(
        transport=tr,
        staging_root=tmp_path / "youtube_staging",
        title="LabJS Deep Dive",
        description_intro="Exploring interactive math notebooks.",
        render_qr_markers=False,
        append_end_screen=False,
    )
    assert pkg["description"].startswith(f"Interactive Code & Resources: {url}")
    assert "Topics & Links:\n00:03 — Nyquist Interactive Demo:" in pkg["description"]
    assert pkg["chapters"] == ["00:00 Intro", "00:12 Sum-Check Protocol"]
    assert pkg["first_comment"] and url in pkg["first_comment"]
    assert (Path(pkg["staging_dir"]) / "COMMENT.md").is_file()

    # 3. Verify Web GUI `/api/overlays`, `/api/overlays/qr-preview`, and `/api/docs` (including new 00 guide!)
    _ = load_config(cwd=tmp_path, home=tmp_path / "home")
    sess = await bootstrap_session(cwd=tmp_path, injected_transport=tr, auto_connect_resolve=False)
    srv = await start_web_server(session=sess, submit_fn=lambda _t, _o: None, host="127.0.0.1", port=0)
    try:
        hdrs = {"Authorization": f"Bearer {srv.token}"}
        async with aiohttp.ClientSession() as http:
            async with http.get(f"{srv.url}/api/overlays", headers=hdrs) as r_ov:
                assert r_ov.status == 200
                ov_data = await r_ov.json()
                assert len(ov_data["link_markers"]) == 1
                assert ov_data["link_markers"][0]["url"] == url
                assert len(ov_data["youtube_packages"]) == 1

            async with http.get(f"{srv.url}/api/overlays/qr-preview?url={url}&title=Demo", headers=hdrs) as r_qr:
                assert r_qr.status == 200
                qr_data = await r_qr.json()
                assert qr_data["ok"] and "<svg " in qr_data["svg"]

            async with http.get(f"{srv.url}/api/docs", headers=hdrs) as r_docs:
                assert r_docs.status == 200
                docs_data = await r_docs.json()
                doc_ids = [d["id"] for d in docs_data["docs"]]
                assert "00_resolve_to_youtube_and_qr_guide.md" in doc_ids

            # 4. Verify `/api/overlays/adjust` moves Purple marker & `/api/overlays/thumb?live=1` serves JPEG!
            async with http.post(
                f"{srv.url}/api/overlays/adjust",
                headers=hdrs,
                json={"action": "move_marker", "old_frame": 86472, "new_tc": "01:00:05:00", "color": "Purple", "name": "Moved QR", "note": url},
            ) as r_adj:
                assert r_adj.status == 200
                assert (await r_adj.json())["ok"] is True

            async with http.post(f"{srv.url}/api/overlays/adjust", headers=hdrs, json={"action": "set_clip_color", "item_id": (await tr.call('timeline.get_state'))['tracks']['video'][0]['items'][0]['item_id'], "color": "Orange"}) as r_cc:
                assert r_cc.status == 200

            import asyncio
            import subprocess
            ov_dir = tmp_path / "overlays"
            ov_dir.mkdir(exist_ok=True)
            await asyncio.to_thread(subprocess.run, ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=red:s=320x180:d=0.4", str(ov_dir / "test_ov.mov")], check=True)
            async with http.get(f"{srv.url}/api/overlays/render-video/test_ov.mov?frame=86472&token={srv.token}") as r_rv:
                assert r_rv.status == 200
                assert len(await r_rv.read()) > 1000

            async with http.get(f"{srv.url}/api/overlays/thumb?frame=86520&w=1600&live=1&token={srv.token}") as r_th:
                assert r_th.status == 200
                th_bytes = await r_th.read()
                assert th_bytes.startswith(b"\xff\xd8")
    finally:
        await srv.close()
        await sess.close()


def test_youtube_chapters_and_openrouter_image_extraction() -> None:
    markers = [
        {"frame": 108000, "name": "01. Fable 5.2 Intro"},
        {"frame": 108150, "name": "Too Close (<10s)"},  # 5s later -> skipped per YouTube >=10s rule!
        {"frame": 109319, "name": "02. LabJS Intro"},
        {"frame": 110617, "name": "03. Fable Prompt"},
    ]
    ch = format_youtube_chapters(markers, tl_start_frame=108000, fps=30.0)
    assert ch == [
        "00:00 Fable 5.2 Intro",
        "00:44 LabJS Intro",
        "01:27 Fable Prompt",
    ]

    fake_png = b"\x89PNG\r\n\x1a\nfake"
    b64 = base64.b64encode(fake_png).decode("ascii")
    or_payload = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "images": [{"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}}],
                }
            }
        ]
    }
    raw_bytes, remote_u = _extract_image_from_openrouter_payload(or_payload)
    assert raw_bytes == fake_png
    assert remote_u is None


@pytest.mark.asyncio
async def test_deterministic_html_svg_overlay_renders_quicktime_png_and_prores_alpha(tmp_path: Path) -> None:
    try:
        find_headless_browser()
    except RuntimeError:
        pytest.skip("No Chrome/Edge browser installed")

    html = """
    <svg viewBox="0 0 640 360" xmlns="http://www.w3.org/2000/svg">
      <rect id="bar" x="40" y="260" width="0" height="48" rx="10" fill="#ff6b35" />
      <text id="lbl" x="40" y="240" fill="#ffffff" font-size="32" font-family="sans-serif">0%</text>
    </svg>
    <script>
      window.renderFrame = (f, t, dur) => {
        const p = Math.min(1, t / dur);
        document.getElementById('bar').setAttribute('width', String(Math.round(560 * p)));
        document.getElementById('lbl').textContent = Math.round(p * 100) + '%';
      };
    </script>
    """
    out_png_mov = tmp_path / "overlay_png.mov"
    res_png = await render_html_svg_overlay(
        html_or_svg=html,
        output_path=out_png_mov,
        width=640,
        height=360,
        fps=15.0,
        duration_sec=0.4,
    )
    assert res_png["format"] == "mov_png_rgba"
    assert res_png["frames"] == 6
    assert out_png_mov.is_file() and out_png_mov.stat().st_size > 500

    import importlib.util
    import shutil
    import struct

    if importlib.util.find_spec("resvg_py") and shutil.which("node"):
        assert res_png["backend"] == "native_v8_resvg"

    mov_bytes = out_png_mov.read_bytes()
    png_entry_offset = mov_bytes.index(b"png ") - 4
    assert struct.unpack_from(">H", mov_bytes, png_entry_offset + 82)[0] == 32

    info_png = probe_media(out_png_mov)
    assert info_png["video"]["codec"] == "png"
    assert info_png["video"]["width"] == 640
    assert info_png["video"]["height"] == 360

    out_prores = tmp_path / "overlay_prores.mov"
    res_prores = await render_html_svg_overlay(
        html_or_svg=html,
        output_path=out_prores,
        width=640,
        height=360,
        fps=15.0,
        duration_sec=0.4,
        codec="prores",
    )
    assert res_prores["format"] == "prores_4444_alpha"
    info_prores = probe_media(out_prores)
    assert info_prores["video"]["codec"] == "prores"
    assert res_png["bytes"] < res_prores["bytes"]

    # Also verify QuickTime RLE (`qtrle`) + Straight-Alpha `stsd` depth=32 + dynamic `auto_clip` HTML card!
    card_html = """
    <div id="card" style="position:absolute; right:48px; bottom:48px; padding:16px 24px; background:rgba(15,20,32,0.92); border-radius:10px; box-shadow:0 8px 24px rgba(0,0,0,0.5); color:#fff;">
      <span id="txt">Frame 0</span>
    </div>
    <script>
      window.renderFrame = (f) => { document.getElementById('txt').textContent = 'Frame ' + f; };
    </script>
    """
    out_qtrle = tmp_path / "overlay_qtrle.mov"
    res_qtrle = await render_html_svg_overlay(
        html_or_svg=card_html,
        output_path=out_qtrle,
        width=640,
        height=360,
        fps=15.0,
        duration_sec=0.6,
        codec="qtrle",
        backend="cdp",
    )
    assert res_qtrle["format"] == "mov_qtrle_rgba"
    assert res_qtrle["backend"] == "cdp_parallel"
    assert res_qtrle["clip"] is not None  # Sparse card triggered `<4ms` dynamic auto_clip!
    assert res_qtrle["clip"]["width"] < 640 and res_qtrle["clip"]["height"] < 360
    info_qtrle = probe_media(out_qtrle)
    assert info_qtrle["video"]["width"] == 640 and info_qtrle["video"]["height"] == 360

    # Verify `native_node_canvas` fast path when `@napi-rs/canvas` is installed
    from davag.media_intel.motion import probe_motion_capabilities

    m_caps = probe_motion_capabilities()
    if m_caps["node_canvas_module"]:
        canvas_html = """<canvas id="c"></canvas>
        <script>
          window.renderFrame = (f, t, dur) => {
            const c = document.getElementById('c');
            const ctx = c.getContext('2d');
            ctx.clearRect(0, 0, c.width, c.height);
            ctx.fillStyle = '#4cc9f0';
            ctx.fillRect(20, 20, 40 + f * 30, 60);
          };
        </script>"""
        out_canvas = tmp_path / "overlay_canvas.mov"
        res_canvas = await render_html_svg_overlay(
            html_or_svg=canvas_html,
            output_path=out_canvas,
            width=320,
            height=180,
            fps=15.0,
            duration_sec=0.3,
            backend="native",
        )
        assert res_canvas["backend"] == "native_node_canvas"
        assert res_canvas["format"] == "mov_png_rgba"


@pytest.mark.asyncio
async def test_stsd_depth32_patcher_mediapool_alpha_mode_and_batch_prerender(tmp_path: Path) -> None:
    import struct

    from davag.agent.executor import _prerender_batch_overlays, execute_batch
    from davag.agent.journal import Journal
    from davag.media_intel.motion import _classify_overlay_source, _ensure_mov_png_rgba_depth32
    from davag.resolve_ops import OpError
    from davag.transport import DirectTransport

    from .fake_resolve import FakeResolve

    # 1. Source classifier check
    assert _classify_overlay_source("<svg viewBox='0 0 100 100'><rect/></svg><script>window.renderFrame=()=>{}</script>")[0] == "svg"
    assert _classify_overlay_source("<canvas id='c'></canvas><script>window.renderFrame=()=>{}</script>")[0] == "canvas"
    assert _classify_overlay_source("<div>card</div><script>window.renderFrame=()=>{}</script>")[0] == "html"

    # 2. Synthetic QuickTime `moov -> trak -> mdia -> minf -> stbl -> stsd -> png ` with depth=24 -> patched to 32!
    sample_entry = bytearray(86)
    struct.pack_into(">I", sample_entry, 0, 86)
    sample_entry[4:8] = b"png "
    struct.pack_into(">H", sample_entry, 82, 24)  # FFmpeg RGB24 depth bug
    struct.pack_into(">h", sample_entry, 84, 0)

    def _box(btype: bytes, payload: bytes) -> bytes:
        return struct.pack(">I4s", 8 + len(payload), btype) + payload

    stsd = _box(b"stsd", struct.pack(">II", 0, 1) + bytes(sample_entry))
    moov = _box(b"moov", _box(b"trak", _box(b"mdia", _box(b"minf", _box(b"stbl", stsd)))))
    fake_mov = tmp_path / "synthetic_rgb24.mov"
    fake_mov.write_bytes(_box(b"ftyp", b"qt  \x00\x00\x02\x00qt  ") + _box(b"mdat", b"\x00" * 16) + moov)

    assert _ensure_mov_png_rgba_depth32(fake_mov) is True
    assert _ensure_mov_png_rgba_depth32(fake_mov) is False  # Already 32-bit on second pass!
    raw_after = fake_mov.read_bytes()
    png_idx = raw_after.index(b"png ") - 4
    assert struct.unpack_from(">H", raw_after, png_idx + 82)[0] == 32
    assert struct.unpack_from(">h", raw_after, png_idx + 84)[0] == -1

    # 3. `mediapool.import` sets `SetClipProperty("Alpha mode", "Straight")` and rejects bad values
    fake = FakeResolve()
    tr = DirectTransport(resolve_obj=fake)
    imp = await tr.call(
        "mediapool.import",
        {"paths": [str(fake_mov)], "bin_path": "Overlays", "alpha_mode": "Straight"},
    )
    assert imp["alpha_mode"] == "Straight"
    assert imp["clips"][0]["alpha_mode"] == "Straight"

    with pytest.raises(OpError, match="Invalid alpha_mode"):
        await tr.call("mediapool.import", {"paths": [str(fake_mov)], "alpha_mode": "Bogus"})

    # 4. Concurrent batch pre-rendering (`_prerender_batch_overlays`) + Resolve Straight-Alpha placement
    try:
        find_headless_browser()
    except RuntimeError:
        return

    await tr.call("timeline.create", {"name": "Overlay Batch Cut"})
    await tr.call("mediapool.import", {"paths": ["base.mp4"]})
    base_cid = (await tr.call("mediapool.clips"))["clips"][-1]["clip_id"]
    await tr.call("timeline.append_clips", {"clips": [{"clip_id": base_cid, "start_frame": 0, "end_frame": 200, "track_index": 1}]})

    batch_ops = [
        OpCall(
            index=0,
            op="motion.render_overlay",
            params={"name": "ov_one", "static": True, "width": 320, "height": 180, "record_tc": "01:00:01:00", "html": "<svg viewBox='0 0 320 180'><circle cx='90' cy='90' r='40' fill='#4cc9f0'/></svg>"},
        ),
        OpCall(
            index=1,
            op="motion.render_overlay",
            params={"name": "ov_two", "static": True, "width": 320, "height": 180, "record_tc": "01:00:04:00", "html": "<svg viewBox='0 0 320 180'><rect x='40' y='40' width='120' height='80' fill='#f72585'/></svg>"},
        ),
    ]
    sb = Sandbox(tmp_path)
    prerendered = await _prerender_batch_overlays(batch_ops, sandbox=sb, transport=tr)
    assert set(prerendered.keys()) == {0, 1}

    outcome = await execute_batch(
        batch_ops,
        sandbox=sb,
        journal=Journal(tmp_path / "journal"),
        transport=tr,
        cache_dir=tmp_path / "cache",
    )
    assert outcome.status == "ok"
    assert outcome.results[0]["result"]["inserted_ok"] is True
    assert outcome.results[1]["result"]["inserted_ok"] is True