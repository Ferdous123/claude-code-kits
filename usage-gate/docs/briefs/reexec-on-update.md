# Brief: reexec-on-update
Model: middle
Branch: reexec
Touch: gate.py, install.py, tests/**, docs/**
Done when: `./check.sh`

## Task
Live 2026-11-21 12:17: a `listen` started at 10:27 (before the buttons update was installed) kept running old code; its polls took a button press off the Telegram queue and dropped it, so the button spun. Long-running loops must pick up a new gate.py.

1. In every long-running loop (`listen`, `wait`, `timer`, and any other loop that polls), at each iteration compare the size+mtime (or hash) of `__file__` with the value at start; if it changed, log "gate.py updated: restarting <cmd>" and `os.execv(sys.executable, [sys.executable, "-I", __file__] + original argv)` so the process keeps its pid and stdout (the background task stays the same) and continues with the new code. Keep any state the loop needs across the restart (e.g. the timer's original asked_at / deadline, listen's seen command seq) by passing it on the command line or re-reading it from the session file; the timer must not restart its countdown.
2. Tests, red on old behaviour: run `listen` (and `timer`) from a copy of gate.py in the isolated HOME, then rewrite the copy with an added line that writes a marker file at import; the marker appears within a few seconds and the process is still the same pid; the timer still packs at the original deadline.
