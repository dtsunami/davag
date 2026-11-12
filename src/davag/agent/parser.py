"""Fenced `dr_ops` scanner, `--davag:NAME--` verbatim payload parser, and XML-leak recovery.

Mirrors `davai/src/agent/parser.js`:
- Extracts at most one `dr_ops` (also accepts `da_ops` alias) block per assistant turn.
- Preserves exact multi-line indentation in `--davag:NAME-- ... --davag:end--` payload sections.
- Recovers unclosed fences and serving-stack pseudo-XML tag leaks (`</rfl:parameter>`) cleanly,
  surfacing a warning rather than wasting repair turns.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from .executor import OpCall

FENCE_RE = re.compile(r"^([ \t]*)(`{3,}|~{3,})[ \t]*([^\s`]*)[ \t]*$")
PAYLOAD_OPEN_RE = re.compile(r"^--(?:davag|davai):([A-Za-z0-9_-]+)--[ \t]*$")
PAYLOAD_CLOSE_RE = re.compile(r"^--(?:davag|davai):end--[ \t]*$")
LONE_XML_TAG_RE = re.compile(r"^[ \t]*</?[A-Za-z_][A-Za-z0-9_.:-]*>[ \t]*$")


class ParseError(Exception):
    def __init__(self, message: str, detail: str = "") -> None:
        super().__init__(message)
        self.detail = detail


@dataclass
class FencedBlock:
    kind: str  # "dr_ops" | "artifact"
    lang: str
    body: str
    start_line: int
    unterminated: bool = False


@dataclass
class ExtractedTurn:
    ops: list[OpCall]
    artifacts: list[FencedBlock]
    prose: str
    recovered: str | None = None


def scan_blocks(text: str) -> tuple[list[FencedBlock], str]:
    lines = text.split("\n")
    blocks: list[FencedBlock] = []
    prose: list[str] = []

    i = 0
    n = len(lines)
    while i < n:
        m = FENCE_RE.match(lines[i])
        if not m:
            prose.append(lines[i])
            i += 1
            continue

        indent, fence, lang = m.group(1), m.group(2), m.group(3)
        closer_re = re.compile(rf"^[ \t]*{re.escape(fence[0])}{{{len(fence)},}}[ \t]*$")
        is_ops = lang.lower() in ("dr_ops", "da_ops")
        start = i
        body: list[str] = []
        i += 1
        closed = False
        in_payload = False

        while i < n:
            raw_ln = lines[i]
            ln = raw_ln[len(indent) :] if indent and raw_ln.startswith(indent) else raw_ln
            if is_ops:
                if not in_payload and PAYLOAD_OPEN_RE.match(ln):
                    in_payload = True
                elif in_payload and PAYLOAD_CLOSE_RE.match(ln):
                    in_payload = False
            if not in_payload and closer_re.match(raw_ln):
                closed = True
                i += 1
                break
            body.append(ln)
            i += 1

        blocks.append(
            FencedBlock(
                kind="dr_ops" if is_ops else "artifact",
                lang=lang or "text",
                body="\n".join(body),
                start_line=start,
                unterminated=not closed,
            )
        )

    return blocks, "\n".join(prose).strip()


def _split_payloads(body: str) -> tuple[str, dict[str, str]]:
    lines = body.split("\n")
    envelope_lines: list[str] = []
    payloads: dict[str, str] = {}

    cur_name: str | None = None
    cur_buf: list[str] = []

    for ln in lines:
        if cur_name is None:
            m_open = PAYLOAD_OPEN_RE.match(ln)
            if m_open:
                name = m_open.group(1)
                if name in payloads:
                    raise ParseError(f'Duplicate payload block "--davag:{name}--"')
                cur_name = name
                cur_buf = []
            elif PAYLOAD_CLOSE_RE.match(ln):
                raise ParseError('Stray "--davag:end--" without matching "--davag:NAME--"')
            else:
                envelope_lines.append(ln)
        else:
            if PAYLOAD_CLOSE_RE.match(ln):
                payloads[cur_name] = "\n".join(cur_buf)
                cur_name = None
                cur_buf = []
            else:
                cur_buf.append(ln)

    if cur_name is not None:
        raise ParseError(f'Unterminated payload block "--davag:{cur_name}--" (missing "--davag:end--")')

    return "\n".join(envelope_lines).strip(), payloads


def _resolve_refs(val: Any, payloads: dict[str, str], used: set[str]) -> Any:
    if isinstance(val, str) and val.startswith("@") and len(val) > 1:
        ref = val[1:]
        if ref in payloads:
            used.add(ref)
            return payloads[ref]
        return val
    if isinstance(val, list):
        return [_resolve_refs(item, payloads, used) for item in val]
    if isinstance(val, dict):
        return {k: _resolve_refs(v, payloads, used) for k, v in val.items()}
    return val


def parse_ops_body(body: str) -> tuple[list[OpCall], str | None]:
    envelope_str, payloads = _split_payloads(body)
    if not envelope_str:
        raise ParseError("Empty dr_ops JSON envelope")

    recovered: str | None = None
    try:
        envelope = json.loads(envelope_str)
    except json.JSONDecodeError as first_err:
        # Tolerant pass: strip lone serving-stack XML tags (`</rfl:parameter>`)
        cleaned = "\n".join(
            ln for ln in envelope_str.splitlines() if not LONE_XML_TAG_RE.match(ln)
        ).strip()
        try:
            envelope = json.loads(cleaned)
            recovered = "stripped stray XML tag"
        except json.JSONDecodeError:
            raise ParseError(f"Invalid JSON in dr_ops envelope: {first_err}") from first_err

    if not isinstance(envelope, dict) or not isinstance(envelope.get("ops"), list):
        raise ParseError('dr_ops envelope must be a JSON object {"ops": [...]}')

    used: set[str] = set()
    calls: list[OpCall] = []
    for idx, raw_op in enumerate(envelope["ops"]):
        if not isinstance(raw_op, dict) or not raw_op.get("op"):
            raise ParseError(f"ops[{idx}] must be an object with a non-empty 'op' string")
        resolved = _resolve_refs(raw_op, payloads, used)
        op_name = str(resolved.pop("op")).strip()
        calls.append(OpCall(index=idx, op=op_name, params=resolved))

    unused = set(payloads.keys()) - used
    if unused:
        names = ", ".join(sorted(unused))
        raise ParseError(f"Unreferenced payload block(s): {names} (reference via '@NAME')")

    return calls, recovered


def extract_turn(text: str) -> ExtractedTurn:
    blocks, prose = scan_blocks(text)
    ops_blocks = [b for b in blocks if b.kind == "dr_ops"]
    artifacts = [b for b in blocks if b.kind == "artifact"]

    if len(ops_blocks) > 1:
        raise ParseError(
            f"Found {len(ops_blocks)} dr_ops blocks in one turn; emit at most ONE dr_ops batch per turn."
        )
    if not ops_blocks:
        return ExtractedTurn(ops=[], artifacts=artifacts, prose=prose)

    blk = ops_blocks[0]
    calls, recovered = parse_ops_body(blk.body)
    if blk.unterminated:
        recovered = f"{recovered}; no closing fence" if recovered else "no closing fence"

    return ExtractedTurn(ops=calls, artifacts=artifacts, prose=prose, recovered=recovered)