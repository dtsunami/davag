"""Shared FastAPI application state (`WebState`) for the `davag` Web Sidecar."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, Request

from davag import __version__
from davag.session.bootstrap import Session


@dataclass
class WebState:
    session: Session
    submit_fn: Callable[[str, str], Any]
    secret: str
    bg_tasks: set[asyncio.Task[Any]] = field(default_factory=set)
    sse_queues: set[asyncio.Queue[dict[str, Any] | None]] = field(default_factory=set)
    shutdown_ev: asyncio.Event = field(default_factory=asyncio.Event)
    grab_files: dict[str, Path] = field(default_factory=dict)
    thumb_sem: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(4))
    tl_span_memo: dict[str, Any] = field(default_factory=dict)

    def describe_session(self) -> dict[str, Any]:
        cfg = self.session.cfg
        return {
            "version": __version__,
            "session_id": self.session.log.id,
            "cwd": str(cfg.cwd),
            "provider": cfg.provider,
            "model": cfg.model.id,
            "model_label": cfg.model.label,
            "video_capable": cfg.model.video,
            "effort": cfg.effort,
            "role": cfg.role,
            "yolo": cfg.yolo,
            "resolve_connected": self.session.transport is not None,
            "resolve_mode": self.session.transport.mode if self.session.transport else None,
            "stats": dict(self.session.agent.stats),
            "context": {
                "tokens": self.session.ledger.total_tokens,
                "limit": self.session.ledger.limit,
                "fraction": round(self.session.ledger.usage_fraction, 4),
            },
        }

    def register_media_file(self, p: Path) -> str:
        resolved = p.resolve()
        for existing_id, existing_p in self.grab_files.items():
            if existing_p == resolved:
                return f"/api/media/still/{existing_id}"
        mid = f"m{len(self.grab_files) + 1}"
        self.grab_files[mid] = resolved
        return f"/api/media/still/{mid}"


def get_state(req: Request) -> WebState:
    st: WebState = req.app.state.davag
    return st


StateDep = Annotated[WebState, Depends(get_state)]