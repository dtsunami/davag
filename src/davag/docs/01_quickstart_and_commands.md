# Quickstart, CLI Flags & Interactive Commands

## Launching `davag`

```powershell
davag                                    # Interactive Terminal TUI REPL
davag --web                              # TUI REPL + Web GUI on http://127.0.0.1:7879
davag --open                             # TUI REPL + auto-open tokenized Web GUI in browser
davag --host 0.0.0.0 --port 7879         # TUI REPL + LAN Web GUI (desktop, laptop, phone/tablet)
davag --role reviewer -p "QC active cut" # Strictly read-only one-shot review
davag --resume                           # Resume most recent session
```

## Terminal Shortcuts & History

- **`Up` / `Down` Arrows:** Recall previous prompts across sessions (`~/.davag/history.jsonl`) in both the Terminal REPL and the Web GUI composer.
- **`Alt+O`:** Open tokenized Web Studio URL in default browser.
- **`Alt+C` / `Alt+T`:** Copy tokenized Web URL or raw bearer token to clipboard.
- **`Esc`:** Cancel the active agent turn immediately.
- **Type while busy:** Queues a live `[steer]` instruction injected at the next step boundary without losing progress.

## Full Slash Command Reference

| Command | Summary |
|---|---|
| `/help` | List all REPL & Web slash commands |
| `/resolve [direct\|bridge]` | Inspect or reconnect DaVinci Resolve Studio / Free |
| `/role [reviewer\|logger\|editor\|finisher\|master]` | View or switch the active capability surface |
| `/grab [HH:MM:SS:FF]` | Grab current Resolve viewer frame & attach PNG |
| `/sheet <clip\|path> [start dur]` | Generate `4×4` burned-in-TC mosaic contact sheet |
| `/index <clip\|path> [--force]` | Run fast Scout model (`gemini-3.8-flash`) & cache Digest |
| `/proxy <clip\|path> [360\|540\|720\|1080] [kbps]` | Generate QSV/NVENC proxy & link in Resolve |
| `/transcribe <clip\|path> [start dur]` | Extract word/segment transcript mapped to source frames |
| `/watch <clip\|path> [start dur]` | Attach short video proxy slice for native Gemini video review |
| `/vo [prefix]` | Convert timeline markers into Fairlight ADR `.csv` + `ST1` `.srt` Teleprompter |
| `/thumbnail <prompt>` | Generate 16:9 YouTube thumbnail via OpenRouter Flux + viewer frame |
| `/youtube-stage <title>` | Stage YouTube bundle (`DESCRIPTION.md` w/ chapters & links, QR overlays, `.srt`, render job) |
| `/youtube-auth [json_path]` | Run one-time Google OAuth2 browser login & save `~/.davag/youtube_token.json` |
| `/web [open\|copy\|token]` | Inspect, open, or copy tokenized Web Studio URL / bearer token |
| `/undo` | Restore active Resolve timeline from `.davag-snap-*` checkpoint |
| `/model [id] [--save]` | Switch active LLM (`gemini-4-carbon`, `claude-opus-5-5`, `gpt-5.1`, etc.) |
| `/effort <low\|medium\|high\|xhigh\|max>` | Adjust reasoning effort |
| `/keys [provider key] [--save]` | Inspect masked keys or store API keys in `~/.davag/.env` |
| `/env [select\|set ...]` | Switch `#---#` `.env` provider blocks preserving comments & CRLF |
| `/context` / `/drop <id>` / `/pin <id>` | Manage addressable context segments |
| `/prompts [export\|diff\|apply]` | Inspect or customize layered `.prompts/*.md` |