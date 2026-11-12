"""Atomic, comment-preserving, CRLF-safe .env reader and editor.

Mirrors davai/src/config/envfile.js so .env files hand-edited on Windows or POSIX
keep their exact comments, ordering, and line endings across key updates and
provider-block switches.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from pathlib import Path

ASSIGN_RE = re.compile(r"^(\s*(?:export\s+)?)([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")
BARE_RE = re.compile(r"^[A-Za-z0-9_@%+:,./=-]*$")


def env_file_path(home: str | Path) -> Path:
    return Path(home) / ".env"


def parse_dotenv(text: str) -> dict[str, str]:
    """Parse dotenv-formatted text into a dict without mutating os.environ."""
    out: dict[str, str] = {}
    for raw_line in to_lines(text):
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        m = ASSIGN_RE.match(raw_line)
        if not m:
            continue
        name = m.group(2)
        val = m.group(3).strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ('"', "'"):
            quote = val[0]
            if quote == '"':
                try:
                    val = json.loads(val)
                except ValueError:
                    val = val[1:-1]
            else:
                val = val[1:-1]
        else:
            # Strip inline unquoted `# comment` preceded by whitespace
            hash_idx = re.search(r"\s+#", val)
            if hash_idx:
                val = val[: hash_idx.start()].rstrip()
        out[name] = val
    return out


def read_env(file: str | Path) -> dict[str, str]:
    text = read_env_text(file)
    return parse_dotenv(text) if text else {}


def format_assignment(name: str, value: str, prefix: str = "") -> str:
    s = str(value)
    rhs = s if BARE_RE.match(s) else json.dumps(s)
    return f"{prefix}{name}={rhs}"


def to_lines(text: str) -> list[str]:
    if not text:
        return []
    lines = [line.removesuffix("\r") for line in text.split("\n")]
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def detect_eol(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def join_lines(lines: list[str]) -> str:
    if not lines:
        return ""
    return "\n".join(lines) + "\n"


def to_eol(text: str, eol: str) -> str:
    lf = text.replace("\r\n", "\n")
    return lf.replace("\n", "\r\n") if eol == "\r\n" else lf


def read_env_text(file: str | Path) -> str:
    try:
        # newline="" preserves raw \r\n so detect_eol sees the file's real line endings!
        with open(file, "r", encoding="utf-8", newline="") as f:
            return f.read()
    except OSError:
        return ""


def write_env_text(file: str | Path, text: str) -> None:
    path = Path(file)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.davag-tmp")
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def edit_env_file(file: str | Path, transform: Callable[[str], str]) -> dict[str, object]:
    path = Path(file)
    raw = read_env_text(path)
    eol = detect_eol(raw)
    after = transform(to_eol(raw, "\n"))
    out = to_eol(after, eol)
    changed = out != raw
    if changed:
        write_env_text(path, out)
    return {"file": str(path), "changed": changed, "text": after}


def set_env_vars(file: str | Path, updates: dict[str, str | None]) -> dict[str, object]:
    path = Path(file)
    names = list(updates.keys())
    if not names:
        return {"set": [], "removed": [], "file": str(path)}

    raw = read_env_text(path)
    eol = detect_eol(raw)
    lines = to_lines(raw)

    seen: dict[str, list[int]] = {}
    for idx, line in enumerate(lines):
        m = ASSIGN_RE.match(line)
        if not m or m.group(2) not in updates:
            continue
        seen.setdefault(m.group(2), []).append(idx)

    drop: set[int] = set()
    updated: list[str] = []
    removed: list[str] = []

    for name in names:
        value = updates[name]
        at = seen.get(name, [])
        for idx in at[:-1]:
            drop.add(idx)
        last = at[-1] if at else None

        if value is None:
            if last is not None:
                drop.add(last)
            if at:
                removed.append(name)
            continue

        if last is None:
            lines.append(format_assignment(name, str(value)))
        else:
            m = ASSIGN_RE.match(lines[last])
            prefix = m.group(1) if m else ""
            lines[last] = format_assignment(name, str(value), prefix)
        updated.append(name)

    kept = [line for idx, line in enumerate(lines) if idx not in drop]
    write_env_text(path, to_eol(join_lines(kept), eol))
    return {"set": updated, "removed": removed, "file": str(path)}