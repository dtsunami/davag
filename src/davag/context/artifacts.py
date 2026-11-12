"""Session artifact store for non-`dr_ops` fenced code blocks emitted by the model."""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

LANG_EXT: dict[str, str] = {
    "python": ".py",
    "py": ".py",
    "lua": ".lua",
    "json": ".json",
    "otio": ".otio",
    "xml": ".fcpxml",
    "fcpxml": ".fcpxml",
    "srt": ".srt",
    "markdown": ".md",
    "md": ".md",
    "sh": ".sh",
    "bash": ".sh",
}


@dataclass
class Artifact:
    id: int
    lang: str
    title: str
    body: str
    lines: int
    turn: int
    created_at: float

    def to_meta_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("body", None)
        d["suggested_filename"] = suggest_filename(self)
        return d


def suggest_filename(art: Artifact) -> str:
    ext = LANG_EXT.get(art.lang.lower(), ".txt")
    return f"artifact-{art.id}{ext}"


class ArtifactStore:
    def __init__(self) -> None:
        self._seq = 0
        self.items: list[Artifact] = []

    def add(self, *, lang: str, body: str, turn: int) -> Artifact:
        self._seq += 1
        first_line = next((ln.strip() for ln in body.splitlines() if ln.strip()), "")[:60]
        art = Artifact(
            id=self._seq,
            lang=lang or "text",
            title=first_line or f"{lang or 'text'} snippet",
            body=body,
            lines=len(body.splitlines()),
            turn=turn,
            created_at=time.time(),
        )
        self.items.append(art)
        return art

    def get(self, art_id: int) -> Artifact | None:
        return next((a for a in self.items if a.id == art_id), None)

    def save_to(self, art_id: int, dest: Path) -> Path:
        art = self.get(art_id)
        if art is None:
            raise KeyError(f"No artifact #{art_id}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(art.body, encoding="utf-8")
        return dest