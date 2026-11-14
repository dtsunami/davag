"""Cross-session prompt history (`~/.davag/history.jsonl`), capped at 500 prompts."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

MAX_HISTORY = 500


class PromptHistory:
    def __init__(self, home: Path) -> None:
        self.path = home / "history.jsonl"
        self.entries: list[dict[str, Any]] = self._load()

    def _load(self) -> list[dict[str, Any]]:
        if not self.path.is_file():
            return []
        items: list[dict[str, Any]] = []
        try:
            for line in self.path.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.strip():
                    items.append(json.loads(line))
        except (OSError, ValueError):
            return items[-MAX_HISTORY:]
        return items[-MAX_HISTORY:]

    def add(self, text: str) -> None:
        cleaned = text.strip()
        if not cleaned or cleaned.startswith(("/", "sh ")) or cleaned == "sh":
            return
        if self.entries and self.entries[-1].get("text") == cleaned:
            return
        entry = {"t": time.time(), "text": cleaned}
        self.entries.append(entry)
        if len(self.entries) > MAX_HISTORY:
            self.entries = self.entries[-MAX_HISTORY:]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = "\n".join(json.dumps(e, ensure_ascii=False) for e in self.entries) + "\n"
        self.path.write_text(payload, encoding="utf-8")

    def recent(self, n: int = 15) -> list[dict[str, Any]]:
        return self.entries[-max(1, n) :]

    def strings(self) -> list[str]:
        return [str(e.get("text", "")) for e in self.entries if e.get("text")]