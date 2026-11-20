#!/bin/sh
# Full check: compile + gate state-machine tests (isolated HOME, fake usage).
set -e
cd "$(dirname "$0")"
python3 -I -m py_compile gate.py
python3 -m unittest -v tests/test_gate.py
if cmp -s gate.py ~/.claude/usage-gate/gate.py && cmp -s skill/SKILL.md ~/.claude/skills/usage-gate/SKILL.md; then
  echo "installed copy: matches repo"; else echo "installed copy: DIFFERS from repo (run ./install.sh)"; fi
