"""Sequenced Session Event Bus & Raced Approval Broker (mirrors `davai/src/session/bus.js`).

Three responsibilities:
1. Assign monotonically increasing `seq` IDs to every event in a bounded ring (`limit=2000`)
   so SSE clients reconnecting with `Last-Event-ID` get exact gap replay.
2. Project current live turn state (`busy`, `status`, `live`, `thinking`, `stats`) for late-joining
   browser tabs.
3. Broker `approval-request` across TUI and any number of browser tabs: stamps `req["id"]`,
   wraps `respond`, and broadcasts `approval-resolved` so whichever UI answers first clears
   the modal pane in every other UI immediately!
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from davag.agent.loop import Agent

FORWARD_EVENTS: tuple[str, ...] = (
    "thinking",
    "text",
    "turn-start",
    "turn-end",
    "ops-parsed",
    "ops-result",
    "artifact",
    "usage",
    "error",
    "warning",
    "done",
    "cancelled",
    "compact-start",
    "compact-done",
    "repair",
    "nudge",
    "retry",
    "steer-queued",
    "steered",
)


class SessionBus:
    def __init__(self, *, agent: Agent, limit: int = 2000) -> None:
        self.agent = agent
        self.limit = limit
        self.seq = 0
        self.ring: list[dict[str, Any]] = []
        self.dropped = 0
        self.pending: dict[str, dict[str, Any]] = {}
        self._next_approval = 1
        self._subscribers: list[Callable[[dict[str, Any]], None]] = []
        self._handlers: dict[str, Callable[[Any], None]] = {}

        self.state: dict[str, Any] = {
            "busy": False,
            "status": None,
            "live": "",
            "thinking": "",
            "stats": {},
        }
        self._attach()

    def _attach(self) -> None:
        for name in FORWARD_EVENTS:
            def _make_fwd(ev_name: str) -> Callable[[Any], None]:
                def _fn(payload: Any) -> None:
                    self.publish(ev_name, payload)
                _fn.passive = True  # type: ignore[attr-defined]
                return _fn

            fn = _make_fwd(name)
            self._handlers[name] = fn
            self.agent.on(name, fn)

        def _on_approval(req: dict[str, Any]) -> None:
            self._broker_approval(req)

        self._handlers["approval-request"] = _on_approval
        self.agent.on("approval-request", _on_approval)

    def _broker_approval(self, req: dict[str, Any]) -> None:
        ap_id = f"ap{self._next_approval}"
        self._next_approval += 1
        original_respond = req["respond"]
        settled = False

        def _wrapped(decision: dict[str, Any]) -> None:
            nonlocal settled
            if settled:
                return
            settled = True
            self.pending.pop(ap_id, None)
            self.publish("approval-resolved", {"id": ap_id, "decision": decision})
            original_respond(decision)

        req["respond"] = _wrapped
        req["id"] = ap_id
        self.pending[ap_id] = {"id": ap_id, "op": req["op"], "index": req["index"], "respond": _wrapped}
        self.publish("approval-request", {"id": ap_id, "op": req["op"], "index": req["index"]})

    def resolve_approval(self, ap_id: str, decision: dict[str, Any]) -> bool:
        entry = self.pending.get(ap_id)
        if entry is None:
            return False
        entry["respond"](decision)
        return True

    def subscribe(self, fn: Callable[[dict[str, Any]], None]) -> Callable[[], None]:
        self._subscribers.append(fn)

        def _unsub() -> None:
            if fn in self._subscribers:
                self._subscribers.remove(fn)

        return _unsub

    def publish(self, ev_type: str, data: Any) -> dict[str, Any]:
        self.seq += 1
        ev = {"seq": self.seq, "t": int(time.time() * 1000), "type": ev_type, "data": data}
        self._project(ev_type, data)
        self.ring.append(ev)
        while len(self.ring) > self.limit:
            self.ring.pop(0)
            self.dropped += 1
        for sub in list(self._subscribers):
            sub(ev)
        return ev

    def _project(self, ev_type: str, data: Any) -> None:
        s = self.state
        if ev_type == "turn-start":
            s["busy"] = True
            s["status"] = "thinking"
            s["live"] = ""
            s["thinking"] = ""
        elif ev_type == "thinking":
            s["thinking"] += str(data or "")
        elif ev_type == "text":
            if s["thinking"]:
                s["thinking"] = ""
            s["live"] += str(data or "")
        elif ev_type == "ops-parsed":
            n = len(data) if isinstance(data, list) else 1
            s["status"] = f"running {n} op{'s' if n != 1 else ''}"
        elif ev_type == "ops-result":
            s["status"] = None
        elif ev_type == "compact-start":
            s["status"] = "compacting context"
        elif ev_type in ("turn-end", "done", "cancelled", "error"):
            s["busy"] = False
            s["status"] = None
            s["live"] = ""
            s["thinking"] = ""
        s["stats"] = dict(self.agent.stats)

    def since(self, last_seq: int = 0) -> tuple[list[dict[str, Any]], bool]:
        first = self.ring[0]["seq"] if self.ring else (self.seq + 1)
        gap = last_seq > 0 and last_seq < first - 1
        events = [e for e in self.ring if e["seq"] > last_seq]
        return events, gap

    def snapshot(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "state": dict(self.state),
            "pending": [
                {"id": p["id"], "op": p["op"], "index": p["index"]}
                for p in self.pending.values()
            ],
        }

    def detach(self) -> None:
        for name, fn in self._handlers.items():
            self.agent.off(name, fn)
        self._handlers.clear()
        self._subscribers.clear()