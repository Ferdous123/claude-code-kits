# usage-gate

Development happens in this folder of claude-code-kits. The working notes in `docs/` (handoff, decisions, briefs, lessons) are the developer's history and mention the developer's own machines and paths; they are not installation instructions, and some of them refer to scripts that are not in this repository.

A Claude Code skill and hook that stop a session cleanly before it hits the plan's 5-hour or weekly limit, then let it carry on by itself after the reset. Watching costs no tokens: a hook reads your account's usage figures and blocks tool calls only when a threshold is crossed. Optional Telegram alerts and phone commands.

Status: used daily on macOS (Claude Code desktop app and the VS Code extension). Linux (incl. ARM64) and Windows are supported through the VS Code extension: with no keychain the login is read from `~/.claude/.credentials.json`, read-only (the gate never refreshes it), and the commands use `python` on Windows, `python3` elsewhere. Not yet verified live on those machines.

## How it behaves

Arm a session with `/usage-gate [soft] [hard] [ask=<minutes>]` (defaults 80, 95, 10). Sessions that never ran it are not touched.

- **Soft limit:** Claude starts nothing new and asks you once, continue or pause. With no answer within `ask` minutes it packs up: running agents finish, it writes a `RESUME.md` note, and waits.
- **Hard limit:** every tool is blocked except writing notes. Agents write checkpoints and stop; the session writes `RESUME.md` and waits.
- **Resume:** when both limits are back below the soft threshold (or you say resume), the waiting background command exits, which wakes the session; it reads its notes and carries on.

## Requirements

- Python 3 (standard library only; the minimum version is not recorded in the repository) and a Claude Code login on the account to be watched.
- Network access: the gate reads the account's usage figures from Anthropic's usage endpoint, and Telegram alerts use the Telegram API.
- macOS reads the login from the keychain (`security` command); Linux and Windows read `~/.claude/.credentials.json`.
- Optional: a Telegram account and a bot.

## Install

```
python3 install.py        # Windows: python install.py   (./install.sh is a wrapper for it)
```

It copies `gate.py` to `~/.claude/usage-gate/`, the skill to `~/.claude/skills/usage-gate/`, and adds a PreToolUse and a PostToolUse hook to `~/.claude/settings.json` (idempotent: it replaces an older usage-gate entry and keeps your other hooks). The hook command uses absolute paths: `python3` (Windows: `python`) found on PATH, skipping virtualenv, conda and pyenv-shim interpreters, else the one running the installer; and the installed `gate.py`. Where there is no keychain (Linux, Windows) it skips the accounts step with a one-line note. `./check.sh` (run it from this folder) compiles `gate.py`, runs the tests (isolated HOME, fake usage figures; no network) and says whether the installed copies match the repository.

Usage figures come from the account's own Claude Code login. If a session's account has no login the gate can read, it tells you which login command to run once.

## Telegram (optional)

1. Create a bot with @BotFather and send it `/start` from your phone.
2. In a terminal (`python` instead of `python3` on Windows): `python3 -I ~/.claude/usage-gate/gate.py telegram setup` — it asks for the token without echoing it, finds your chat, saves both to `~/.claude/usage-gate/telegram.json` (mode 600) and sends a test message.

Every alert then also goes to Telegram, prefixed `[machine · account · project #session]`. One bot can serve several machines.

Phone commands: `/status`, `/stop` (hard stop now), `/pause` (pack up now), `/continue`, `/resume`, `/off` (disarm), `/help`. Reply to a bot message to address that session, add a machine name (`/stop laptop`) to address one machine, or send it bare for every armed session. Only your chat is obeyed, and messages older than 10 minutes are ignored. A `/stop` or `/pause` from the phone holds until you send `/resume` or `/continue`; the session cannot lift it by itself.

## Commands

`python3 -I ~/.claude/usage-gate/gate.py <command>`: `arm`, `disarm`, `status`, `continue`, `resume`, `pause`, `timer`, `wait`, `listen`, `accounts`, `telegram setup|test|menu|poll`, `hook` (the hook entry point). Claude runs these itself; the hook adds the session id. `telegram menu` sets the "/" command list in Telegram.

## Limitations

- Only sessions armed with `/usage-gate` are watched.
- There is no uninstaller. To remove it, delete the `usage-gate` entries from the PreToolUse and PostToolUse lists in `~/.claude/settings.json`, then delete `~/.claude/usage-gate/` and `~/.claude/skills/usage-gate/`.
- Not verified live on Linux or Windows (see Status). Also not yet confirmed in live use, per `docs/HANDOFF.md`: the 429 (rate-limit) back-off, restarting loops after an update, a multi-day wait, and the login refresh after eight or more hours.
- Tests: of the 172 tests in `tests/test_gate.py`, 169 pass and 3 fail when run on Linux (Python 3.12). Two installer tests fail when a virtualenv is active, because the installer skips virtualenv interpreters and the tests expect the first `python3` on PATH; one test of the macOS keychain path fails where there is no `security` command. These are open defects in the tests, not documented behaviour.

## License

MIT
