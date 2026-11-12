"""Multi-line paste folding (`[[paste#N: K lines]]`) and submit-time expansion."""

from __future__ import annotations

import re
from dataclasses import dataclass

PASTE_TOKEN_RE = re.compile(r"\[\[paste#(\d+)(?::\s*\d+\s*lines)?\]\]")


@dataclass
class PasteItem:
    n: int
    lines: int
    text: str


class PasteStore:
    def __init__(self) -> None:
        self._seq = 0
        self._items: dict[int, PasteItem] = {}

    def add(self, text: str) -> tuple[str, PasteItem]:
        self._seq += 1
        line_count = max(1, len(text.splitlines()))
        item = PasteItem(n=self._seq, lines=line_count, text=text)
        self._items[item.n] = item
        return f"[[paste#{item.n}: {item.lines} lines]]", item

    def expand(self, prompt: str) -> tuple[str, list[PasteItem]]:
        referenced: list[PasteItem] = []
        seen: set[int] = set()

        def _repl(m: re.Match[str]) -> str:
            num = int(m.group(1))
            item = self._items.get(num)
            if item is None:
                return m.group(0)
            if num not in seen:
                seen.add(num)
                referenced.append(item)
            return f"[paste#{item.n}]"

        return PASTE_TOKEN_RE.sub(_repl, prompt), referenced