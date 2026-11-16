"""UTF-8 / Win32 UTF-16 surrogate sanitizer (`davag.context.utf8`).

On Windows consoles (`ReadConsoleInputW`), pasted astral codepoints (e.g. U+1F4AD)
can arrive as UTF-16 surrogate pairs (`0xD83D 0xDCAD`) inside Python `str` objects, or
lone surrogates from truncated JSON escapes. Rust `pydantic_core.to_json()` and
`str.encode('utf-8')` reject surrogates with `UnicodeEncodeError: surrogates not allowed`.

`sanitize_utf8_str` is a zero-allocation no-op when no surrogates are present, losslessly
recombines valid UTF-16 surrogate pairs into real astral Unicode characters, and
replaces any lone unpaired surrogate with `U+FFFD`.
"""

from __future__ import annotations

import re
from typing import Any

_SURROGATE_RE = re.compile("[" + chr(0xD800) + "-" + chr(0xDFFF) + "]")


def sanitize_utf8_str(s: str) -> str:
    if not s or _SURROGATE_RE.search(s) is None:
        return s
    return s.encode("utf-16", "surrogatepass").decode("utf-16", "replace")


def sanitize_utf8(obj: Any) -> Any:
    if isinstance(obj, str):
        return sanitize_utf8_str(obj)
    if isinstance(obj, list):
        return [sanitize_utf8(x) for x in obj]
    if isinstance(obj, tuple):
        return [sanitize_utf8(x) for x in obj]
    if isinstance(obj, dict):
        return {
            (sanitize_utf8_str(k) if isinstance(k, str) else k): sanitize_utf8(v)
            for k, v in obj.items()
        }
    return obj
