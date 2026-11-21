# Brief: telegram-alerts
Model: middle
Branch: telegram
Touch: gate.py, tests/**, skill/**, install.sh, docs/**
Done when: `./check.sh`
Read: docs/DECISIONS.md

## Task
Send every gate notification to Telegram as well as the macOS notification (D8). One bot for all machines (D9). Commands are the next brief, not this one; keep the design ready for them.

1. Config `~/.claude/usage-gate/telegram.json` (`token`, `chat_id`, optional `machine`), mode 600. Never in the repo, never in argv, never printed or logged.
2. `gate.py telegram setup`: read the bot token from stdin; tell the owner to send `/start` to the bot; read `getUpdates` once to find the chat id; save; send a test message. `gate.py telegram test` sends one message.
3. `notify(text)`: keep the osascript call on macOS (skip it elsewhere); if Telegram is configured, also `sendMessage` with the prefix `[<machine> · <account email>]`. Machine defaults to the short hostname. Timeout 5 s; a failure is logged to gate.log (no token) and never raises or blocks the hook.
4. Messages carry the figures and the reading's age; skip sending when the figures are older than 5 min and say so in the log.
5. Tests first, isolated HOME: point the API base at a local fake HTTP server via `USAGE_GATE_TELEGRAM_API`; assert the prefix, that nothing is sent when unconfigured, and that a server error does not change the hook's decision. `USAGE_GATE_QUIET=1` must silence both channels.
6. `./check.sh`, `./install.sh`, then live: the owner creates the bot with BotFather and runs setup; arm at a figure just under the current one and confirm the phone gets the alert.
