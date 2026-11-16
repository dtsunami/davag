"""Wires Config, ResolveTransport, Grounding, Ledger, Sandbox, Journal, Agent, and SessionBus."""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from davag.agent.grounding import build_grounding_text, snapshot_resolve_grounding
from davag.agent.journal import Journal
from davag.agent.loop import Agent
from davag.agent.prompt import build_system_prompt, resolve_prompts_layered
from davag.agent.promptfork import check_forks
from davag.agent.sandbox import Sandbox
from davag.config import Config, load_config, load_settings, resolve_home, settings_to_defaults
from davag.context.artifacts import ArtifactStore
from davag.context.ledger import Ledger
from davag.context.pastes import PasteStore
from davag.providers import ProviderAdapter, create_provider
from davag.transport import ResolveTransport, TransportConnectionError, connect_resolve

from .bus import SessionBus
from .history import PromptHistory
from .log import SessionLog
from .perf import PerfTracker
from .promptedit import apply_staged_prompts
from .resume import find_session_dir, resolve_lineage, restore_ledger_from_lines


@dataclass
class Session:
    cfg: Config
    prompts: dict[str, str]
    prompt_sources: dict[str, str]
    prompt_layers: dict[str, str]
    ledger: Ledger
    sandbox: Sandbox
    journal: Journal
    artifacts: ArtifactStore
    pastes: PasteStore
    history: PromptHistory
    perf: PerfTracker
    log: SessionLog
    agent: Agent
    bus: SessionBus
    transport: ResolveTransport | None
    resolve_grounding_snapshot: str = ""
    resolve_connect_note: str | None = None
    agents_sections: list[dict[str, Any]] = field(default_factory=list)
    grounding_notices: list[str] = field(default_factory=list)
    pending_prompts: dict[str, Any] | None = None

    async def reconnect_resolve(self, mode: str | None = None) -> dict[str, Any]:
        if self.transport is not None:
            await self.transport.close()
            self.transport = None
            self.agent.transport = None
        eff_mode = mode or self.cfg.transport
        self.transport = await connect_resolve(
            mode=eff_mode,
            home=self.cfg.home,
            bridge_host=self.cfg.bridge_host,
            bridge_port=self.cfg.bridge_port,
        )
        self.agent.transport = self.transport
        st = await self.transport.call("resolve.status", timeout=4.0)
        self.resolve_connect_note = None
        return st

    async def close(self) -> None:
        self.log.update_totals(
            self.agent.stats, provider=self.cfg.provider, model_id=self.cfg.model.id
        )
        self.bus.detach()
        if self.transport is not None:
            await self.transport.close()
            self.transport = None


async def bootstrap_session(
    *,
    cwd: str | Path | None = None,
    overrides: dict[str, str] | None = None,
    resume: str | bool = False,
    accept_stale_prompts: bool = False,
    make_provider_fn: Callable[..., ProviderAdapter] = create_provider,
    injected_transport: ResolveTransport | None = None,
    auto_connect_resolve: bool = True,
) -> Session:
    home = resolve_home()
    resumed_id: str | None = None
    resume_dir: Path | None = None

    lineage: dict[str, Any] | None = None
    if resume:
        resume_dir, meta = find_session_dir(home, resume)
        resumed_id = str(meta.get("id"))
        lineage = resolve_lineage(home, resume_dir, meta)

    defaults = settings_to_defaults(load_settings(home))
    cfg = load_config(cwd=cwd, defaults=defaults, overrides=overrides, home=home)

    # Keep the operator's current working directory active; append the resumed session's
    # original cwd (and any prior cwds in the resume chain) as read-only directories.
    resumed_cwd_paths: list[Path] = []
    for cand_raw in (lineage["resumed_cwds"] if lineage else []):
        with contextlib.suppress(OSError):
            cand_p = Path(cand_raw).resolve()
            if cand_p.is_dir():
                if cand_p not in resumed_cwd_paths:
                    resumed_cwd_paths.append(cand_p)
                if cand_p != cfg.cwd and cand_p not in cfg.ro_dirs:
                    cfg.ro_dirs.append(cand_p)

    prompts, prompt_sources, prompt_layers = resolve_prompts_layered(home=cfg.home, cwd=cfg.cwd)
    forks = check_forks(sources=prompt_sources, layers=prompt_layers)
    if forks["stale"] and not accept_stale_prompts:
        s = forks["stale"][0]
        raise ValueError(
            f"Prompt override {s['file']} ({s['key']}) was forked from davag {s['forkedVersion']} "
            f"({s['forkedHash']}) and built-in is now {s['currentVersion']} ({s['currentHash']}). "
            "Run `/prompts accept` or pass `--accept-prompts`."
        )

    # Connect to Resolve BEFORE building grounding so the initial cached system prompt
    # already contains Project, Media Pool clip_ids, Timelines, and Timeline Markers!
    transport: ResolveTransport | None = injected_transport
    connect_note: str | None = None
    if transport is None and auto_connect_resolve:
        try:
            transport = await connect_resolve(
                mode=cfg.transport,
                home=cfg.home,
                bridge_host=cfg.bridge_host,
                bridge_port=cfg.bridge_port,
            )
        except TransportConnectionError as err:
            connect_note = str(err).splitlines()[0]

    sandbox = Sandbox(cfg.cwd, ro_dirs=cfg.ro_dirs)
    resolve_snap = await snapshot_resolve_grounding(transport, cache_dir=cfg.home / "cache")
    grounding_text, agents_sections, notices = build_grounding_text(
        cfg=cfg, sandbox=sandbox, resolve_section=resolve_snap
    )
    cfg.warnings.extend(notices)

    system_prompt = build_system_prompt(prompts=prompts, grounding=grounding_text, role=cfg.role)
    ledger = Ledger(limit=cfg.context_limit, compact_at=cfg.compact_at)
    ledger.set_system(system_prompt)

    log = SessionLog(
        home=cfg.home,
        cwd=cfg.cwd,
        provider=cfg.provider,
        model_id=cfg.model.id,
        resumed_from=resumed_id,
        ro_dirs=cfg.ro_dirs,
        resumed_cwds=resumed_cwd_paths,
        initial_totals=lineage["totals"] if lineage else None,
        initial_transcript_lines=lineage["transcript_lines"] if lineage else None,
    )
    if lineage is not None:
        restore_ledger_from_lines(lineage["transcript_lines"], ledger)

    journal = Journal(log.dir, resume_from=resume_dir)
    artifacts = ArtifactStore()
    pastes = PasteStore()
    history = PromptHistory(cfg.home)
    perf = PerfTracker(
        initial_turns=lineage["perf_turns"] if lineage else None,
        turn_offset=int(lineage["totals"]["turns"]) if lineage else 0,
    )

    sess_holder: list[Session] = []

    def _drain_prompts() -> dict[str, Any] | None:
        if sess_holder and sess_holder[0].pending_prompts:
            return apply_staged_prompts(sess_holder[0])
        return None

    agent = Agent(
        cfg=cfg,
        make_provider=make_provider_fn,
        ledger=ledger,
        sandbox=sandbox,
        journal=journal,
        artifacts=artifacts,
        prompts=prompts,
        transport=transport,
        session_log=log,
        perf_tracker=perf,
        drain_prompts_hook=_drain_prompts,
        initial_stats=lineage["totals"] if lineage else None,
    )
    bus = SessionBus(agent=agent)
    bus.state["stats"] = dict(agent.stats)
    if lineage is not None:
        for entry in lineage["web_replay"]:
            bus.publish("transcript", entry)
    journal.on_change = lambda st_payload: bus.publish("journal-updated", st_payload)

    session = Session(
        cfg=cfg,
        prompts=prompts,
        prompt_sources=prompt_sources,
        prompt_layers=prompt_layers,
        ledger=ledger,
        sandbox=sandbox,
        journal=journal,
        artifacts=artifacts,
        pastes=pastes,
        history=history,
        perf=perf,
        log=log,
        agent=agent,
        bus=bus,
        transport=transport,
        resolve_grounding_snapshot=resolve_snap,
        resolve_connect_note=connect_note,
        agents_sections=agents_sections,
        grounding_notices=notices,
    )
    sess_holder.append(session)
    return session