# Brief: port-linux-windows
Model: middle
Branch: port
Touch: gate.py, tests/**, skill/**, install.sh, install.py, check.sh, publish.sh, README.md, docs/**
Done when: `./check.sh`

## Task
Make the gate work in VS Code on Linux ARM64 (DGX Spark) and Windows, as well as on the Mac (D10, D12). Test first for each item; run `./check.sh` after each.

1. Write tests with a fake `~/.claude/.credentials.json` in the isolated HOME and no keychain: login lookup reads that file read-only (never writes or refreshes it, D12), and a missing file gives a clear "no login" status, not a crash. Make them pass in gate.py.
2. Launcher: hook command and every message that names a command use `python` on Windows and `python3` elsewhere. Test the choice with an injected platform value.
3. Guard `os.fchmod` (absent on Windows) so session and telegram.json writes do not raise; mode 600 stays on POSIX.
4. O_EXCL lock fallback: stale age at most the wait timeout. Make `save_sess` honour `got` (the TODO in gate.py). Test with a stale lock file and with a lost-lock case.
5. Installer: move install.sh's logic into `install.py` (stdlib, runs on macOS, Linux, Windows); install.sh becomes a one-line wrapper. The hook command it writes uses absolute paths (`sys.executable` and the installed gate.py), so it needs neither `python3` on PATH nor `~` expansion; replace an older usage-gate hook entry instead of adding a second. The accounts step is skipped with a one-line note where there is no keychain. Tests: run install.py with an isolated HOME and an existing settings.json (keeps other hooks, no duplicate, absolute paths).
6. Update README.md and skill/SKILL.md where they name macOS-only behaviour. Add the new decision to docs/DECISIONS.md if you make a choice the owner did not.
7. Lead, with the owner (not the builder): after merge, `./publish.sh` and push claude-code-kits (D17); on the DGX Spark and on Windows clone it and run `python3 usage-gate/install.py` (Windows: `python`); then in VS Code: arm, status, a Telegram alert (copy telegram.json or run setup), and /continue from the phone. Record results in the handoff, not in this brief.
