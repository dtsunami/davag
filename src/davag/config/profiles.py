"""Provider blocks in a .env (`#---# NAME` ... `#-end-provider-#`).

Inactive blocks are ordinary single-`#` comments so every standard dotenv parser
agrees with davag on which variables are active. `##` marks prose comments that
are never uncommented. Pure text-in / text-out.
"""

from __future__ import annotations

import re
from typing import Any

from .envfile import format_assignment, join_lines, to_lines

SENTINEL_RE = re.compile(r"^([ \t]*)#-{3}#[ \t]*(\S+)[ \t]*(.*?)[ \t]*$")
END_TAG_RE = re.compile(r"^[ \t]*#-end-provider-#[ \t]*$", re.IGNORECASE)
IMMUNE_RE = re.compile(r"^[ \t]*##")
LIVE_RE = re.compile(r"^([ \t]*)((?:export[ \t]+)?)([A-Za-z_][A-Za-z0-9_]*)[ \t]*=(.*)$")
OFF_RE = re.compile(r"^([ \t]*)#[ \t]?((?:export[ \t]+)?)([A-Za-z_][A-Za-z0-9_]*)[ \t]*=(.*)$")

END_MARKER = "#-end-provider-#"
NAME_OK_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
VAR_OK_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def sentinel_for(name: str) -> str:
    return f"#---# {name}"


def validate_block_name(name: str) -> str:
    n = str(name or "").strip()
    if not NAME_OK_RE.match(n):
        raise ValueError(
            f'"{name}" is not a usable block name — letters, digits, dot, dash and underscore, '
            'no spaces (the header is a single "#---# NAME" line).'
        )
    if n.lower() == "end":
        raise ValueError('"end" is reserved: "#---# end" closes a block.')
    return n


def _classify(line: str) -> dict[str, Any] | None:
    if IMMUNE_RE.match(line):
        return None
    live = LIVE_RE.match(line)
    if live:
        return {
            "indent": live.group(1),
            "prefix": live.group(2),
            "name": live.group(3),
            "value": live.group(4).strip(),
            "enabled": True,
        }
    off = OFF_RE.match(line)
    if off:
        return {
            "indent": off.group(1),
            "prefix": off.group(2),
            "name": off.group(3),
            "value": off.group(4).strip(),
            "enabled": False,
        }
    return None


def _comment(line: str) -> str:
    m = re.match(r"^([ \t]*)(.*)$", line)
    assert m is not None
    return f"{m.group(1)}#{m.group(2)}"


def _uncomment(line: str) -> str:
    m = re.match(r"^([ \t]*)#[ \t]?(.*)$", line)
    return f"{m.group(1)}{m.group(2)}" if m else line


def _mark_shadowed(vars_list: list[dict[str, Any]]) -> None:
    chosen: dict[str, int] = {}
    for idx, v in enumerate(vars_list):
        prev = chosen.get(v["name"])
        if prev is None or v["enabled"] or not vars_list[prev]["enabled"]:
            chosen[v["name"]] = idx
    for idx, v in enumerate(vars_list):
        v["shadowed"] = chosen[v["name"]] != idx


def _choices_for(vars_list: list[dict[str, Any]], name: str) -> list[dict[str, Any]]:
    group = [v for v in vars_list if v["name"] == name]
    if len(group) < 2:
        return []
    return [
        {
            "value": v["value"],
            "line": v["line"] + 1,
            "enabled": v["enabled"],
            "chosen": not v["shadowed"],
        }
        for v in group
    ]


def parse_blocks(text: str) -> dict[str, Any]:
    lines = to_lines(text)
    blocks: list[dict[str, Any]] = []
    preamble: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None

    for i, line in enumerate(lines):
        sentinel = SENTINEL_RE.match(line)
        closes = bool(END_TAG_RE.match(line)) or (
            sentinel is not None and sentinel.group(2).lower() == "end"
        )
        if closes:
            if current is not None:
                current["end"] = i + 1
                current["closed"] = True
                current = None
            continue
        if sentinel is not None:
            if current is not None:
                current["end"] = i
            current = {
                "name": sentinel.group(2),
                "note": sentinel.group(3) or "",
                "indent": sentinel.group(1),
                "header": i,
                "end": len(lines),
                "closed": False,
                "vars": [],
            }
            blocks.append(current)
            continue
        v = _classify(line)
        if v is None:
            continue
        entry = {**v, "line": i}
        if current is not None:
            current["vars"].append(entry)
        else:
            preamble.append(entry)

    for b in blocks:
        _mark_shadowed(b["vars"])
        b["enabled"] = any(v["enabled"] and not v["shadowed"] for v in b["vars"])
    _mark_shadowed(preamble)
    return {"lines": lines, "blocks": blocks, "preamble": preamble}


def block_state(text: str) -> dict[str, Any]:
    parsed = parse_blocks(text)
    blocks = parsed["blocks"]
    preamble = parsed["preamble"]
    return {
        "blocks": [
            {
                "name": b["name"],
                "note": b["note"],
                "enabled": b["enabled"],
                "line": b["header"] + 1,
                "closed": b["closed"],
                "vars": [
                    {
                        "name": v["name"],
                        "value": v["value"],
                        "enabled": v["enabled"],
                        "line": v["line"] + 1,
                        "choices": _choices_for(b["vars"], v["name"]),
                    }
                    for v in b["vars"]
                    if not v["shadowed"]
                ],
                "shadowed": [v["name"] for v in b["vars"] if v["shadowed"]],
            }
            for b in blocks
        ],
        "active": [b["name"] for b in blocks if b["enabled"]],
        "preamble": [v["name"] for v in preamble if v["enabled"] and not v["shadowed"]],
    }


def block_values(text: str, name: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for b in parse_blocks(text)["blocks"]:
        if b["name"] != name:
            continue
        for v in b["vars"]:
            if not v["shadowed"]:
                out[v["name"]] = v["value"]
    return out


def enable_block(text: str, name: str | None) -> dict[str, Any]:
    parsed = parse_blocks(text)
    lines: list[str] = list(parsed["lines"])
    blocks: list[dict[str, Any]] = parsed["blocks"]
    want = None if name is None else validate_block_name(name)
    if want is not None and not any(b["name"] == want for b in blocks):
        have = ", ".join(b["name"] for b in blocks) or "(none)"
        raise ValueError(f'no provider block "{want}" in this file — have: {have}')

    enabled: list[str] = []
    disabled: list[str] = []
    for b in blocks:
        on = want is not None and b["name"] == want
        for v in b["vars"]:
            live = on and not v["shadowed"]
            if live and not v["enabled"]:
                lines[v["line"]] = _uncomment(lines[v["line"]])
                enabled.append(v["name"])
            elif not live and v["enabled"]:
                lines[v["line"]] = _comment(lines[v["line"]])
                disabled.append(v["name"])

    return {
        "text": join_lines(lines),
        "block": want,
        "enabled": enabled,
        "disabled": disabled,
    }


def _unquote(value: str | None) -> str:
    s = str(value or "").strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ('"', "'"):
        return s[1:-1]
    return s


def select_block_var(
    text: str,
    block_name: str,
    var_name: str,
    *,
    line: int | None = None,
    value: str | None = None,
) -> dict[str, Any]:
    block = validate_block_name(block_name)
    parsed = parse_blocks(text)
    lines: list[str] = list(parsed["lines"])
    matches = [b for b in parsed["blocks"] if b["name"] == block]
    if not matches:
        have = ", ".join(b["name"] for b in parsed["blocks"]) or "(none)"
        raise ValueError(f'no provider block "{block}" in this file — have: {have}')

    target = matches[-1]
    group = [v for v in target["vars"] if v["name"] == var_name]
    if not group:
        raise ValueError(f'block "{block}" does not set {var_name}')

    pick = None
    if line is not None:
        pick = next((v for v in group if v["line"] == int(line) - 1), None)
    else:
        want_val = _unquote(value)
        pick = next((v for v in group if _unquote(v["value"]) == want_val), None)

    if pick is None:
        have_vals = ", ".join(_unquote(v["value"]) for v in group)
        asked = f"on line {line}" if line is not None else f'"{_unquote(value)}"'
        raise ValueError(f'{var_name} in block "{block}" has no assignment {asked} — have: {have_vals}')

    live = bool(target["enabled"])
    for v in group:
        if v is not pick and v["enabled"]:
            lines[v["line"]] = _comment(lines[v["line"]])
    if live and not pick["enabled"]:
        lines[pick["line"]] = _uncomment(lines[pick["line"]])
    if not live and pick["enabled"]:
        lines[pick["line"]] = _comment(lines[pick["line"]])

    last_line = group[-1]["line"]
    if pick["line"] < last_line:
        start = pick["line"]
        while start - 1 > target["header"]:
            above = lines[start - 1]
            if not re.match(r"^[ \t]*#", above) or _classify(above) is not None:
                break
            start -= 1
        moved = lines[start : pick["line"] + 1]
        del lines[start : pick["line"] + 1]
        insert_at = last_line - len(moved) + 1
        lines[insert_at:insert_at] = moved

    return {
        "text": join_lines(lines),
        "block": block,
        "name": var_name,
        "value": _unquote(pick["value"]),
        "enabled": live,
    }


def _render(assignment: str, live: bool) -> str:
    return assignment if live else f"#{assignment}"


def set_block_var(
    text: str,
    block_name: str,
    name: str,
    value: str,
    *,
    live: bool | None = None,
) -> dict[str, Any]:
    block = validate_block_name(block_name)
    var_name = str(name or "")
    if not VAR_OK_RE.match(var_name):
        raise ValueError(f'"{name}" is not a valid environment variable name')

    parsed = parse_blocks(text)
    lines: list[str] = list(parsed["lines"])
    matches = [b for b in parsed["blocks"] if b["name"] == block]

    if not matches:
        is_live = live is True
        added: list[str] = []
        if lines and lines[-1].strip() != "":
            added.append("")
        added.append(sentinel_for(block))
        added.append(_render(format_assignment(var_name, value), is_live))
        added.append(END_MARKER)
        return {
            "text": join_lines([*lines, *added]),
            "block": block,
            "created": True,
            "enabled": is_live,
        }

    target = matches[-1]
    is_live = bool(target["enabled"]) if live is None else bool(live)
    hits = [v for v in target["vars"] if v["name"] == var_name]

    if hits:
        for v in hits[:-1]:
            if v["enabled"]:
                lines[v["line"]] = _comment(lines[v["line"]])
        last = hits[-1]
        rendered = _render(format_assignment(var_name, value, last["prefix"]), is_live)
        lines[last["line"]] = f'{last["indent"]}{rendered}'
    else:
        at = target["end"] - 1 if target["closed"] else target["end"]
        rendered = _render(format_assignment(var_name, value), is_live)
        lines.insert(at, f'{target["indent"]}{rendered}')

    return {
        "text": join_lines(lines),
        "block": block,
        "created": False,
        "enabled": is_live,
    }