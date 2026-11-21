# Handoff

## Anchor
Goal: Armed Claude Code sessions stop cleanly near the 5-hour or weekly plan limit and resume by themselves after the reset, at no token cost.
Milestone: M3 Linux and Windows
Ends when: arm, status, a Telegram alert and /continue work in VS Code on the DGX Spark (Linux ARM64) and on Windows, and `usage-gate/check.sh` is green on main.
Workflow: change usage-gate/gate.py test first in usage-gate/tests/test_gate.py, `usage-gate/check.sh`, `usage-gate/install.sh`, verify live in a session.
Out of scope: gating sessions that never ran /usage-gate; any token-costing usage source.
Decides: owner

## State
Main: main @ fdff698 check:pass
Live on the Mac 2026-11-21 17:10: soft limit tells instead of blocking (call ran; context arrived on PreToolUse and again on PostToolUse); Telegram Continue button applied to its own session only (phase run, override soft); `/` menu set.
Merged: soft-tells D21 (5546395, 169 tests): only NEW_WORK refused at soft; paused_hard and past-hard pauses block like a hard stop; refused calls logged to blocked.md, archived on resume.
Installed copy matches 5546395. Public copy committed in ~/Developer/handoff-kit as dbc17fa, NOT pushed (owner asks before every public push).
Old-code listener (process 683) is gone; loops now restart on update (dd14bc4).
Not verified live: 429 back-off (no 429 since), reexec on update, Linux (DGX Spark), Windows; multi-day `wait`; refresh after 8 h+.

## Next
| # | Step | Brief | Model | Done when |
|---|---|---|---|---|
| 1 | Live check on DGX Spark and Windows (port brief item 7): owner installs from claude-code-kits per README; lead reads outputs and fixes | usage-gate/docs/briefs/port-linux-windows.md | strongest | arm, status, Telegram alert and /continue work on both |
| 2 | Stop rewriting gate commands: session id from env (probe CLAUDE_CODE_SESSION_ID in Bash calls) | | smallest | probe result recorded, then a middle brief if yes |
| 3 | Refresh check: gate.log "refreshed login" lines after 8 h+ | | smallest | a refresh row exists and status still reads |

## Questions
| # | Question | Blocks |
|---|---|---|

## How to work
- Source of truth is this repo; `python3 usage-gate/install.py` copies usage-gate/gate.py and the skill into ~/.claude and merges the hooks (idempotent).
- Tests use an isolated HOME and `USAGE_GATE_FAKE="<5h>,<weekly>"`; `USAGE_GATE_QUIET=1` silences notifications.
- Run each gate command in a response of its own, never batched with another call (four misfires so far; the hook rewrites them to add `--session`); arm, spawn and re-arm in separate responses.
- A paused session refuses its own edits; live tests that change the session file need the owner to run the command in a terminal.
- Never refresh the default login `Claude Code-credentials` (the CLI owns it); never print tokens.
- Telegram (D8/D9): `~/.claude/usage-gate/telegram.json` (mode 600: token, chat_id, optional machine); `usage-gate/gate.py telegram setup` (token on stdin) / `test`. Tests use `USAGE_GATE_TELEGRAM_API` (fake server), `USAGE_GATE_NO_OSASCRIPT=1`, `USAGE_GATE_FAKE_AGE=<s>`.
- Telegram commands: `usage-gate/gate.py telegram poll` (rate limit `USAGE_GATE_POLL_INTERVAL`, default 15 s; tests also use `USAGE_GATE_TICK` for loop sleeps), `usage-gate/gate.py listen`; state in `telegram-last.json`, `telegram-msgs.json` and `telegram-polled`. Owner `/stop` and `/pause` set `manual` in the session file and hold until `/resume`/`/continue`.
- Debug: `~/.claude/usage-gate/gate.log`, session state in `~/.claude/usage-gate/sessions/<id>.json`.
- Work happens in ~/Developer/handoff-kit (claude-code-kits); the old private repo ~/Developer/usage-gate is kept read-only for history.
- Briefs must ask for tests that are red on behaviour, not through a helper error (builder's first 429 tests were red via a KeyError).
- Before a live Telegram test, check `ps` for gate loops started before the last install; never stop another session's process (D19).

## Read first
| Ref |
|---|
| usage-gate/docs/briefs/port-linux-windows.md |

## Read when
| Trigger | Ref |
|---|---|
| changing gate behaviour | usage-gate/gate.py |
| changing what Claude is told to do | usage-gate/skill/SKILL.md |
| a test fails | usage-gate/tests/test_gate.py |
| a past choice is questioned | usage-gate/docs/DECISIONS.md |

## Never read
| Path |
|---|
| .hk/ |

## Exists
| Piece | Status | Evidence | Paths |
|---|---|---|---|
| Gate hook + commands | tested (169 tests), live on the Mac; not run on Linux or Windows | check:fdff698 | usage-gate/gate.py |
| /usage-gate skill | installed, used live in both apps on the Mac | check:fdff698 | usage-gate/skill/** |
| Installer (shell) | ran on the Mac earlier, copy matched | check:fdff698 | usage-gate/install.sh |
| Installer (python) | tested (atomic copy); ran on the Mac with absolute hook paths; not run on Linux or Windows | check:fdff698 | usage-gate/install.py |
| 429 handling | tested; back-off holds; arm read usage live on the Mac | check:fdff698 | usage-gate/gate.py |
| Telegram alerts + commands | tested with fake server; alerts live from phone on the Mac | check:fdff698 | usage-gate/gate.py, usage-gate/tests/** |
| Telegram buttons + menu | tested with fake server; menu set live; Continue button live-verified 2026-11-21 | check:fdff698 | usage-gate/gate.py |
| reexec-on-update | tested (169 tests); not run live | check:fdff698 | usage-gate/gate.py, usage-gate/install.py |
| Soft tells (D21) + blocked.md | tested; soft tell live on the Mac | check:fdff698 | usage-gate/gate.py, usage-gate/skill/** |

## Parked
| Branch | Tip | Verified | Not verified | Next |
|---|---|---|---|---|

## Findings
| Sev | Finding | File |
|---|---|---|
| low | `claude` NEW_WORK regex misses leading space/newline, nohup/env/timeout, $( and full paths; catches '; claude' inside quoted text | usage-gate/gate.py |
| low | blocked.md archive: 1 s name resolution; when listen, wait and timer all handle one /continue, later messages say none were refused | usage-gate/gate.py |
| low | blocked.md trim past 200 lines rewrites in place (concurrent deny can be lost); post_tell not cleared on phase change | usage-gate/gate.py |
| low | Soft tell arrives twice (PreToolUse and PostToolUse both delivered live); the PostToolUse repeat can go | usage-gate/gate.py |
| low | Background `wait` dies if the app restarts or the Mac sleeps long; the session then resumes on the next message only | usage-gate/gate.py |
| low | Side sessions must be armed on their own (skill tells the lead to add it to briefs) | usage-gate/skill/SKILL.md |
| low | Hook adds ~50 ms per tool call in every session (fast exit when not armed) | usage-gate/gate.py |
| low | `usage-gate/gate.py off` is not a command (skill maps `/usage-gate off` to `disarm`) | usage-gate/gate.py |
| low | Bare /help is answered by every machine with armed sessions | usage-gate/gate.py |
| low | Child send has no overall deadline (DNS) | usage-gate/gate.py |
| low | Two-tick reexec untested on a half-written file | usage-gate/gate.py |

## Decisions
D1–D21 in usage-gate/docs/DECISIONS.md; D11–D13, D15, D16, D18, D20 taken on the owner's behalf. Kit fix (section 10): hk C4 failed on a reverted Anchor typo; now compares with one baseline (kit 7f7b57f, pushed).

## Machines
the Mac: the app account (keychain `Claude Code-credentials-<hash>`, gate-refreshed); the CLI account (keychain `Claude Code-credentials`). Remote: origin (claude-code-kits). Mac app now signed in to the CLI account (owner, 2026-11-21).
the Mac: Telegram configured (~/.claude/usage-gate/telegram.json, mode 600).
Windows PC: VS Code extension; not set up.
DGX Spark (lab, Linux ARM64): VS Code extension; not set up.
