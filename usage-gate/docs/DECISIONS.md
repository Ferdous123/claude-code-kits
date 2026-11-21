# Decisions

| Id | Date | Decision | By |
|---|---|---|---|
| D1 | 2026-11-21 | Gate only sessions that ran /usage-gate; owner opens new sessions freely | owner |
| D2 | 2026-11-21 | Soft (default 80): ask continue/pause with timeout (default 10 min), then pack up; hard (default 95): agents checkpoint and stop | owner |
| D3 | 2026-11-21 | Resume by itself when both limits < soft, or when the owner says resume | owner |
| D4 | 2026-11-21 | Usage from each account's own Claude Code login, no token-costing fallback; notify on login failure | owner |
| D5 | 2026-11-21 | App account read via second CLI login `CLAUDE_CONFIG_DIR=~/.claude-appacct`; gate refreshes only that login | owner / Claude |
| D6 | 2026-11-21 | "continue" lasts until hard; "resume" at hard turns gating off until below soft | Claude (owner's behalf) |
| D7 | 2026-11-21 | Remote is the local bare repo ~/Developer/remotes/usage-gate.git | Claude (owner's behalf) |
| D8 | 2026-11-21 | Telegram notifications in addition to macOS notifications (both kept) | owner |
| D9 | 2026-11-21 | One Telegram bot shared by all machines; messages name machine and account; commands pick the machine by replying to its message or naming it | owner |
| D10 | 2026-11-21 | Targets: macOS, Windows, Linux (DGX Spark ARM64); VS Code extension first, Mac app too | owner |
| D11 | 2026-11-21 | Commands reach a session through a background `gate.py listen` that exits to wake it; no tmux, no token-costing Channels | Claude (owner's behalf) |
| D12 | 2026-11-21 | On Linux and Windows read ~/.claude/.credentials.json read-only and never refresh it | Claude (owner's behalf) |
| D13 | 2026-11-21 | A phone /stop or /pause is lifted only by the owner (phone or terminal); the hook denies the model's continue/resume/disarm/arm meanwhile | Claude (owner's behalf) |
| D14 | 2026-11-21 | Public repo Ferdous123/claude-code-kits (renamed from handoff-kit); usage-gate copied in by publish.sh, scrubbed; docs stay private | owner |
| D15 | 2026-11-21 | Telegram prefix `[machine · email · project #sid4]` | Claude (owner's behalf) |
| D16 | 2026-11-21 | A bare phone command goes to every armed session on every machine; reply or machine name narrows it | Claude (owner's behalf) |
| D17 | 2026-11-21 | Windows and DGX Spark install from the public repo claude-code-kits (clone + installer); no private remote; development stays on the Mac | owner |
| D18 | 2026-11-21 | Port details: no keychain means read the login file; `USAGE_GATE_NO_KEYCHAIN` forces that. Fallback lock file counts as stale after the wait timeout (not 30 s). `save_sess` raises "session file is busy" instead of writing without the lock. Session files are mode 600 on POSIX. Installer writes `"<sys.executable>" -I "<gate.py>" hook` (forward slashes on Windows) | Claude (owner's behalf) |
| D19 | 2026-11-21 | Live tests never stop or message other sessions' processes; buttons act only on their own session | owner |
| D20 | 2026-11-21 | Only a lone gate command is auto-allowed by the hook; the installed copy ignores USAGE_GATE_* test overrides | Claude (owner's behalf) |
| D21 | 2026-11-21 | Soft phases (asked, packing, paused_soft) tell, not block: only NEW_WORK is denied; hard, paused_hard and phone stop/pause still block | owner |
