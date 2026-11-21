# Brief: telegram-commands
Model: middle
Branch: telegram-cmd
Touch: gate.py, tests/**, skill/**, docs/**
Done when: `./check.sh`
Read: docs/DECISIONS.md

## Task
Commands from the owner's phone act on armed sessions, through the one bot shared by every machine (D9), at no token cost until a command arrives (D11). Test first; stdlib only; must import on Linux and Windows.

Commands: `/status`, `/stop` (hard stop now), `/pause` (pack up now), `/continue` (same as `gate.py continue`), `/resume` (same as `gate.py resume`), `/off` (disarm), `/help`.
Addressing: reply to a gate message → that session (record sent message_id → session id per machine when sending); `/cmd <machine>` → armed sessions on that machine; bare → every armed session on every machine. Only messages from the configured chat_id count; ignore others and messages older than 10 min.

1. `gate.py telegram poll`: one short `getUpdates` (timeout 0); apply each new command to the matching local session files using the existing continue/resume/pause/disarm code (add a `stop` override the hook treats as hard); reply per session `[machine · email] <what happened>`. Remember the last seen update_id per machine in a local file. Never pass an offset that would drop other machines' updates, except updates older than 10 min (acknowledge those so the queue stays short). A 409 from Telegram → back off and try later.
2. Who polls, rate-limited to once per 15 s per machine through a timestamp file:
   - the hook, only when the session is armed and Telegram is configured, via a detached child (same pattern as the send; the hook must not wait);
   - the background loops `timer` and `wait`, which must also exit when a command changed their session (`/continue` or `/pause` ends `timer`; `/resume` or `/continue` ends `wait`), printing what the owner asked for;
   - new `gate.py listen` (background): polls until a command reaches this session, then exits printing the instruction. Only one `listen` per session: a new one ends the old one.
3. Skill: while armed, before ending a turn that waits for the user, start `gate.py listen` with run_in_background; when it exits, follow what it printed. When the hook says stop/pause, behave as for hard stop/packing.
4. Tests with the fake Telegram server: each command and each addressing form; foreign chat ignored; old messages ignored; two machines (two HOMEs, same fake server) both see a bare command; poll rate limit; hook returns fast with a hanging server; timer/wait/listen exit on the matching command.
5. Live (lead): arm, end the turn with `listen` running, send `/status` and `/continue` from the phone; pause with a test threshold and send `/resume`.
