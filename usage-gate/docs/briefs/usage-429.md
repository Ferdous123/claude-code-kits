# Brief: usage-429
Model: middle
Branch: usage-429
Touch: gate.py, tests/**, skill/**, docs/**
Done when: `./check.sh`

## Task
Live 2026-11-21 09:21: `/api/oauth/usage` returned HTTP 429; the gate cached the error over the last good reading (gating off for HOOK_TTL) and told the owner "Can't read usage … Log in again", which was wrong. Test first for each item (fake usage server via `USAGE_GATE_API`, see existing login-file tests).

1. Keep the last good reading in the cache separately from errors. On a transient error (429, 5xx, timeout, URLError) return the last good reading while it is younger than `STALE_OK` (15 min), marked stale; the hook keeps gating on it.
2. Back off after a 429: honour `Retry-After` (seconds) if present, else 60 s doubling to 15 min; store `retry_after_until` in the cache so every process on the machine (hooks of all sessions, wait/timer/listen) skips fetching until then. Reset on success.
3. Only a login error (401 after any refresh, missing or unreadable login) says "Log in again". A transient error notifies only when there has been no good reading for more than `STALE_OK`, worded "Can't read usage for <email> for N min (rate-limited/network); gating paused until it reads again", at most once an hour; and says when it reads again.
4. Fewer requests: no forced fresh fetch (`usage(svc, 0)`) where a reading under 60 s old would do; `wait` polls no more often than every 5 min.
5. Tests: 429 keeps the old reading and still denies at hard; 429 with Retry-After stops fetches until then (count server hits across two hook processes); 401 still says log in; stale beyond 15 min notifies with the new wording once.
