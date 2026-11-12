# davag

DaVinci Resolve AI editing agent & custom harness (TUI REPL + Web Sidecar GUI).
See `plan.md` for full architecture and roadmap.

## Architectural Invariants

1. **Zero third-party dependencies inside Resolve:**
   `src/davag/resolve_ops/` and `src/davag/bridge/` MUST import Python standard library modules only. They execute both inside the external `.venv` (`DirectTransport`) and inside DaVinci Resolve's embedded `fuscript` Python host (`BridgeTransport`).
2. **Single-owner state, dual views:**
   `agent/`, `context/`, `providers/`, `session/`, and `server/` must remain TUI-free. Both the terminal REPL and the `--web` / `--host` SSE browser GUI drive the session via `session/dispatch.py` and subscribe via `session/bus.py`.
3. **Configuration precedence (lowest → highest):**
   `$DAVAG_HOME/settings.json` → `~/.davai/.env` (read-only API key fallback) → `$DAVAG_HOME/.env` → `./.env` (with `HOME_ONLY_VARS` stripped) → `os.environ` → CLI flags → live session overrides.
4. **CRLF-preserving `.env` edits:**
   All `.env` edits go through `config/envfile.py` and `config/profiles.py`, preserving comments (`##` immune prose vs `#` parked settings) and original line endings (`\r\n` vs `\n`) byte-for-byte.
5. **Safe mutations & undo:**
   Resolve API mutations do not reliably populate Resolve's UI undo stack. Mutating `dr_ops` batches snapshot the active timeline (`.davag-snap-*`) before applying changes, and `resolve.eval` / `shell` / non-loopback `--yolo` follow strict operator approval gates.