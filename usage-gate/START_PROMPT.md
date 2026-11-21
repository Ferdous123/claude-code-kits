# Start prompt: usage-gate

Repo: ~/Developer/handoff-kit (GitHub Ferdous123/claude-code-kits); usage-gate lives in usage-gate/.

1. `cd ~/Developer/handoff-kit && git pull -q && python3 ~/.claude/skills/handoff/hk.py boot`. Its output is your whole reading: it already contains the handoff sections and the Read-first files. Do not open HANDOFF.md, the context map, decisions or any other file yourself.
2. When a Read-when trigger fires or a step names a file, `hk get <ref>` for that section only.
3. Do Next row 1. Explore through a smallest-model agent (answers ≤15 lines with file:line), not by reading widely.
4. Every task is a brief; finished task → `hk verify <brief>`; long commands via `hk run`.
5. Re-read the Anchor before each step. Other work goes to the backlog.
6. Hand off with /handoff at the next clean point, when the owner says so, or after one summary.
(`hk` = `python3 ~/.claude/skills/handoff/hk.py`)
