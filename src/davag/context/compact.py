"""Context auto-compaction policy.

Follows davai's eviction hierarchy so the cached prefix (`system`) is never busted:
1. Evict older unpinned `media` attachments (stills/videos) and bulky `op-result` dumps first,
   replacing them with a 1-line placeholder so turn alternation stays valid and re-queryable.
2. Truncate oversized `shell` / `paste` segments.
3. If still above target, condense the oldest unpinned conversation slice into a `summary` segment.
"""

from __future__ import annotations

from typing import Any

from .ledger import PROTECTED_TYPES, Ledger
from .tokens import estimate_text_tokens


async def compact_ledger(
    ledger: Ledger,
    *,
    target_fraction: float = 0.50,
    summarizer: Any = None,
) -> dict[str, Any]:
    async def _run() -> dict[str, Any]:
        before_tokens = ledger.total_tokens
        evicted = 0
        truncated = 0

        # Keep the 4 most recent segments untouched so the active step never loses its immediate results
        compressible = ledger.segments[:-4] if len(ledger.segments) > 4 else []

        # Pass 1: Evict stale media attachments & bulky op-result JSON payloads
        for seg in compressible:
            if ledger.usage_fraction <= target_fraction:
                break
            if seg.pinned or seg.type in PROTECTED_TYPES:
                continue
            if seg.type in ("media", "op-result") and seg.tokens > 250:
                note = f"[compacted {seg.type}: {seg.label} — re-run op if needed]"
                seg.part = {"type": "text", "text": note}
                seg.tokens = estimate_text_tokens(note)
                evicted += 1

        # Pass 2: Truncate large shell/paste blocks
        for seg in compressible:
            if ledger.usage_fraction <= target_fraction:
                break
            if seg.pinned or seg.type in PROTECTED_TYPES:
                continue
            if seg.type in ("shell", "paste") and seg.part.get("type") == "text" and seg.tokens > 500:
                txt = str(seg.part.get("text", ""))
                clipped = txt[:800] + f"\n… [truncated {len(txt) - 800} chars during compaction]"
                seg.part = {"type": "text", "text": clipped}
                seg.tokens = estimate_text_tokens(clipped)
                truncated += 1

        # Pass 3: Condense oldest unpinned turns if still above target
        summarized = 0
        if ledger.usage_fraction > target_fraction and len(ledger.segments) > 8:
            candidates = [
                i
                for i, s in enumerate(ledger.segments[:-6])
                if not s.pinned and s.type not in PROTECTED_TYPES
            ]
            if len(candidates) >= 2:
                slice_segs = [ledger.segments[i] for i in candidates[: min(8, len(candidates))]]
                transcript_text = "\n\n".join(
                    f"[{s.role.upper()} - {s.label}]\n{s.part.get('text', '[media]')[:1500]}"
                    for s in slice_segs
                )
                summary_body: str
                if callable(summarizer):
                    summary_body = await summarizer(transcript_text)
                else:
                    lines = [f"- {s.role}: {s.label}" for s in slice_segs]
                    summary_body = "[Earlier session summary]\n" + "\n".join(lines)

                drop_ids = {s.id for s in slice_segs}
                first_idx = next(i for i, s in enumerate(ledger.segments) if s.id in drop_ids)
                ledger.segments = [s for s in ledger.segments if s.id not in drop_ids]
                seg = ledger.add(
                    seg_type="summary",
                    label=f"summary ({len(slice_segs)} segments)",
                    role="user",
                    text=summary_body,
                )
                # Move newly created summary back into the historical slot
                ledger.segments.pop()
                ledger.segments.insert(first_idx, seg)
                summarized = len(slice_segs)

        after_tokens = ledger.total_tokens
        return {
            "before_tokens": before_tokens,
            "after_tokens": after_tokens,
            "freed_tokens": max(0, before_tokens - after_tokens),
            "evicted": evicted,
            "truncated": truncated,
            "summarized": summarized,
        }

    return await ledger.with_compaction_lock(_run)