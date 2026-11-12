"""Fork provenance & `.history` snapshots for overridden prompts (`davai/src/agent/promptfork.js`)."""

from __future__ import annotations

import datetime
import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

from davag import __version__

from .prompt import PROMPT_SPECS, builtin_prompts

MANIFEST_FILE = ".davag-prompts.json"
HISTORY_DIR = ".history"


def hash_text(text: str) -> str:
    return hashlib.sha256(str(text or "").replace("\r\n", "\n").encode("utf-8")).hexdigest()[:12]


def manifest_path(directory: Path) -> Path:
    return directory / MANIFEST_FILE


def read_manifest(directory: Path) -> dict[str, Any]:
    try:
        parsed = json.loads(manifest_path(directory).read_text(encoding="utf-8"))
        if isinstance(parsed, dict):
            return {"version": 1, "prompts": {}, **parsed}
    except (OSError, ValueError):
        pass
    return {"version": 1, "prompts": {}}


def write_manifest(directory: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    directory.mkdir(parents=True, exist_ok=True)
    manifest_path(directory).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def stamp_fork(directory: Path, keys: list[str], *, version: str = __version__) -> dict[str, Any]:
    manifest = read_manifest(directory)
    defaults = builtin_prompts()
    stamped_at = datetime.datetime.now(tz=datetime.UTC).isoformat()
    prompts_map = manifest.setdefault("prompts", {})
    for key in keys:
        if key not in defaults:
            continue
        prompts_map[key] = {
            "davagVersion": version,
            "defaultHash": hash_text(defaults[key]),
            "stampedAt": stamped_at,
        }
    return write_manifest(directory, manifest)


def snapshot_prompt_file(file_path: Path) -> str | None:
    if not file_path.is_file():
        return None
    hist_dir = file_path.parent / HISTORY_DIR
    hist_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now(tz=datetime.UTC).strftime("%Y-%m-%dT%H-%M-%SZ")
    dest = hist_dir / f"{file_path.name}.{stamp}"
    shutil.copy2(file_path, dest)
    return str(dest)


def check_forks(
    *, sources: dict[str, str], layers: dict[str, str]
) -> dict[str, list[dict[str, Any]]]:
    defaults = builtin_prompts()
    stale: list[dict[str, Any]] = []
    unstamped: list[dict[str, Any]] = []

    for spec in PROMPT_SPECS:
        layer = layers.get(spec.key, "default")
        if layer == "default":
            continue
        file_str = sources.get(spec.key)
        if not file_str:
            continue
        f_path = Path(file_str)
        entry = read_manifest(f_path.parent).get("prompts", {}).get(spec.key)
        cur_hash = hash_text(defaults[spec.key])

        if not entry or not entry.get("defaultHash"):
            unstamped.append(
                {"key": spec.key, "file": file_str, "dir": str(f_path.parent), "layer": layer, "currentHash": cur_hash}
            )
        elif entry["defaultHash"] != cur_hash:
            stale.append(
                {
                    "key": spec.key,
                    "file": file_str,
                    "dir": str(f_path.parent),
                    "layer": layer,
                    "forkedHash": entry["defaultHash"],
                    "currentHash": cur_hash,
                    "forkedVersion": entry.get("davagVersion", "unknown"),
                    "currentVersion": __version__,
                    "stampedAt": entry.get("stampedAt", ""),
                }
            )
    return {"stale": stale, "unstamped": unstamped}