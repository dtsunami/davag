"""HTTP + Server-Sent Events (SSE) Web GUI sidecar server (`davag --web` / `--host`)."""

from .auth import make_web_token, token_matches
from .http import RunningWebServer, start_web_server

__all__ = ["RunningWebServer", "make_web_token", "start_web_server", "token_matches"]