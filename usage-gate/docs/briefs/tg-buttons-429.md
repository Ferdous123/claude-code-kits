# Brief: tg-buttons-429
Model: middle
Branch: tg-buttons
Touch: gate.py, tests/**, skill/**, README.md, docs/**
Done when: `./check.sh`

## Task
Live 2026-11-21 10:17–10:28 (gate.log): /api/oauth/usage answered 429 every 4–15 s from processes running the new code (installed 464f561), so the shared back-off is not holding in practice; and a phone `/continue` during the 429 left the session un-overridden, so the soft question came straight back ("Go on." without "Next gate at 95%", then "80% reached" again). Owner also wants buttons instead of typed commands. Test first; each new test must fail on the old behaviour, not on a helper.

1. Investigate first (log only, no fix yet): which callers fetched during the back-off window (hook, poll child, listen, wait, timer, do_continue, arm)? Add a log line per real HTTP fetch with the caller name. Find why `retry_after_until` did not stop them (e.g. 429 without Retry-After resetting, callers bypassing the cache, ttl 0/60 paths). Fix so a 429 stops every fetch on the machine for the back-off window; log "usage error" only on a real fetch.
2. (Done on main at aedc8e0: continue with unreadable usage holds until hard.) Also: when the timer sees phase run after a phone /continue it must not leave a second ask possible; add a test soft ask → phone /continue → next hooks pass until 95%.
3. Telegram command menu: `telegram setup` (and a new `gate.py telegram menu`) calls `setMyCommands` so typing "/" lists /status /stop /pause /continue /resume /off /help with one-line descriptions.
4. Buttons: alerts carry an inline keyboard — soft question: Continue / Pause; hard stop or paused: Resume / Status; others: Status. `callback_data` holds the action and the session id prefix, so a press addresses exactly that session. The poller handles `callback_query` from the configured chat only (same age limit), calls `answerCallbackQuery`, applies the action like the typed command, and edits or replies with the result. Typed commands keep working.
5. Tests with the fake Telegram server: setMyCommands sent on setup; alert JSON has the right reply_markup per phase; a callback for session A doesn't touch session B; foreign chat callback ignored; /continue during 429 keeps the override; back-off holds across hook, poll child and listen (count server hits).
