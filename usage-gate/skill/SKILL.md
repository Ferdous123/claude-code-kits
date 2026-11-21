---
name: usage-gate
description: Arm the usage gate for this session so it pauses cleanly near the 5-hour or weekly plan limit and resumes by itself after the reset. Use when the user types /usage-gate (optionally with thresholds), or asks to watch, gate or stop on usage limits.
---

# Usage gate

A hook (`gate.py hook`, installed in `~/.claude/settings.json` with absolute paths) does the watching at no token cost. It reads the account's login from the macOS keychain, or from `~/.claude/.credentials.json` on Linux and Windows (read-only, never refreshed). This skill only arms it and tells you how to react to its messages.

## Arm

Arguments: `[soft] [hard] [ask=<minutes>]`, defaults 80, 95, 10. `off` disarms, `status` reports.

Run (the hook adds the session id; use `python` instead of `python3` on Windows, and the messages the gate prints already do):

```
python3 -I ~/.claude/usage-gate/gate.py arm --soft <soft> --hard <hard> --ask <minutes>
```

Reply to the user in one line with what it printed (account, thresholds, current figures). If it says usage can't be read, tell them which login to redo and stop.

## Phone commands (Telegram)

While armed, before you end a turn that waits for the user, start `python3 -I ~/.claude/usage-gate/gate.py listen` with run_in_background (one per session; a new one ends the old one). Alerts also carry buttons (Continue/Pause, Resume/Status) that act on exactly that session, and `gate.py telegram menu` sets the "/" command list. The owner can send `/status`, `/stop`, `/pause`, `/continue`, `/resume`, `/off`, `/help` from the phone. When `listen` exits, do exactly what it printed: continue the work, pack up (as for packing), hard stop (as for hard stop), or carry on ungated after `/off`. If it says Telegram is not set up, skip it.

When the hook says stop or pause and the message came from the owner's phone, behave as for hard stop or packing: write `RESUME.md`, run `gate.py wait` in the background. A phone `/stop` or `/pause` holds until the owner sends `/resume` or `/continue`, even if usage is low.

## When a tool call is blocked with "USAGE GATE"

Do exactly what the message says. It is the automation, not an error. In short:

- **Soft (asked):** start nothing new; ask the user once, continue or pause, via `wrap_up` buttons, or plain text where there is no `wrap_up` (VS Code) (never AskUserQuestion, it can't time out); start `gate.py timer` with run_in_background; end the turn.
  - User says continue → `gate.py continue`. User says pause → `gate.py pause`.
  - Timer exits with "no answer" → pack up.
- **Packing:** let running agents finish and hand back, write `RESUME.md` in the notes folder the message names, run `gate.py wait` with run_in_background, end the turn with one line.
- **Hard stop:** agents write checkpoints and stop; you write `RESUME.md`, start `gate.py wait` in the background, end the turn.
- **Paused, then the user tells you to resume:** run `gate.py resume` first, then carry on.
- **`wait` exits with "limits are back":** read `RESUME.md` and the `agent-*.md` checkpoints, resume the plan, say so in one line.

When you start a side session while armed, add to its brief: "first run `python3 -I ~/.claude/usage-gate/gate.py arm --soft <s> --hard <h> --ask <m>`".

If the hook says to log in again, tell the user the login command it names, once, and carry on. If it says usage has been unreadable for N min (rate-limited/network), say so in one line: gating resumes by itself when it reads again; no login needed.
