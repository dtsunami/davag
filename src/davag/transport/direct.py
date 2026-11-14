"""External Direct IPC transport via Blackmagic's `fusionscript.dll` / `DaVinciResolveScript`."""

from __future__ import annotations

import asyncio
import importlib
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from davag.config.resolve_paths import ResolveInstallation, discover_resolve
from davag.resolve_ops import OpContext, dispatch_op

from .base import ResolveTransport, TransportConnectionError


class DirectTransport(ResolveTransport):
    """Drives DaVinci Resolve Studio over `fusionscript` C++ IPC on a pinned worker thread."""

    mode = "direct"

    def __init__(
        self,
        *,
        resolve_obj: Any = None,
        fusion_obj: Any = None,
        bmd_obj: Any = None,
        installation: ResolveInstallation | None = None,
    ) -> None:
        self._injected_resolve = resolve_obj
        self._injected_fusion = fusion_obj
        self._injected_bmd = bmd_obj
        self._installation = installation
        # Single-thread executor keeps all fusionscript C++ proxy calls on the exact OS thread
        # that initialized `scriptapp("Resolve")`, while keeping asyncio completely non-blocking.
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="davag-fusionscript")
        self._ctx: OpContext | None = None

    def _connect_sync(self) -> OpContext:
        if self._ctx is not None:
            return self._ctx

        if self._injected_resolve is not None:
            fus = self._injected_fusion
            if fus is None and hasattr(self._injected_resolve, "Fusion"):
                fus = self._injected_resolve.Fusion()
            self._ctx = OpContext(
                resolve=self._injected_resolve,
                fusion=fus,
                bmd=self._injected_bmd,
                mode="direct",
            )
            return self._ctx

        inst = self._installation or discover_resolve()
        if not inst.direct_ready or not inst.script_lib_path or not inst.dvr_module_file:
            raise TransportConnectionError(
                "DaVinci Resolve scripting files (fusionscript / DaVinciResolveScript.py) were not found."
            )

        os.environ["RESOLVE_SCRIPT_LIB"] = inst.script_lib_path
        if inst.script_api_dir:
            os.environ["RESOLVE_SCRIPT_API"] = inst.script_api_dir
        if inst.script_modules_dir and inst.script_modules_dir not in sys.path:
            sys.path.insert(0, inst.script_modules_dir)

        # On Windows Python 3.8+, C extension DLLs (`fusionscript.dll`) also need their
        # parent directory registered in the DLL search path so dependent Blackmagic DLLs load.
        lib_dir = str(Path(inst.script_lib_path).parent)
        add_dll_dir = getattr(os, "add_dll_directory", None)
        if callable(add_dll_dir) and Path(lib_dir).is_dir():
            self._dll_dir_handle = add_dll_dir(lib_dir)

        # Blackmagic's DaVinciResolveScript.py replaces `sys.modules[__name__]` with the loaded
        # `fusionscript` C-extension module during `import`, so `importlib.import_module`
        # returns the real `fusionscript` module exposing `.scriptapp()`.
        try:
            dvr_mod = importlib.import_module("DaVinciResolveScript")
        except Exception as err:
            raise TransportConnectionError(f"Failed to import DaVinciResolveScript: {err}") from err

        scriptapp_fn = getattr(dvr_mod, "scriptapp", None)
        if not callable(scriptapp_fn):
            raise TransportConnectionError(
                "fusionscript.dll failed to load into Python (check RESOLVE_SCRIPT_LIB or use Bridge mode)."
            )

        resolve_handle = scriptapp_fn("Resolve")
        if resolve_handle is None:
            if not inst.resolve_running:
                raise TransportConnectionError("DaVinci Resolve is not running.")
            raise TransportConnectionError(
                "scriptapp('Resolve') returned None — either DaVinci Resolve is the Free edition "
                "(use Bridge mode) or 'External scripting using' is Off in Resolve Preferences -> System -> General."
            )

        bmd_mod = dvr_mod
        fusion_handle = resolve_handle.Fusion() if hasattr(resolve_handle, "Fusion") else None
        self._ctx = OpContext(resolve=resolve_handle, fusion=fusion_handle, bmd=bmd_mod, mode="direct")
        return self._ctx

    async def connect(self) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(self._executor, self._connect_sync)

    def _call_sync(self, op_name: str, params: dict[str, Any] | None) -> dict[str, Any]:
        ctx = self._connect_sync()
        if self._injected_resolve is None:
            try:
                # Verify C++ fusionscript handle is still alive (guards overnight sleep/Resolve restart)
                if ctx.resolve.GetProductName() is None:
                    self._ctx = None
                    ctx = self._connect_sync()
            except Exception:  # noqa: BLE001
                self._ctx = None
                ctx = self._connect_sync()
        return dispatch_op(ctx, op_name, params)

    async def call(
        self, op_name: str, params: dict[str, Any] | None = None, *, timeout: float = 60.0
    ) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        fut = loop.run_in_executor(self._executor, self._call_sync, op_name, params)
        return await asyncio.wait_for(fut, timeout=timeout)

    async def close(self) -> None:
        self._ctx = None
        self._executor.shutdown(wait=False, cancel_futures=True)