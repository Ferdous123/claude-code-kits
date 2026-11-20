# usage-gate

A Claude Code skill and hook that stop a session cleanly before it hits the plan's 5-hour or weekly limit, then let it carry on by itself after the reset. Watching costs no tokens: a hook reads your account's usage figures and blocks tool calls only when a threshold is crossed. Optional Telegram alerts and phone commands.

Status: used daily on macOS (Claude Code desktop app and the VS Code extension). Linux (incl. ARM64) and Windows are supported through the VS Code extension: with no keychain the login is read from `~/.claude/.credentials.json`, read-only (the gate never refreshes it), and the commands use `python` on Windows, `python3` elsewhere. Not yet verified live on those machines.

## How it behaves

Arm a session with `/usage-gate [soft] [hard] [ask=<minutes>]` (defaults 80, 95, 10). Sessions that never ran it are not touched.

- **Soft limit:** Claude starts nothing new and asks you once, continue or pause. With no answer within `ask` minutes it packs up: running agents finish, it writes a `RESUME.md` note, and waits.
- **Hard limit:** every tool is blocked except writing notes. Agents write checkpoints and stop; the session writes `RESUME.md` and waits.
- **Resume:** when both limits are back below the soft threshold (or you say resume), the waiting background command exits, which wakes the session; it reads its notes and carries on.

## Install

```
python3 install.py        # Windows: python install.py   (./install.sh is a wrapper for it)
```

It copies `gate.py` to `~/.claude/usage-gate/`, the skill to `~/.claude/skills/usage-gate/`, and adds a PreToolUse and a PostToolUse hook to `~/.claude/settings.json` (idempotent: it replaces an older usage-gate entry and keeps your other hooks). The hook command uses absolute paths: `python3` (Windows: `python`) found on PATH, skipping virtualenv, conda and pyenv-shim interpreters, else the one running the installer; and the installed `gate.py`. Where there is no keychain (Linux, Windows) it skips the accounts step with a one-line note. `./check.sh` runs the tests (isolated HOME, fake usage figures; no network).

Usage figures come from the account's own Claude Code login. If a session's account has no login the gate can read, it tells you which login command to run once.

## Telegram (optional)

1. Create a bot with @BotFather and send it `/start` from your phone.
2. In a terminal (`python` instead of `python3` on Windows): `python3 -I ~/.claude/usage-gate/gate.py telegram setup` — it asks for the token without echoing it, finds your chat, saves both to `~/.claude/usage-gate/telegram.json` (mode 600) and sends a test message.

Every alert then also goes to Telegram, prefixed `[machine · account · project #session]`. One bot can serve several machines.

Phone commands: `/status`, `/stop` (hard stop now), `/pause` (pack up now), `/continue`, `/resume`, `/off` (disarm), `/help`. Reply to a bot message to address that session, add a machine name (`/stop laptop`) to address one machine, or send it bare for every armed session. Only your chat is obeyed, and messages older than 10 minutes are ignored. A `/stop` or `/pause` from the phone holds until you send `/resume` or `/continue`; the session cannot lift it by itself.

## Commands

`python3 -I ~/.claude/usage-gate/gate.py <command>`: `arm`, `disarm`, `status`, `continue`, `resume`, `pause`, `timer`, `wait`, `listen`, `accounts`, `telegram setup|test`. Claude runs these itself; the hook adds the session id.

## License

MIT
