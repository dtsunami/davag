"""Wire framing and discovery metadata for the localhost TCP bridge (stdlib only).

Uses newline-delimited UTF-8 JSON messages (`1 line = 1 JSON envelope`) with a constant-time
shared token check so untrusted local browser tabs or processes cannot drive Resolve.
"""

from __future__ import annotations

import hmac
import json
import os
import secrets
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

DEFAULT_BRIDGE_HOST = "127.0.0.1"
DEFAULT_BRIDGE_PORT = 18765
MAX_LINE_BYTES = 16 * 1024 * 1024  # 16 MB max single RPC frame


@dataclass(frozen=True)
class BridgeDiscovery:
    host: str
    port: int
    token: str
    pid: int
    started_at: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _resolve_davag_home() -> Path:
    raw = os.environ.get("DAVAG_HOME", "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return (Path.home() / ".davag").resolve()


def bridge_discovery_path(home: str | Path | None = None) -> Path:
    base = Path(home).resolve() if home else _resolve_davag_home()
    return base / "bridge.json"


def generate_bridge_token() -> str:
    return secrets.token_hex(24)


def verify_bridge_token(expected: str, given: str | None) -> bool:
    if not expected or not given:
        return False
    return hmac.compare_digest(expected.encode("utf-8"), str(given).encode("utf-8"))


def write_discovery(info: BridgeDiscovery, home: str | Path | None = None) -> Path:
    path = bridge_discovery_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_text(json.dumps(info.to_dict(), indent=2) + "\n", encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)
    return path


def read_discovery(home: str | Path | None = None) -> BridgeDiscovery | None:
    path = bridge_discovery_path(home)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return BridgeDiscovery(
            host=str(data["host"]),
            port=int(data["port"]),
            token=str(data["token"]),
            pid=int(data["pid"]),
            started_at=float(data["started_at"]),
        )
    except (OSError, ValueError, KeyError, TypeError):
        return None


def remove_discovery_if_owned(pid: int, home: str | Path | None = None) -> None:
    path = bridge_discovery_path(home)
    current = read_discovery(home)
    if current and current.pid == pid:
        try:
            path.unlink()
        except OSError:
            pass


def encode_frame(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")


def decode_frame(line: bytes) -> dict[str, Any]:
    if len(line) > MAX_LINE_BYTES:
        raise ValueError(f"Bridge frame exceeds {MAX_LINE_BYTES} bytes")
    obj = json.loads(line.decode("utf-8"))
    if not isinstance(obj, dict):
        raise TypeError("Bridge frame must be a JSON object")
    return obj