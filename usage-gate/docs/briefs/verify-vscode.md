# Brief: verify-vscode
Model: middle
Branch: main
Touch: gate.py, tests/**, skill/**, install.sh, docs/**
Done when: `./check.sh`

## Task
Verify usage-gate from a VS Code Claude Code session (CLI account the CLI account), then fix what fails, test first.

1. `python3 -I ~/.claude/usage-gate/gate.py status` will say "not armed": fine. Run `/usage-gate 99 100 ask=1`. Expect it to name the CLI account and print figures. If it names the app account or fails, check how the session account is resolved (`session_service()` in gate.py: env CLAUDE_CODE_ACCOUNT_UUID, else CLAUDE_CONFIG_DIR, else default login) and list the names (not values) of CLAUDE_* env vars.
2. Confirm the hook fires and injects `--session`: `tail ~/.claude/usage-gate/gate.log` shows "hook input keys" lines for this session id.
3. Soft path: re-arm with `--soft` just under the current max figure (`--ask 1`). Next tool call must be denied with "USAGE GATE"; start `gate.py timer` in the background; its exit must wake the session; the next call must be denied with "packing up".
4. Wait path: run `gate.py wait` in the background, then edit the session file's `soft` above the current figure (sessions/<id>.json). Within ~5 min `wait` must exit with "limits are back" and wake the session.
5. Hard path with an agent: arm `--soft 1 --hard <just under current>`, spawn one small agent told to run `echo x` then edit a temp file; its tool call must be denied with HARD STOP and it must write agent-<id>.md in the notes folder.
6. `/usage-gate off`. Delete test notes. Record outcomes in the handoff.
Do not touch ~/.claude/settings.json beyond ./install.sh; never refresh the default login.
