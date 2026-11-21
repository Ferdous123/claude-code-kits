# Lessons (newest first)

- [tests] Before a live Telegram test, `ps` for gate loops started before the last install: an old-code poller eats new update types.
- [kit] hk C4 compared the Anchor with every older version, so a reverted typo failed forever; fixed in kit 7f7b57f (baseline only).
- [agents] Ask every builder for tests that fail on the OLD behaviour, not on a helper; a KeyError red is not red.
- [tests] A builder's green run is not the gate: `hk verify` caught a load-dependent flake.
- [agents] A builder returned "PLACEHOLDER" as its report: check the branch and `hk verify`, never the report alone.
- [tests] When an agent says a test was not seen failing, run it against the previous commit's code before merging.
- [hooks] Third time a gate command batched with another Bash lost auto-mode review: Next row to stop rewriting gate commands.
- [hooks] Batching arm with an Agent spawn arms first and blocks the spawn; arm, spawn and re-arm in separate responses (second time a batched gate call misfired: noted in How to work).
- [hooks] VS Code sessions have no wrap_up tool; gate messages must not assume app-only tools.
- [hooks] Run gate commands alone: the hook rewrites their input, so a call batched with them loses auto-mode review.
- [tests] Keep gate tests in python unittest with HOME isolated; a shell quoting slip made the hook exit silently and hid it.
- [accounts] Never assume which account a keychain login belongs to; ask /api/oauth/profile (`gate.py accounts`).
