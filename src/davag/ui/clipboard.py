"""Cross-platform clipboard copy and browser launch helpers."""

from __future__ import annotations

import platform
import shutil
import subprocess
import webbrowser


def copy_to_clipboard(text: str) -> bool:
    """Copy `text` to the OS clipboard without third-party dependencies."""
    sys_name = platform.system()
    try:
        if sys_name == "Windows":
            # PowerShell Set-Clipboard preserves exact UTF-8 without trailing newline!
            res = subprocess.run(
                ["powershell", "-NoProfile", "-Command", "$input | Set-Clipboard"],
                input=text,
                text=True,
                encoding="utf-8",
                capture_output=True,
                timeout=3,
                check=False,
            )
            return res.returncode == 0
        if sys_name == "Darwin" and shutil.which("pbcopy"):
            res = subprocess.run(
                ["pbcopy"], input=text, text=True, encoding="utf-8", timeout=3, check=False
            )
            return res.returncode == 0
        for linux_cmd in (["wl-copy"], ["xclip", "-selection", "clipboard"]):
            if shutil.which(linux_cmd[0]):
                res = subprocess.run(
                    linux_cmd, input=text, text=True, encoding="utf-8", timeout=3, check=False
                )
                return res.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False
    return False


def open_in_browser(url: str) -> bool:
    try:
        return bool(webbrowser.open(url, new=2))
    except Exception:  # noqa: BLE001
        return False