"""Normalization and SMPTE timecode/frame math helpers for DaVinci Resolve (stdlib only).

Handles two notorious Resolve scripting quirks:
1. `fusionscript` marshals Lua 1-indexed arrays as `{1: a, 2: b}` dicts in Python.
2. Frame rates can be fractional/drop-frame (`23.976`, `29.97`, `59.94`) or strings (`"24.000"`).
"""

from __future__ import annotations

import re
from typing import Any

TC_RE = re.compile(r"^(?P<h>\d{1,2}):(?P<m>\d{2}):(?P<s>\d{2})(?P<sep>[:;])(?P<f>\d{2,3})$")


def lua_list(val: Any) -> list[Any]:
    """Normalize a Resolve return value (list, tuple, or 1-based Lua table dict) into a Python list."""
    if val is None:
        return []
    if isinstance(val, list):
        return val
    if isinstance(val, tuple):
        return list(val)
    if isinstance(val, dict):
        if not val:
            return []
        # Check if keys are 1..N integer or numeric-string indices from Lua
        int_items: list[tuple[int, Any]] = []
        for k, v in val.items():
            if isinstance(k, int):
                int_items.append((k, v))
            elif isinstance(k, str) and k.isdigit():
                int_items.append((int(k), v))
            else:
                return list(val.values())
        int_items.sort(key=lambda pair: pair[0])
        return [v for _, v in int_items]
    return [val]


def lua_dict(val: Any) -> dict[str, Any]:
    """Normalize a Resolve dictionary return value into a JSON-safe string-keyed dict."""
    if not isinstance(val, dict):
        return {}
    out: dict[str, Any] = {}
    for k, v in val.items():
        if isinstance(v, (str, int, float, bool)) or v is None:
            out[str(k)] = v
        elif isinstance(v, (list, tuple)):
            out[str(k)] = list(v)
        elif isinstance(v, dict):
            out[str(k)] = lua_dict(v)
        else:
            out[str(k)] = str(v)
    return out


def parse_fps(raw_fps: Any) -> tuple[float, int, bool]:
    """Return `(exact_fps, nominal_timebase, is_ntsc_fractional)` from Resolve's frame rate setting.

    Resolve reports frame rates in formats like `"24"`, `"23.976"`, `"29.97 DF"`, `29.97`, `"59.94"`.
    """
    if raw_fps is None:
        return 24.0, 24, False
    s = str(raw_fps).strip().upper()
    s_num = re.sub(r"[^0-9.]", "", s)
    val = float(s_num) if s_num else 24.0

    # Standard NTSC fractional rates
    if abs(val - 23.976) < 0.02:
        return 24000.0 / 1001.0, 24, True
    if abs(val - 29.97) < 0.02:
        return 30000.0 / 1001.0, 30, True
    if abs(val - 47.952) < 0.03:
        return 48000.0 / 1001.0, 48, True
    if abs(val - 59.94) < 0.03:
        return 60000.0 / 1001.0, 60, True
    if abs(val - 119.88) < 0.06:
        return 120000.0 / 1001.0, 120, True

    nominal = max(1, round(val))
    return float(val), nominal, False


def tc_to_frames(tc: str, raw_fps: Any = 24, *, drop_frame: bool | None = None) -> int:
    """Convert SMPTE timecode (`HH:MM:SS:FF` or `HH:MM:SS;FF`) to frame count from `00:00:00:00`."""
    m = TC_RE.match(tc.strip())
    if not m:
        raise ValueError(f'Invalid SMPTE timecode "{tc}" (expected HH:MM:SS:FF or HH:MM:SS;FF)')

    hours = int(m.group("h"))
    minutes = int(m.group("m"))
    seconds = int(m.group("s"))
    sep = m.group("sep")
    frames = int(m.group("f"))

    _, timebase, _ = parse_fps(raw_fps)
    is_df = (sep == ";") if drop_frame is None else drop_frame
    if "DF" in str(raw_fps).upper() and "NDF" not in str(raw_fps).upper() and drop_frame is None:
        is_df = True

    if is_df and timebase in (30, 60):
        drop_frames = 2 if timebase == 30 else 4
        total_minutes = 60 * hours + minutes
        dropped = drop_frames * (total_minutes - (total_minutes // 10))
        return (
            (hours * 3600 + minutes * 60 + seconds) * timebase
            + frames
            - dropped
        )

    return ((hours * 3600) + (minutes * 60) + seconds) * timebase + frames


def frames_to_tc(frame_number: int, raw_fps: Any = 24, *, drop_frame: bool = False) -> str:
    """Convert a 0-based frame count into SMPTE timecode (`HH:MM:SS:FF` or `HH:MM:SS;FF`)."""
    frames = max(0, int(frame_number))
    _, timebase, _ = parse_fps(raw_fps)
    is_df = drop_frame or ("DF" in str(raw_fps).upper() and "NDF" not in str(raw_fps).upper())

    if is_df and timebase in (30, 60):
        drop = 2 if timebase == 30 else 4
        frames_per_10_min = timebase * 60 * 10 - (9 * drop)
        frames_per_min = timebase * 60 - drop

        d = frames // frames_per_10_min
        m = frames % frames_per_10_min

        if m >= drop:
            frames += drop * 9 * d + drop * ((m - drop) // frames_per_min)
        else:
            frames += drop * 9 * d

        ff = frames % timebase
        ss = (frames // timebase) % 60
        mm = (frames // (timebase * 60)) % 60
        hh = frames // (timebase * 3600)
        return f"{hh:02d}:{mm:02d}:{ss:02d};{ff:02d}"

    ff = frames % timebase
    total_seconds = frames // timebase
    ss = total_seconds % 60
    mm = (total_seconds // 60) % 60
    hh = total_seconds // 3600
    return f"{hh:02d}:{mm:02d}:{ss:02d}:{ff:02d}"


def safe_call(obj: Any, method: str, *args: Any, default: Any = None) -> Any:
    """Call `getattr(obj, method)(*args)` safely, returning `default` if absent or failing."""
    if obj is None:
        return default
    fn = getattr(obj, method, None)
    if not callable(fn):
        return default
    try:
        res = fn(*args)
        return default if res is None else res
    except Exception:  # noqa: BLE001 — C++ proxy boundary may raise arbitrary runtime errors
        return default