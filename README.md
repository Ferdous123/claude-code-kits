# handoff-kit

A Claude Code skill for end-of-session handoffs, plus one Python tool, `hk.py`.

- The skill tells the assistant how to close a working session.
- It writes a short, strictly formatted `docs/HANDOFF.md` in your project.
- `hk` makes every check a rule can make, so no language model is needed for them.
- `hk check` reads the handoff from the pushed git state, as a fresh clone would see it.
- A new session on any machine then continues from git alone.

## Install

Copy the `skill/handoff` folder to `~/.claude/skills/handoff/` (all projects) or to
`<your project>/.claude/skills/handoff/` (one project). `hk` runs from anywhere
inside a git repository: `python3 <skill folder>/hk.py <command>`.

## Commands

Details are in [docs/SPEC.md](docs/SPEC.md); file formats are in `skill/handoff/FORMAT.md`.

| Command | What it does |
|---|---|
| `boot` | Prints the handoff's boot sections and the files it says to read first, with a size line. |
| `get` | Prints chosen files or sections (`path#Heading`, `path@tail:N`) and logs the reads. |
| `for` | Lists the files to read before touching given paths, from the context map. |
| `pack` | Builds everything an agent needs for one task brief, plus a fixed report shape. |
| `run` | Runs a command, saves its log, and records a ledger row with the commit and result. |
| `verify` | Checks a finished task's changed files against its brief, then runs its acceptance command. |
| `facts` | Prints the git evidence needed to write the handoff: tips, unmerged branches, ledger rows. |
| `check` | The handoff checker: verifies the handoff and everything it cites at the remote branch. |
| `export` | Writes the remote branch's tree to a folder, for a cold-start test. |
| `quiz` | Prints the fixed cold-start questions. |
| `key` | Prints the answers to the quiz, taken mechanically from the handoff. |
| `record` | Appends one metrics row after a successful check. |
| `tune` | Proposes changes (never applies them) from the last metrics rows. |
| `learned` | Prints the active learned rows of the project and user files. |
| `version` | Prints the tool version and a short hash of `hk.py`. |
| `hook-read` | Hook for `PreToolUse` on `Read|Grep|Glob`: tracks reading against a budget. |
| `hook-anchor` | Hook for `SessionStart` after compaction: re-prints the anchor and the next step. |

## Tests

From the repository root (takes a few minutes):

    python3 -m unittest discover -s tests

## Requirements

Python 3.8 or newer (standard library only) and git.

## Status

- 169 tests pass on macOS with Python 3.15.
- Not tested on Windows.
- The hooks have not yet been tried in a live session.

## License

No license chosen yet.
