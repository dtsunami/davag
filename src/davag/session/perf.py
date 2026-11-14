"""Per-second tok/s sparkline buckets, TTFT, and model throughput vs contextIn telemetry."""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any

SPARK_CHARS = "▁▂▃▄▅▆▇█"


@dataclass
class TurnPerf:
    turn: int
    model: str
    effort: str
    ttft: float | None
    elapsed: float
    out_tokens: int
    context_in: int
    tps: float
    buckets: list[float] = field(default_factory=list)


class PerfTracker:
    def __init__(self) -> None:
        self.turns: list[TurnPerf] = []
        self._active_t0: float | None = None
        self._active_model: str = ""
        self._active_effort: str = ""
        self._active_ttft: float | None = None
        self._char_buckets: dict[int, int] = {}
        self._total_chars = 0

    def on_turn_start(self, *, model: str, effort: str) -> None:
        self._active_t0 = time.perf_counter()
        self._active_model = model
        self._active_effort = effort
        self._active_ttft = None
        self._char_buckets = {}
        self._total_chars = 0

    def on_first_token(self, ttft: float) -> None:
        self._active_ttft = ttft

    def on_chars(self, count: int) -> None:
        if self._active_t0 is None or count <= 0:
            return
        sec_slot = int(time.perf_counter() - self._active_t0)
        self._char_buckets[sec_slot] = self._char_buckets.get(sec_slot, 0) + count
        self._total_chars += count

    def on_turn_end(
        self,
        *,
        elapsed: float,
        out_tokens: int,
        context_in: int,
        effort: str,
    ) -> TurnPerf:
        est_toks = out_tokens if out_tokens > 0 else round(self._total_chars / 3.8)
        scale = (est_toks / self._total_chars) if self._total_chars > 0 else 0.25
        max_slot = max(self._char_buckets.keys(), default=0)
        buckets = [round(self._char_buckets.get(s, 0) * scale, 2) for s in range(max_slot + 1)]

        vis_est = round(self._total_chars / 3.8)
        # If internal unstreamed thinking tokens accounted for most of `out_tokens` during `ttft`,
        # measure over `elapsed` rather than dividing hidden thinking tokens by a 0.05s text tail!
        if self._active_ttft and self._active_ttft > 0.7 * elapsed and est_toks > max(64, vis_est * 2):
            gen_window = max(0.05, elapsed)
        else:
            gen_window = max(0.05, elapsed - (self._active_ttft or 0.0))
        tps = round(est_toks / gen_window, 1) if est_toks > 0 else 0.0

        rec = TurnPerf(
            turn=len(self.turns) + 1,
            model=self._active_model,
            effort=effort,
            ttft=round(self._active_ttft, 3) if self._active_ttft is not None else None,
            elapsed=round(elapsed, 3),
            out_tokens=est_toks,
            context_in=context_in,
            tps=tps,
            buckets=buckets,
        )
        self.turns.append(rec)
        self._active_t0 = None
        return rec

    @property
    def live_tps(self) -> float:
        if self._active_t0 is not None and self._total_chars > 0:
            gen_t = max(0.1, (time.perf_counter() - self._active_t0) - (self._active_ttft or 0.0))
            return round((self._total_chars / 3.8) / gen_t, 1)
        return self.turns[-1].tps if self.turns else 0.0

    def sparkline(self, width: int = 6) -> str:
        vals = [t.tps for t in self.turns[-width:]]
        if not vals:
            return ""
        hi = max(vals) or 1.0
        out = []
        for v in vals:
            idx = min(len(SPARK_CHARS) - 1, max(0, int((v / hi) * (len(SPARK_CHARS) - 1))))
            out.append(SPARK_CHARS[idx])
        return "".join(out)

    def to_dict(self) -> dict[str, Any]:
        return {
            "live_tps": self.live_tps,
            "sparkline": self.sparkline(10),
            "turns": [asdict(t) for t in self.turns[-100:]],
        }