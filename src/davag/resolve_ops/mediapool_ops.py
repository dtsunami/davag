"""Media Pool & Bin operations (100% Python stdlib only)."""

from __future__ import annotations

from typing import Any

from .helpers import lua_dict, lua_list, safe_call
from .schema import OpContext, OpError, resolve_op


def _serialize_clip(clip: Any, bin_path: str) -> dict[str, Any]:
    props = lua_dict(safe_call(clip, "GetClipProperty", default={}))
    frames_str = str(props.get("Frames", "0") or "0")
    frames = int(frames_str) if frames_str.isdigit() else 0
    markers = safe_call(clip, "GetMarkers", default={})
    out = {
        "clip_id": str(safe_call(clip, "GetUniqueId", default="")),
        "name": str(safe_call(clip, "GetName", default=props.get("Clip Name", ""))),
        "bin_path": bin_path,
        "file_path": str(props.get("File Path", "")),
        "fps": props.get("FPS", ""),
        "frames": frames,
        "resolution": props.get("Resolution", ""),
        "audio_channels": props.get("Audio Ch", ""),
        "proxy_status": str(props.get("Proxy", "")),
        "proxy_path": str(props.get("Proxy Media Path", "")),
        "clip_color": str(safe_call(clip, "GetClipColor", default="")),
        "markers_count": len(markers) if isinstance(markers, dict) else 0,
    }
    alpha_val = str(props.get("Alpha mode", "")).strip()
    if alpha_val:
        out["alpha_mode"] = alpha_val
    return out


def _folder_tree(folder: Any, prefix: str = "") -> dict[str, Any]:
    name = str(safe_call(folder, "GetName", default="Master"))
    path = f"{prefix}/{name}" if prefix else name
    clips = lua_list(safe_call(folder, "GetClipList", default=[]))
    subs = lua_list(safe_call(folder, "GetSubFolderList", default=[]))
    return {
        "folder_id": str(safe_call(folder, "GetUniqueId", default="")),
        "name": name,
        "path": path,
        "clip_count": len(clips),
        "subfolders": [_folder_tree(sub, path) for sub in subs],
    }


def find_folder_by_path(root: Any, bin_path: str | None, *, create: bool = False, mp: Any = None) -> Any:
    if not bin_path or bin_path.strip() in ("", "/", "Master"):
        return root
    parts = [p.strip() for p in bin_path.replace("\\", "/").split("/") if p.strip()]
    if parts and parts[0].lower() == str(safe_call(root, "GetName", default="Master")).lower():
        parts = parts[1:]

    cur = root
    for part in parts:
        subs = lua_list(safe_call(cur, "GetSubFolderList", default=[]))
        match = next(
            (s for s in subs if str(safe_call(s, "GetName", default="")).lower() == part.lower()),
            None,
        )
        if match is None:
            if not create or mp is None:
                raise OpError(f'Bin "{bin_path}" not found (missing "{part}")', code="BIN_NOT_FOUND")
            match = mp.AddSubFolder(cur, part)
            if match is None:
                raise OpError(f'Failed to create subfolder "{part}"', code="CREATE_BIN_FAILED")
        cur = match
    return cur


def iter_all_clips(folder: Any, prefix: str = "") -> list[tuple[Any, str]]:
    name = str(safe_call(folder, "GetName", default="Master"))
    path = f"{prefix}/{name}" if prefix else name
    out: list[tuple[Any, str]] = [(c, path) for c in lua_list(safe_call(folder, "GetClipList", default=[]))]
    for sub in lua_list(safe_call(folder, "GetSubFolderList", default=[])):
        out.extend(iter_all_clips(sub, path))
    return out


def find_media_pool_clip(mp: Any, *, clip_id: str | None = None, name: str | None = None) -> Any:
    root = mp.GetRootFolder()
    for clip, _path in iter_all_clips(root):
        if clip_id and str(safe_call(clip, "GetUniqueId", default="")) == clip_id:
            return clip
        if name and str(safe_call(clip, "GetName", default="")) == name:
            return clip
    target = clip_id or name or "<unspecified>"
    raise OpError(f'MediaPool clip "{target}" not found', code="CLIP_NOT_FOUND")


@resolve_op(
    "mediapool.tree",
    tier="read",
    summary="Return the recursive folder/bin hierarchy and clip counts in the Media Pool.",
)
def op_mediapool_tree(ctx: OpContext, _params: dict[str, Any]) -> dict[str, Any]:
    mp = ctx.media_pool
    root = mp.GetRootFolder()
    cur = safe_call(mp, "GetCurrentFolder")
    return {
        "current_folder": str(safe_call(cur, "GetName", default="")) if cur else None,
        "root": _folder_tree(root),
    }


@resolve_op(
    "mediapool.clips",
    tier="read",
    summary="List clips and metadata in the Media Pool (recursively or within a specific bin).",
    params_doc={
        "bin_path": "Optional bin path (e.g. 'Master/B-Roll'); defaults to root",
        "recursive": "Optional bool (default true)",
    },
)
def op_mediapool_clips(ctx: OpContext, params: dict[str, Any]) -> dict[str, Any]:
    mp = ctx.media_pool
    root = mp.GetRootFolder()
    bin_path = params.get("bin_path")
    recursive = bool(params.get("recursive", True))

    folder = find_folder_by_path(root, bin_path)
    folder_name = str(safe_call(folder, "GetName", default="Master"))
    if recursive:
        pairs = iter_all_clips(folder, "" if folder is root else str(bin_path).rsplit("/", 1)[0])
    else:
        pairs = [(c, str(bin_path or folder_name)) for c in lua_list(safe_call(folder, "GetClipList", default=[]))]

    return {
        "count": len(pairs),
        "clips": [_serialize_clip(c, p) for c, p in pairs],
    }


@resolve_op(
    "mediapool.create_bin",
    tier="mutate",
    summary="Create a bin path (including nested bins like 'Master/Footage/A-Cam') and select it.",
    params_doc={"bin_path": "Bin path to create, e.g. 'Interviews/Day1'"},
)
def op_mediapool_create_bin(ctx: OpContext, params: dict[str, Any]) -> dict[str, Any]:
    bin_path = str(params.get("bin_path", "")).strip()
    if not bin_path:
        raise OpError("mediapool.create_bin requires 'bin_path'", code="MISSING_PARAM")
    mp = ctx.media_pool
    folder = find_folder_by_path(mp.GetRootFolder(), bin_path, create=True, mp=mp)
    safe_call(mp, "SetCurrentFolder", folder)
    return {
        "folder_id": str(safe_call(folder, "GetUniqueId", default="")),
        "name": str(safe_call(folder, "GetName", default="")),
        "bin_path": bin_path,
    }


@resolve_op(
    "mediapool.import",
    tier="mutate",
    summary="Import media files from disk into a Media Pool bin (with optional Straight/Premultiplied alpha_mode).",
    params_doc={
        "paths": "List of file or directory paths on disk",
        "bin_path": "Optional target bin path (created automatically if missing)",
        "alpha_mode": "Optional alpha channel mode ('Straight', 'Premultiplied', or 'None') applied via SetClipProperty",
    },
)
def op_mediapool_import(ctx: OpContext, params: dict[str, Any]) -> dict[str, Any]:
    raw_paths = params.get("paths")
    if not isinstance(raw_paths, list) or not raw_paths:
        raise OpError("mediapool.import requires non-empty list 'paths'", code="MISSING_PARAM")

    raw_alpha = str(params.get("alpha_mode", "")).strip()
    norm_alpha = ""
    if raw_alpha:
        alpha_map = {
            "straight": "Straight",
            "premultiplied": "Premultiplied",
            "none": "None",
            "ignore": "None",
        }
        norm_alpha = alpha_map.get(raw_alpha.lower(), "")
        if not norm_alpha:
            raise OpError(
                f"Invalid alpha_mode '{raw_alpha}' (expected 'Straight', 'Premultiplied', or 'None')",
                code="INVALID_PARAM",
            )

    mp = ctx.media_pool
    bin_path = str(params.get("bin_path", "")).strip()
    if bin_path:
        folder = find_folder_by_path(mp.GetRootFolder(), bin_path, create=True, mp=mp)
        safe_call(mp, "SetCurrentFolder", folder)
    else:
        folder = safe_call(mp, "GetCurrentFolder") or mp.GetRootFolder()

    folder_label = bin_path or str(safe_call(folder, "GetName", default="Master"))
    imported = lua_list(mp.ImportMedia([str(p) for p in raw_paths]))
    if norm_alpha:
        for c in imported:
            safe_call(c, "SetClipProperty", "Alpha mode", norm_alpha)

    return {
        "imported_count": len(imported),
        "bin_path": folder_label,
        **({"alpha_mode": norm_alpha} if norm_alpha else {}),
        "clips": [_serialize_clip(c, folder_label) for c in imported],
    }


@resolve_op(
    "mediapool.link_proxy",
    tier="mutate",
    summary="Link (or unlink) an external proxy media file onto a DaVinci Resolve Media Pool clip.",
    params_doc={
        "clip_id": "Media Pool clip_id (or provide clip_name)",
        "clip_name": "Optional clip name if clip_id omitted",
        "proxy_path": "Absolute path to proxy .mp4/.mov (omit or pass '' with unlink=true to unlink)",
        "unlink": "Optional bool (default false) — unlink existing proxy media",
    },
)
def op_mediapool_link_proxy(ctx: OpContext, params: dict[str, Any]) -> dict[str, Any]:
    clip = find_media_pool_clip(
        ctx.media_pool,
        clip_id=params.get("clip_id"),
        name=params.get("clip_name"),
    )
    if bool(params.get("unlink", False)):
        ok = bool(safe_call(clip, "UnlinkProxyMedia", default=False))
        return {"clip_id": str(safe_call(clip, "GetUniqueId", default="")), "unlinked": ok}

    proxy_path = str(params.get("proxy_path", "")).strip()
    if not proxy_path:
        raise OpError("mediapool.link_proxy requires 'proxy_path' (or unlink=true)", code="MISSING_PARAM")
    ok = bool(clip.LinkProxyMedia(proxy_path))
    if not ok:
        raise OpError(f'Resolve rejected LinkProxyMedia("{proxy_path}")', code="LINK_PROXY_FAILED")
    return {
        "clip_id": str(safe_call(clip, "GetUniqueId", default="")),
        "name": str(safe_call(clip, "GetName", default="")),
        "proxy_path": proxy_path,
        "linked": True,
    }