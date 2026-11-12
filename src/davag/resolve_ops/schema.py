"""Operation registry, context wrapper, and safety tier metadata (stdlib only)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

SafetyTier = Literal["read", "mutate", "destructive", "eval"]


class OpError(Exception):
    """Structured error raised by any resolve_op when preconditions or Resolve calls fail."""

    def __init__(self, message: str, *, code: str = "OP_ERROR") -> None:
        super().__init__(message)
        self.code = code


@dataclass
class OpContext:
    """Runtime handles passed to every resolve_op handler."""

    resolve: Any
    fusion: Any = None
    bmd: Any = None
    mode: str = "direct"  # "direct" | "bridge"

    @property
    def project_manager(self) -> Any:
        pm = self.resolve.GetProjectManager()
        if pm is None:
            raise OpError("Resolve returned no ProjectManager", code="NO_PROJECT_MANAGER")
        return pm

    @property
    def project(self) -> Any:
        proj = self.project_manager.GetCurrentProject()
        if proj is None:
            raise OpError("No DaVinci Resolve project is currently open", code="NO_PROJECT")
        return proj

    @property
    def media_pool(self) -> Any:
        mp = self.project.GetMediaPool()
        if mp is None:
            raise OpError("Current project returned no MediaPool", code="NO_MEDIA_POOL")
        return mp

    @property
    def timeline(self) -> Any:
        tl = self.project.GetCurrentTimeline()
        if tl is None:
            raise OpError("No timeline is currently active in the open project", code="NO_TIMELINE")
        return tl


OpHandler = Callable[[OpContext, dict[str, Any]], dict[str, Any]]


@dataclass(frozen=True)
class OpSpec:
    name: str
    tier: SafetyTier
    summary: str
    handler: OpHandler
    params_doc: dict[str, str] = field(default_factory=dict)
    checkpoint_timeline: bool = False


OPS_REGISTRY: dict[str, OpSpec] = {}


def resolve_op(
    name: str,
    *,
    tier: SafetyTier = "read",
    summary: str = "",
    params_doc: dict[str, str] | None = None,
    checkpoint_timeline: bool = False,
) -> Callable[[OpHandler], OpHandler]:
    """Decorator registering a serializable Resolve operation (`dict` in -> `dict` out)."""

    def decorator(fn: OpHandler) -> OpHandler:
        OPS_REGISTRY[name] = OpSpec(
            name=name,
            tier=tier,
            summary=summary or (fn.__doc__ or "").strip().splitlines()[0],
            handler=fn,
            params_doc=dict(params_doc or {}),
            checkpoint_timeline=checkpoint_timeline,
        )
        return fn

    return decorator


def get_op(name: str) -> OpSpec | None:
    return OPS_REGISTRY.get(name)


def list_ops() -> list[dict[str, Any]]:
    return [
        {
            "name": spec.name,
            "tier": spec.tier,
            "summary": spec.summary,
            "params": spec.params_doc,
            "checkpoint_timeline": spec.checkpoint_timeline,
        }
        for spec in sorted(OPS_REGISTRY.values(), key=lambda s: s.name)
    ]


def dispatch_op(ctx: OpContext, op_name: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    spec = OPS_REGISTRY.get(op_name)
    if spec is None:
        available = ", ".join(sorted(OPS_REGISTRY))
        raise OpError(f'Unknown resolve op "{op_name}". Available: {available}', code="UNKNOWN_OP")
    return spec.handler(ctx, dict(params or {}))