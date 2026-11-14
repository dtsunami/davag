"""Bearer token generation, constant-time verification, and non-loopback `--yolo` guard (FastAPI/Starlette)."""

from __future__ import annotations

import hmac
import secrets
from typing import Any

from davag.cli import assert_safe_bind, is_loopback_host

__all__ = ["assert_safe_bind", "is_loopback_host", "make_web_token", "read_request_token", "token_matches"]


def make_web_token() -> str:
    return secrets.token_urlsafe(24)


def token_matches(expected: str, given: str | None) -> bool:
    if not expected or not given:
        return False
    return hmac.compare_digest(expected.encode("utf-8"), str(given).encode("utf-8"))


def read_request_token(req: Any, *, allow_query: bool = False) -> str | None:
    auth_hdr = str(req.headers.get("Authorization", "")).strip()
    if auth_hdr.lower().startswith("bearer "):
        return auth_hdr[7:].strip()
    if allow_query:
        q_params = getattr(req, "query_params", None) or getattr(req, "query", {})
        q_tok = str(q_params.get("token", "")).strip()
        if q_tok:
            return q_tok
    return None