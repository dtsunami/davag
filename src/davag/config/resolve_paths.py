"""Cross-platform discovery for DaVinci Resolve scripting libraries, folders, and FFmpeg."""

from __future__ import annotations

import os
import platform
import shutil
import struct
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class ResolveInstallation:
    platform: str
    python_version: str
    python_arch_bits: int
    script_api_dir: str | None
    script_modules_dir: str | None
    script_lib_path: str | None
    dvr_module_file: str | None
    user_scripts_utility_dir: str
    system_scripts_utility_dir: str | None
    resolve_exe_path: str | None
    resolve_running: bool
    ffmpeg_path: str | None
    ffprobe_path: str | None

    @property
    def direct_ready(self) -> bool:
        return bool(
            self.python_arch_bits == 64
            and self.script_lib_path
            and Path(self.script_lib_path).is_file()
            and self.dvr_module_file
            and Path(self.dvr_module_file).is_file()
        )

    def to_dict(self) -> dict[str, object]:
        d = asdict(self)
        d["direct_ready"] = self.direct_ready
        return d


def _first_existing(candidates: list[Path]) -> Path | None:
    for p in candidates:
        try:
            if p.exists():
                return p
        except OSError:
            continue
    return None


def _is_resolve_running(sys_name: str) -> bool:
    try:
        if sys_name == "Windows":
            res = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq Resolve.exe", "/NH"],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
            return "resolve.exe" in res.stdout.lower()
        res = subprocess.run(
            ["pgrep", "-f", "Resolve"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        return res.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def discover_resolve(env: dict[str, str] | None = None) -> ResolveInstallation:
    """Locate DaVinci Resolve scripting paths on Windows, macOS, or Linux."""
    e = env if env is not None else dict(os.environ)
    sys_name = platform.system()
    arch_bits = struct.calcsize("P") * 8

    env_api = e.get("DAVAG_RESOLVE_SCRIPT_API") or e.get("RESOLVE_SCRIPT_API")
    env_lib = e.get("DAVAG_RESOLVE_SCRIPT_LIB") or e.get("RESOLVE_SCRIPT_LIB")

    api_candidates: list[Path] = []
    lib_candidates: list[Path] = []
    exe_candidates: list[Path] = []
    sys_scripts_dir: Path | None = None

    if env_api:
        api_candidates.append(Path(env_api))
    if env_lib:
        lib_candidates.append(Path(env_lib))

    if sys_name == "Windows":
        prog_data = Path(e.get("PROGRAMDATA", r"C:\ProgramData"))
        prog_files = Path(e.get("PROGRAMFILES", r"C:\Program Files"))
        appdata = Path(e.get("APPDATA", str(Path.home() / "AppData" / "Roaming")))

        api_candidates.append(
            prog_data / "Blackmagic Design" / "DaVinci Resolve" / "Support" / "Developer" / "Scripting"
        )
        lib_candidates.extend(
            [
                prog_files / "Blackmagic Design" / "DaVinci Resolve" / "fusionscript.dll",
                Path(r"C:\Program Files\Blackmagic Design\DaVinci Resolve\fusionscript.dll"),
            ]
        )
        exe_candidates.append(prog_files / "Blackmagic Design" / "DaVinci Resolve" / "Resolve.exe")
        user_utility = (
            appdata / "Blackmagic Design" / "DaVinci Resolve" / "Support" / "Fusion" / "Scripts" / "Utility"
        )
        sys_scripts_dir = (
            prog_data / "Blackmagic Design" / "DaVinci Resolve" / "Fusion" / "Scripts" / "Utility"
        )
    elif sys_name == "Darwin":
        api_candidates.append(
            Path("/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting")
        )
        lib_candidates.append(
            Path("/Applications/DaVinci Resolve/DaVinci Resolve.app/Contents/Libraries/Fusion/fusionscript.so")
        )
        exe_candidates.append(
            Path("/Applications/DaVinci Resolve/DaVinci Resolve.app/Contents/MacOS/Resolve")
        )
        user_utility = (
            Path.home()
            / "Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Utility"
        )
        sys_scripts_dir = Path(
            "/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Utility"
        )
    else:
        api_candidates.extend(
            [
                Path("/opt/resolve/Developer/Scripting"),
                Path("/home/resolve/Developer/Scripting"),
            ]
        )
        lib_candidates.extend(
            [
                Path("/opt/resolve/libs/Fusion/fusionscript.so"),
                Path("/home/resolve/libs/Fusion/fusionscript.so"),
            ]
        )
        exe_candidates.extend([Path("/opt/resolve/bin/resolve"), Path("/home/resolve/bin/resolve")])
        user_utility = Path.home() / ".local/share/DaVinciResolve/Fusion/Scripts/Utility"
        sys_scripts_dir = Path("/opt/resolve/Fusion/Scripts/Utility")

    api_dir = _first_existing(api_candidates)
    modules_dir = api_dir / "Modules" if api_dir and (api_dir / "Modules").is_dir() else None
    dvr_mod = (
        modules_dir / "DaVinciResolveScript.py"
        if modules_dir and (modules_dir / "DaVinciResolveScript.py").is_file()
        else None
    )
    lib_path = _first_existing(lib_candidates)
    exe_path = _first_existing(exe_candidates)

    return ResolveInstallation(
        platform=f"{sys_name} ({platform.machine()})",
        python_version=sys.version.split()[0],
        python_arch_bits=arch_bits,
        script_api_dir=str(api_dir) if api_dir else None,
        script_modules_dir=str(modules_dir) if modules_dir else None,
        script_lib_path=str(lib_path) if lib_path else None,
        dvr_module_file=str(dvr_mod) if dvr_mod else None,
        user_scripts_utility_dir=str(user_utility),
        system_scripts_utility_dir=str(sys_scripts_dir) if sys_scripts_dir else None,
        resolve_exe_path=str(exe_path) if exe_path else None,
        resolve_running=_is_resolve_running(sys_name),
        ffmpeg_path=shutil.which("ffmpeg"),
        ffprobe_path=shutil.which("ffprobe"),
    )