# Brief: expired-token
Model: middle
Branch: expired-token
Touch: gate.py, tests/**, docs/**
Done when: `./check.sh`

## Task
Live 2026-11-21 22:28: the CLI-owned login's access token passed its `expiresAt`; the CLI only saved a refreshed one at 22:43. The gate called the usage API with the expired token (401 → "Log in again", wrong), kept retrying every ~2 min and drew a 429 at 22:34. Test first (fake usage server via `USAGE_GATE_API`, login file as in RateLimitTest).

1. For a login the gate does not manage (D12), when `expiresAt` has passed, do not call the API. Up to LOGIN_WAIT (30 min) past expiry it is a transient error of kind "waiting for Claude Code to refresh its login": the last good reading still gates while younger than STALE_OK, and the 15-min notice names that kind. No "Log in again".
2. More than LOGIN_WAIT past expiry it is a login problem ("Log in again"), still without an API call.
3. A 401 on a token that has not expired still says "Log in again".
