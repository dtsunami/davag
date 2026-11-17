"""In-memory mock of DaVinci Resolve's C++ scripting object graph (`Resolve`, `ProjectManager`,
`Project`, `MediaPool`, `Folder`, `MediaPoolItem`, `Timeline`, `TimelineItem`, `Fusion`).

Intentionally reproduces Resolve's real API quirks (1-indexed tracks, Lua `{1: x, 2: y}` dict
returns on selected methods, PascalCase method names, and frame/timecode conventions) so tests
exercise the exact code paths used against Resolve.exe.
"""

from __future__ import annotations

import copy
import uuid
from dataclasses import dataclass, field
from typing import Any


@dataclass
class FakeMediaPoolItem:
    name: str
    file_path: str = ""
    frames: int = 240
    fps: str = "24.000"
    resolution: str = "1920x1080"
    audio_channels: int = 2
    clip_color: str = ""
    unique_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    markers: dict[int, dict[str, Any]] = field(default_factory=dict)

    def GetName(self) -> str:
        return self.name

    def GetUniqueId(self) -> str:
        return self.unique_id

    def GetClipProperty(self, key: str | None = None) -> Any:
        extra = getattr(self, "_extra_clip_props", {})
        props = {
            "Clip Name": self.name,
            "File Path": self.file_path,
            "Frames": str(self.frames),
            "FPS": self.fps,
            "Resolution": self.resolution,
            "Audio Ch": str(self.audio_channels),
            "Proxy": "Full" if getattr(self, "proxy_media_path", "") else "None",
            "Proxy Media Path": getattr(self, "proxy_media_path", ""),
            "Start": "0",
            "End": str(max(0, self.frames - 1)),
            **extra,
        }
        if key is None:
            return props
        return props.get(key, "")

    def SetClipProperty(self, key: str, value: Any) -> bool:
        if not hasattr(self, "_extra_clip_props"):
            self._extra_clip_props: dict[str, str] = {}
        self._extra_clip_props[str(key)] = str(value)
        return True

    def LinkProxyMedia(self, proxy_file_path: str) -> bool:
        self.proxy_media_path = proxy_file_path
        return True

    def UnlinkProxyMedia(self) -> bool:
        self.proxy_media_path = ""
        return True

    def GetClipColor(self) -> str:
        return self.clip_color

    def SetClipColor(self, color: str) -> bool:
        self.clip_color = color
        return True

    def AddMarker(
        self, frame_id: int, color: str, name: str, note: str, duration: int, custom_data: str = ""
    ) -> bool:
        if frame_id in self.markers:
            return False
        self.markers[int(frame_id)] = {
            "color": color,
            "name": name,
            "note": note,
            "duration": int(duration),
            "customData": custom_data,
        }
        return True

    def GetMarkers(self) -> dict[int, dict[str, Any]]:
        return copy.deepcopy(self.markers)

    def DeleteMarkerAtFrame(self, frame_id: int) -> bool:
        return self.markers.pop(int(frame_id), None) is not None


class FakeFusionTool:
    def __init__(self, name: str, tool_id: str = "TextPlus") -> None:
        self.Name = name
        self.ID = tool_id
        self.inputs: dict[str, Any] = {"StyledText": "Sample Title", "Size": 0.08}

    def GetAttrs(self) -> dict[str, Any]:
        return {"TOOLS_Name": self.Name, "TOOLS_RegID": self.ID}

    def SetInput(self, key: str, value: Any) -> None:
        self.inputs[key] = value

    def GetInput(self, key: str) -> Any:
        return self.inputs.get(key)


class FakeFusionComp:
    def __init__(self, tools: list[FakeFusionTool] | None = None) -> None:
        self._tools = tools or [FakeFusionTool("Template", "TextPlus")]

    def GetToolList(self, _selected: bool = False) -> dict[int, FakeFusionTool]:
        # Returns 1-based Lua table dict just like Fusion!
        return {i + 1: t for i, t in enumerate(self._tools)}

    def FindTool(self, name: str) -> FakeFusionTool | None:
        return next((t for t in self._tools if t.Name == name), None)


@dataclass
class FakeTimelineItem:
    name: str
    media_pool_item: FakeMediaPoolItem | None
    record_start: int
    record_end: int
    source_start: int = 0
    source_end: int = 240
    enabled: bool = True
    clip_color: str = ""
    unique_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    properties: dict[str, Any] = field(
        default_factory=lambda: {
            "Pan": 0.0,
            "Tilt": 0.0,
            "ZoomX": 1.0,
            "ZoomY": 1.0,
            "Opacity": 100.0,
            "CropLeft": 0.0,
            "CropRight": 0.0,
            "CropTop": 0.0,
            "CropBottom": 0.0,
        }
    )
    markers: dict[int, dict[str, Any]] = field(default_factory=dict)
    fusion_comps: list[FakeFusionComp] = field(default_factory=list)
    luts: dict[int, str] = field(default_factory=dict)
    cdl: dict[str, str] = field(default_factory=dict)

    def GetName(self) -> str:
        return self.name

    def GetUniqueId(self) -> str:
        return self.unique_id

    def GetMediaPoolItem(self) -> FakeMediaPoolItem | None:
        return self.media_pool_item

    def GetStart(self) -> int:
        return self.record_start

    def GetEnd(self) -> int:
        return self.record_end

    def GetDuration(self) -> int:
        return self.record_end - self.record_start

    def GetLeftOffset(self) -> int:
        return self.source_start

    def GetRightOffset(self) -> int:
        return self.source_end

    def GetClipEnabled(self) -> bool:
        return self.enabled

    def SetClipEnabled(self, enabled: bool) -> bool:
        self.enabled = bool(enabled)
        return True

    def GetClipColor(self) -> str:
        return self.clip_color

    def SetClipColor(self, color: str) -> bool:
        self.clip_color = color
        return True

    def GetProperty(self, key: str | None = None) -> Any:
        if key is None:
            return dict(self.properties)
        return self.properties.get(key)

    def SetProperty(self, key_or_dict: str | dict[str, Any], value: Any = None) -> bool:
        if isinstance(key_or_dict, dict):
            self.properties.update(key_or_dict)
            return True
        self.properties[str(key_or_dict)] = value
        return True

    def AddMarker(
        self, frame_id: int, color: str, name: str, note: str, duration: int, custom_data: str = ""
    ) -> bool:
        if frame_id in self.markers:
            return False
        self.markers[int(frame_id)] = {
            "color": color,
            "name": name,
            "note": note,
            "duration": int(duration),
            "customData": custom_data,
        }
        return True

    def GetMarkers(self) -> dict[int, dict[str, Any]]:
        return copy.deepcopy(self.markers)

    def DeleteMarkerAtFrame(self, frame_id: int) -> bool:
        return self.markers.pop(int(frame_id), None) is not None

    def GetFusionCompCount(self) -> int:
        return len(self.fusion_comps)

    def GetFusionCompByIndex(self, idx: int) -> FakeFusionComp | None:
        if 1 <= idx <= len(self.fusion_comps):
            return self.fusion_comps[idx - 1]
        return None

    def AddFusionComp(self) -> FakeFusionComp:
        comp = FakeFusionComp([])
        self.fusion_comps.append(comp)
        return comp

    def GetNodeCount(self) -> int:
        return 2

    def SetLUT(self, node_index: int, lut_path: str) -> bool:
        if node_index < 1:
            return False
        self.luts[int(node_index)] = lut_path
        return True

    def GetLUT(self, node_index: int) -> str:
        return self.luts.get(int(node_index), "")

    def SetCDL(self, cdl_map: dict[str, str]) -> bool:
        self.cdl.update({str(k): str(v) for k, v in cdl_map.items()})
        return True


class FakeTimeline:
    def __init__(self, name: str, project: FakeProject, start_frame: int = 86400) -> None:
        self.name = name
        self._project = project
        self.unique_id = uuid.uuid4().hex
        self.start_frame = start_frame  # 01:00:00:00 @ 24fps
        self.playhead_tc = "01:00:00:00"
        self.tracks: dict[str, list[list[FakeTimelineItem]]] = {
            "video": [[]],
            "audio": [[]],
            "subtitle": [],
        }
        self.markers: dict[int, dict[str, Any]] = {}

    def GetName(self) -> str:
        return self.name

    def SetName(self, name: str) -> bool:
        self.name = name
        return True

    def GetUniqueId(self) -> str:
        return self.unique_id

    def GetStartFrame(self) -> int:
        return self.start_frame

    def GetEndFrame(self) -> int:
        max_end = self.start_frame
        for track_list in self.tracks.values():
            for track in track_list:
                for item in track:
                    max_end = max(max_end, item.record_end)
        return max_end

    def GetStartTimecode(self) -> str:
        return "01:00:00:00"

    def GetCurrentTimecode(self) -> str:
        return self.playhead_tc

    def SetCurrentTimecode(self, tc: str) -> bool:
        self.playhead_tc = tc
        return True

    def GetTrackCount(self, track_type: str) -> int:
        return len(self.tracks.get(track_type, []))

    def AddTrack(self, track_type: str, _sub_type: str = "stereo") -> bool:
        if track_type not in self.tracks:
            return False
        self.tracks[track_type].append([])
        return True

    def GetTrackName(self, track_type: str, index: int) -> str:
        names = getattr(self, "_track_names", {})
        return names.get((track_type, index), f"{track_type[0].upper()}{index}")

    def SetTrackName(self, track_type: str, index: int, name: str) -> bool:
        if not hasattr(self, "_track_names"):
            self._track_names: dict[tuple[str, int], str] = {}
        self._track_names[(track_type, index)] = name
        return True

    def GetIsTrackEnabled(self, track_type: str, index: int) -> bool:
        en = getattr(self, "_track_enabled", {})
        return en.get((track_type, index), True)

    def SetTrackEnable(self, track_type: str, index: int, enabled: bool) -> bool:
        if not hasattr(self, "_track_enabled"):
            self._track_enabled: dict[tuple[str, int], bool] = {}
        self._track_enabled[(track_type, index)] = bool(enabled)
        return True

    def GetIsTrackLocked(self, track_type: str, index: int) -> bool:
        lk = getattr(self, "_track_locked", {})
        return lk.get((track_type, index), False)

    def SetTrackLock(self, track_type: str, index: int, locked: bool) -> bool:
        if not hasattr(self, "_track_locked"):
            self._track_locked: dict[tuple[str, int], bool] = {}
        self._track_locked[(track_type, index)] = bool(locked)
        return True

    def CreateSubtitlesFromAudio(self, _opts: dict[str, Any] | None = None) -> bool:
        if not self.tracks["subtitle"]:
            self.tracks["subtitle"].append([])
        return True

    def GetItemListInTrack(self, track_type: str, index: int) -> list[FakeTimelineItem]:
        tlist = self.tracks.get(track_type, [])
        if 1 <= index <= len(tlist):
            return list(tlist[index - 1])
        return []

    def DuplicateTimeline(self, timeline_name: str | None = None) -> FakeTimeline:
        new_name = timeline_name or f"{self.name} copy"
        dup = FakeTimeline(new_name, self._project, self.start_frame)
        dup.playhead_tc = self.playhead_tc
        dup.markers = copy.deepcopy(self.markers)
        dup.tracks = {
            ttype: [
                [
                    FakeTimelineItem(
                        name=it.name,
                        media_pool_item=it.media_pool_item,
                        record_start=it.record_start,
                        record_end=it.record_end,
                        source_start=it.source_start,
                        source_end=it.source_end,
                        enabled=it.enabled,
                        clip_color=it.clip_color,
                        properties=dict(it.properties),
                        markers=copy.deepcopy(it.markers),
                        fusion_comps=copy.deepcopy(it.fusion_comps),
                    )
                    for it in track
                ]
                for track in tlist
            ]
            for ttype, tlist in self.tracks.items()
        }
        self._project.timelines.append(dup)
        return dup

    def DeleteClips(self, clips: list[FakeTimelineItem], ripple: bool = False) -> bool:
        ids = {c.unique_id for c in clips}
        removed_any = False
        for tlist in self.tracks.values():
            for track in tlist:
                kept: list[FakeTimelineItem] = []
                shift = 0
                for item in track:
                    if item.unique_id in ids:
                        removed_any = True
                        if ripple:
                            shift += item.GetDuration()
                    else:
                        if shift:
                            item.record_start -= shift
                            item.record_end -= shift
                        kept.append(item)
                track[:] = kept
        return removed_any

    def InsertFusionTitleIntoTimeline(self, title_name: str) -> FakeTimelineItem:
        cur_end = self.GetEndFrame()
        comp = FakeFusionComp([FakeFusionTool("Template", "TextPlus")])
        item = FakeTimelineItem(
            name=title_name,
            media_pool_item=None,
            record_start=cur_end,
            record_end=cur_end + 120,
            fusion_comps=[comp],
        )
        self.tracks["video"][0].append(item)
        return item

    def InsertGeneratorIntoTimeline(self, gen_name: str) -> FakeTimelineItem:
        cur_end = self.GetEndFrame()
        item = FakeTimelineItem(
            name=gen_name,
            media_pool_item=None,
            record_start=cur_end,
            record_end=cur_end + 120,
        )
        self.tracks["video"][0].append(item)
        return item

    def AddMarker(
        self, frame_id: int, color: str, name: str, note: str, duration: int, custom_data: str = ""
    ) -> bool:
        if int(frame_id) in self.markers:
            return False
        self.markers[int(frame_id)] = {
            "color": color,
            "name": name,
            "note": note,
            "duration": int(duration),
            "customData": custom_data,
        }
        return True

    def GetMarkers(self) -> dict[int, dict[str, Any]]:
        return copy.deepcopy(self.markers)

    def DeleteMarkerAtFrame(self, frame_id: int) -> bool:
        return self.markers.pop(int(frame_id), None) is not None

    def DeleteMarkersByColor(self, color: str) -> bool:
        if color.lower() == "all":
            had = bool(self.markers)
            self.markers.clear()
            return had
        to_del = [f for f, m in self.markers.items() if m["color"].lower() == color.lower()]
        for f in to_del:
            del self.markers[f]
        return bool(to_del)


class FakeFolder:
    def __init__(self, name: str) -> None:
        self.name = name
        self.unique_id = uuid.uuid4().hex
        self.subfolders: list[FakeFolder] = []
        self.clips: list[FakeMediaPoolItem] = []

    def GetName(self) -> str:
        return self.name

    def GetUniqueId(self) -> str:
        return self.unique_id

    def GetSubFolderList(self) -> dict[int, FakeFolder]:
        # Intentionally returns a 1-based Lua table dict to test lua_list() normalization!
        return {i + 1: f for i, f in enumerate(self.subfolders)}

    def GetClipList(self) -> list[FakeMediaPoolItem]:
        return list(self.clips)


class FakeMediaPool:
    def __init__(self, project: FakeProject) -> None:
        self._project = project
        self.root_folder = FakeFolder("Master")
        self.current_folder = self.root_folder

    def GetRootFolder(self) -> FakeFolder:
        return self.root_folder

    def GetCurrentFolder(self) -> FakeFolder:
        return self.current_folder

    def SetCurrentFolder(self, folder: FakeFolder) -> bool:
        self.current_folder = folder
        return True

    def AddSubFolder(self, parent: FakeFolder, name: str) -> FakeFolder:
        sub = FakeFolder(name)
        parent.subfolders.append(sub)
        self.current_folder = sub
        return sub

    def ImportMedia(self, paths: list[str]) -> list[FakeMediaPoolItem]:
        created: list[FakeMediaPoolItem] = []
        for p in paths:
            name = p.replace("\\", "/").split("/")[-1]
            clip = FakeMediaPoolItem(name=name, file_path=p, frames=300)
            self.current_folder.clips.append(clip)
            created.append(clip)
        return created

    def CreateEmptyTimeline(self, name: str) -> FakeTimeline | None:
        if any(t.name == name for t in self._project.timelines):
            return None
        tl = FakeTimeline(name, self._project)
        self._project.timelines.append(tl)
        self._project.current_timeline = tl
        return tl

    def AppendToTimeline(self, clip_infos: list[Any]) -> list[FakeTimelineItem]:
        tl = self._project.current_timeline
        if tl is None:
            tl = FakeTimeline("Timeline 1", self._project)
            self._project.timelines.append(tl)
            self._project.current_timeline = tl

        added: list[FakeTimelineItem] = []
        for entry in clip_infos:
            if isinstance(entry, FakeMediaPoolItem):
                mpi = entry
                s_frame = 0
                e_frame = mpi.frames
                track_idx = 1
                rec_frame = tl.GetEndFrame()
            else:
                mpi = entry["mediaPoolItem"]
                s_frame = int(entry.get("startFrame", 0))
                e_frame = int(entry.get("endFrame", mpi.frames))
                track_idx = int(entry.get("trackIndex", 1))
                rec_frame = int(entry.get("recordFrame", tl.GetEndFrame()))

            while len(tl.tracks["video"]) < track_idx:
                tl.tracks["video"].append([])

            dur = max(1, e_frame - s_frame)
            item = FakeTimelineItem(
                name=mpi.name,
                media_pool_item=mpi,
                record_start=rec_frame,
                record_end=rec_frame + dur,
                source_start=s_frame,
                source_end=e_frame,
            )
            tl.tracks["video"][track_idx - 1].append(item)
            added.append(item)
        return added

    def DeleteTimelines(self, timelines: list[FakeTimeline]) -> bool:
        drop_ids = {t.unique_id for t in timelines}
        self._project.timelines = [t for t in self._project.timelines if t.unique_id not in drop_ids]
        if self._project.current_timeline and self._project.current_timeline.unique_id in drop_ids:
            self._project.current_timeline = (
                self._project.timelines[-1] if self._project.timelines else None
            )
        return True


class FakeProject:
    def __init__(self, name: str = "Demo Edit") -> None:
        self.name = name
        self.settings: dict[str, str] = {
            "timelineFrameRate": "24",
            "timelineResolutionWidth": "1920",
            "timelineResolutionHeight": "1080",
        }
        self.media_pool = FakeMediaPool(self)
        self.timelines: list[FakeTimeline] = []
        tl = FakeTimeline("Main Cut", self)
        self.timelines.append(tl)
        self.current_timeline: FakeTimeline | None = tl
        self.render_jobs: list[dict[str, Any]] = []
        self.render_presets: list[str] = ["H.264 Master", "YouTube 1080p", "ProRes 422 HQ"]

    def GetName(self) -> str:
        return self.name

    def GetMediaPool(self) -> FakeMediaPool:
        return self.media_pool

    def GetSetting(self, name: str = "") -> Any:
        if not name:
            return dict(self.settings)
        return self.settings.get(name, "")

    def SetSetting(self, name: str, value: str) -> bool:
        self.settings[name] = str(value)
        return True

    def GetTimelineCount(self) -> int:
        return len(self.timelines)

    def GetTimelineByIndex(self, idx: int) -> FakeTimeline | None:
        if 1 <= idx <= len(self.timelines):
            return self.timelines[idx - 1]
        return None

    def GetCurrentTimeline(self) -> FakeTimeline | None:
        return self.current_timeline

    def SetCurrentTimeline(self, timeline: FakeTimeline) -> bool:
        if timeline in self.timelines:
            self.current_timeline = timeline
            return True
        return False

    def ExportCurrentFrameAsStill(self, file_path: str) -> bool:
        # Write a minimal valid 1x1 PNG so image loaders see real PNG bytes!
        png_1x1 = (
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
            b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\xcf\xc0"
            b"\x00\x00\x03\x01\x01\x00\xc9\xfe\x92\xef\x00\x00\x00\x00IEND\xaeB`\x82"
        )
        with open(file_path, "wb") as f:
            f.write(png_1x1)
        return True

    def GetRenderPresetList(self) -> list[str]:
        return list(self.render_presets)

    def LoadRenderPreset(self, name: str) -> bool:
        return name in self.render_presets

    def SetRenderSettings(self, settings: dict[str, Any]) -> bool:
        self._last_render_settings = dict(settings)
        return True

    def AddRenderJob(self) -> str:
        job_id = f"job-{len(self.render_jobs) + 1}"
        self.render_jobs.append({"JobId": job_id, "JobStatus": "Ready", "CompletionPercentage": 0})
        return job_id

    def StartRendering(self, *args: Any) -> bool:
        for job in self.render_jobs:
            if not args or job["JobId"] in args or (isinstance(args[0], list) and job["JobId"] in args[0]):
                job["JobStatus"] = "Complete"
                job["CompletionPercentage"] = 100
        return True

    def GetRenderJobList(self) -> list[dict[str, Any]]:
        return copy.deepcopy(self.render_jobs)

    def GetRenderJobStatus(self, job_id: str) -> dict[str, Any]:
        for job in self.render_jobs:
            if job["JobId"] == job_id:
                return {"JobStatus": job["JobStatus"], "CompletionPercentage": job["CompletionPercentage"]}
        return {}


class FakeProjectManager:
    def __init__(self) -> None:
        self.projects: dict[str, FakeProject] = {"Demo Edit": FakeProject("Demo Edit")}
        self.current_project: FakeProject | None = self.projects["Demo Edit"]
        self.saved_count = 0

    def GetCurrentProject(self) -> FakeProject | None:
        return self.current_project

    def GetProjectListInCurrentFolder(self) -> dict[int, str]:
        # Lua 1-based dict return!
        return {i + 1: name for i, name in enumerate(self.projects.keys())}

    def GetFolderListInCurrentFolder(self) -> list[str]:
        return ["Client Work"]

    def GetCurrentFolder(self) -> str:
        return "Root"

    def SaveProject(self) -> bool:
        self.saved_count += 1
        return True

    def LoadProject(self, name: str) -> FakeProject | None:
        proj = self.projects.get(name)
        if proj:
            self.current_project = proj
        return proj

    def CreateProject(self, name: str) -> FakeProject | None:
        if name in self.projects:
            return None
        proj = FakeProject(name)
        self.projects[name] = proj
        self.current_project = proj
        return proj


class FakeResolve:
    def __init__(self, *, studio: bool = True, version: str = "20.0.1") -> None:
        self.studio = studio
        self.version = version
        self.page = "edit"
        self.pm = FakeProjectManager()

    def GetProductName(self) -> str:
        return "DaVinci Resolve Studio" if self.studio else "DaVinci Resolve"

    def GetVersionString(self) -> str:
        return self.version

    def GetVersion(self) -> dict[int, Any]:
        parts = [int(p) for p in self.version.split(".")]
        return {1: parts[0], 2: parts[1], 3: parts[2], 4: 12, 5: ""}

    def GetCurrentPage(self) -> str:
        return self.page

    def OpenPage(self, page: str) -> bool:
        self.page = page
        return True

    def GetProjectManager(self) -> FakeProjectManager:
        return self.pm

    def Fusion(self) -> None:
        return None