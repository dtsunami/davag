"""Abstract ResolveTransport interface and `auto` connection negotiation."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from davag.bridge.protocol import read_discovery
from davag.config.resolve_paths import discover_resolve


class TransportConnectionError(Exception):
    """Raised when neither Bridge nor Direct IPC can reach a running DaVinci Resolve session."""


class ResolveTransport(ABC):
    """Unified async interface over Direct (`fusionscript.dll`) and Bridge (localhost JSON-RPC)."""

    mode: str

    @abstractmethod
    async def call(
        self, op_name: str, params: dict[str, Any] | None = None, *, timeout: float = 60.0
    ) -> dict[str, Any]:
        """Execute a registered `resolve_ops` operation and return its JSON-serializable dict."""

    @abstractmethod
    async def close(self) -> None:
        """Release underlying executor or socket resources."""


async def connect_resolve(
    *,
    mode: str = "auto",
    home: str | Path | None = None,
    bridge_host: str = "127.0.0.1",
    bridge_port: int = 18765,
    bridge_token: str | None = None,
) -> ResolveTransport:
    """Connect to DaVinci Resolve using `"auto"`, `"bridge"`, or `"direct"` mode.

    In `"auto"` mode:
    1. If `~/.davag/bridge.json` exists (or `bridge_token` is supplied), probe `BridgeTransport`
       first. If alive, return it immediately (honors explicit user bridge + fast bulk loops).
    2. Otherwise attempt `DirectTransport` (`fusionscript.dll` / `DaVinciResolveScript`).
    3. If both fail, raise `TransportConnectionError` with clear, actionable instructions.
    """
    from .bridge_client import BridgeTransport
    from .direct import DirectTransport

    want = mode.strip().lower()
    errors: list[str] = []

    if want in ("auto", "bridge"):
        disc = read_discovery(home)
        eff_host = disc.host if disc else bridge_host
        eff_port = disc.port if disc else bridge_port
        eff_token = bridge_token or (disc.token if disc else None)

        if eff_token:
            bt = BridgeTransport(host=eff_host, port=eff_port, token=eff_token)
            try:
                await bt.call("system.ping", timeout=2.0)
                return bt
            except Exception as err:  # noqa: BLE001
                await bt.close()
                errors.append(f"bridge ({eff_host}:{eff_port}): {err}")
        elif want == "bridge":
            errors.append(
                "bridge: no active ~/.davag/bridge.json found (run `davag --install-bridge` "
                "then click Workspace -> Scripts -> davag_bridge in Resolve)"
            )

    if want in ("auto", "direct"):
        dt = DirectTransport()
        try:
            await dt.connect()
            await dt.call("system.ping", timeout=5.0)
            return dt
        except Exception as err:  # noqa: BLE001
            await dt.close()
            errors.append(f"direct (fusionscript IPC): {err}")

    inst = discover_resolve()
    running_hint = (
        "DaVinci Resolve is running, "
        if inst.resolve_running
        else "DaVinci Resolve is NOT currently running — start Resolve first, otherwise "
    )
    guide = (
        f"Could not connect to DaVinci Resolve ({'; '.join(errors) or 'no transport available'}).\n"
        f"  • {running_hint}for Studio zero-click external scripting: open Resolve -> Preferences -> "
        "System -> General -> set 'External scripting using' to 'Local'.\n"
        "  • For DaVinci Resolve Free (or internal bridge mode): run `davag --install-bridge`, "
        "then in Resolve click Workspace -> Scripts -> Utility -> davag_bridge."
    )
    raise TransportConnectionError(guide)