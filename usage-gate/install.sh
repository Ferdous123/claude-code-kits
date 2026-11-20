#!/bin/sh
# Install usage-gate from this repo: script, skill, global hooks, account map. Idempotent.
set -e
cd "$(dirname "$0")"
mkdir -p ~/.claude/usage-gate ~/.claude/skills/usage-gate
cp gate.py ~/.claude/usage-gate/gate.py
cp skill/SKILL.md ~/.claude/skills/usage-gate/SKILL.md
python3 -I - <<'PY'
import json, os
p = os.path.expanduser("~/.claude/settings.json")
d = json.load(open(p)) if os.path.exists(p) else {}
cmd = {"type": "command", "command": "python3 -I ~/.claude/usage-gate/gate.py hook", "timeout": 20}
for ev in ("PreToolUse", "PostToolUse"):
    lst = d.setdefault("hooks", {}).setdefault(ev, [])
    if not any("usage-gate/gate.py" in h.get("command", "") for g in lst for h in g.get("hooks", [])):
        lst.append({"matcher": "*", "hooks": [cmd]})
json.dump(d, open(p, "w"), indent=2)
print("hooks ok in", p)
PY
python3 -I ~/.claude/usage-gate/gate.py accounts --session install
