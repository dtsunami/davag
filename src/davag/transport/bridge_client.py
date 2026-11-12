"""Async TCP JSON-RPC client talking to `davag.bridge.server` inside DaVinci Resolve."""

from __future__ import annotations

import asyncio
from typing import Any

from davag.bridge.protocol import MAX_LINE_BYTES, decode_frame, encode_frame
from davag.resolve_ops import OpError

from .base import ResolveTransport, TransportConnectionError


class BridgeTransport(ResolveTransport):
    """Connects over localhost TCP to the stdlib bridge running inside Resolve's `fuscript`."""

    mode = "bridge"

    def __init__(self, *, host: str = "127.0.0.1", port: int = 18765, token: str) -> None:
        self.host = host
        self.port = int(port)
        self.token = token
        self._seq = 0
        self._lock = asyncio.Lock()
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None

    async def _ensure_open(self, timeout: float) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        if self._reader is not None and self._writer is not None and not self._writer.is_closing():
            return self._reader, self._writer
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port, limit=MAX_LINE_BYTES),
                timeout=min(timeout, 5.0),
            )
        except OSError as err:
            raise TransportConnectionError(
                f"Cannot connect to davag Bridge at {self.host}:{self.port} ({err})"
            ) from err
        self._reader = reader
        self._writer = writer
        return reader, writer

    async def _reset_socket(self) -> None:
        writer = self._writer
        self._reader = None
        self._writer = None
        if writer is not None:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass

    async def call(
        self, op_name: str, params: dict[str, Any] | None = None, *, timeout: float = 60.0
    ) -> dict[str, Any]:
        async with self._lock:
            self._seq += 1
            req_id = self._seq
            frame = encode_frame(
                {
                    "id": req_id,
                    "token": self.token,
                    "op": op_name,
                    "params": dict(params or {}),
                    "timeout": timeout,
                }
            )
            reader, writer = await self._ensure_open(timeout)
            try:
                writer.write(frame)
                await asyncio.wait_for(writer.drain(), timeout=timeout)
                raw = await asyncio.wait_for(reader.readline(), timeout=timeout)
            except (TimeoutError, OSError) as err:
                await self._reset_socket()
                raise TransportConnectionError(f'Bridge RPC "{op_name}" failed: {err}') from err

            if not raw:
                await self._reset_socket()
                raise TransportConnectionError("Bridge closed the connection unexpectedly")

            resp = decode_frame(raw)
            if not resp.get("ok"):
                err_obj = resp.get("error") or {}
                code = str(err_obj.get("code", "OP_ERROR"))
                msg = str(err_obj.get("message", "Unknown bridge error"))
                if code == "UNAUTHORIZED":
                    await self._reset_socket()
                    raise TransportConnectionError(f"Bridge rejected token ({msg})")
                raise OpError(msg, code=code)

            result = resp.get("result")
            return result if isinstance(result, dict) else {"value": result}

    async def close(self) -> None:
        async with self._lock:
            await self._reset_socket()