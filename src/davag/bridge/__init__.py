"""Internal DaVinci Resolve TCP JSON-RPC bridge (100% Python stdlib only).

Runs inside Resolve via `Workspace -> Scripts -> davag_bridge` so both DaVinci Resolve
Free and DaVinci Resolve Studio can execute shared `davag.resolve_ops` on Resolve's main
script thread.
"""

from .protocol import (
    DEFAULT_BRIDGE_PORT,
    BridgeDiscovery,
    bridge_discovery_path,
    encode_frame,
    read_discovery,
    write_discovery,
)

__all__ = [
    "DEFAULT_BRIDGE_PORT",
    "BridgeDiscovery",
    "bridge_discovery_path",
    "encode_frame",
    "read_discovery",
    "write_discovery",
]