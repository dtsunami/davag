"""Undo journal covering BOTH workspace files and DaVinci Resolve timeline checkpoints.

Why Resolve needs timeline checkpoints:
- Resolve's C++ scripting API (`AppendToTimeline`, `DeleteClips`, `SetProperty`, `SetInput`)
  mutates the live Resolve database immediately and often cannot be undone cleanly via Ctrl+Z.
- Before any mutating `dr_ops` batch touches the active timeline, `Journal.create_resolve_checkpoint`
  duplicates the active timeline to `.davag-snap-<seq>-<name>`.
- If the batch fails mid-way (or when the operator types `/undo`), `Journal.undo_last_resolve()`
  restores the snapshot timeline back as the active timeline under its original name!
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import stat
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from davag.transport.base import ResolveTransport


@dataclass
class FileEntry:
    target: str
    existed: bool
    snap_path: str | None
    mode: int | None
    ts: float


@dataclass
class ResolveCheckpoint:
    seq: int
    original_name: str
    original_id: str
    snapshot_name: str
    snapshot_id: str
    ts: float


def _unlock_readonly_win(path: Path) -> None:
    try:
        st = path.stat()
        if not (st.st_mode & stat.S_IWRITE):
            os.chmod(path, st.st_mode | stat.S_IWRITE)
    except OSError:
        pass


class Journal:
    def __init__(self, session_dir: str | Path) -> None:
        self.session_dir = Path(session_dir)
        self.snaps_dir = self.session_dir / "snaps"
        self.journal_file = self.session_dir / "journal.jsonl"
        self.snaps_dir.mkdir(parents=True, exist_ok=True)
        self._seq = 0
        self.resolve_checkpoints: list[ResolveCheckpoint] = []

    def _append_log(self, kind: str, payload: dict[str, Any]) -> None:
        self.session_dir.mkdir(parents=True, exist_ok=True)
        with open(self.journal_file, "a", encoding="utf-8") as f:
            f.write(json.dumps({"kind": kind, **payload}) + "\n")

    def snapshot_file(self, target: Path) -> FileEntry:
        self._seq += 1
        existed = target.is_file()
        snap_path: str | None = None
        mode: int | None = None

        if existed:
            st = target.stat()
            mode = st.st_mode
            snap = self.snaps_dir / f"f-{self._seq:04d}-{target.name}"
            shutil.copy2(target, snap)
            snap_path = str(snap)

        entry = FileEntry(
            target=str(target),
            existed=existed,
            snap_path=snap_path,
            mode=mode,
            ts=time.time(),
        )
        self._append_log("file_snapshot", asdict(entry))
        return entry

    def rollback_files(self, entries: list[FileEntry]) -> list[str]:
        failures: list[str] = []
        for entry in reversed(entries):
            target = Path(entry.target)
            try:
                if not entry.existed:
                    if target.exists():
                        _unlock_readonly_win(target)
                        target.unlink()
                elif entry.snap_path:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if target.exists():
                        _unlock_readonly_win(target)
                    shutil.copy2(entry.snap_path, target)
                    if entry.mode is not None:
                        os.chmod(target, entry.mode)
            except OSError as err:
                failures.append(f"{target}: {err}")
        return failures

    async def create_resolve_checkpoint(self, transport: ResolveTransport) -> ResolveCheckpoint | None:
        """Duplicate the active Resolve timeline as a hidden `.davag-snap-*` checkpoint."""
        status = await transport.call("resolve.status")
        tl_info = status.get("timeline")
        if not tl_info or not tl_info.get("name"):
            return None

        self._seq += 1
        orig_name = str(tl_info["name"])
        snap_name = f".davag-snap-{self._seq:03d}-{orig_name}"[:60]
        dup = await transport.call(
            "timeline.duplicate",
            {"name": snap_name, "switch_to_duplicate": False},
        )
        cp = ResolveCheckpoint(
            seq=self._seq,
            original_name=orig_name,
            original_id=str(dup["original_id"]),
            snapshot_name=str(dup["duplicate_name"]),
            snapshot_id=str(dup["duplicate_id"]),
            ts=time.time(),
        )
        self.resolve_checkpoints.append(cp)
        self._append_log("resolve_checkpoint", asdict(cp))
        return cp

    async def undo_last_resolve(self, transport: ResolveTransport) -> dict[str, Any]:
        """Restore the most recent Resolve timeline checkpoint."""
        if not self.resolve_checkpoints:
            raise RuntimeError("No Resolve timeline checkpoints available to undo in this session.")

        cp = self.resolve_checkpoints.pop()
        # 1. Switch active timeline to the clean snapshot first
        await transport.call("timeline.select", {"timeline_id": cp.snapshot_id})
        # 2. Delete the mutated timeline so we can reclaim its exact original name
        with contextlib.suppress(Exception):
            await transport.call("timeline.delete_timeline", {"timeline_id": cp.original_id})
        # 3. Duplicate the snapshot back under the original name and delete the temp snapshot
        restored = await transport.call(
            "timeline.duplicate",
            {"name": cp.original_name, "switch_to_duplicate": True},
        )
        with contextlib.suppress(Exception):
            await transport.call("timeline.delete_timeline", {"timeline_id": cp.snapshot_id})

        self._append_log("resolve_undo", {"restored": cp.original_name, "from": cp.snapshot_name})
        return {
            "restored_timeline": cp.original_name,
            "checkpoint_seq": cp.seq,
            "new_timeline_id": restored.get("duplicate_id"),
        }