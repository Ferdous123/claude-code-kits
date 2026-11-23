# claude-code-kits

Skills and tools for working with Claude Code. Each kit stands alone.

| Kit | What it does | Needs |
|---|---|---|
| [handoff](#handoff) | End-of-session handoffs a fresh session on any machine continues from, checked by `hk.py` | Python 3.8+, git |
| [usage-gate](usage-gate/README.md) | Stops sessions cleanly near the 5-hour or weekly plan limit and resumes after the reset; optional Telegram alerts and phone commands | Python 3, a Claude Code login |
| [research-workflow](research-workflow/README.md) | Evidence-first research workflow from plain files: papers become located evidence cards, gaps carry a scope line, methods are assembled as cited hypotheses; checks by script, local models only after a measured test | Python 3.8+; optional PyMuPDF (for PDFs) and a local model runner |

Layout: the handoff kit sits at the repository root (`skill/handoff/`, `docs/SPEC.md`, `tests/`); `usage-gate/` and `research-workflow/` are self-contained folders, each with its own README. `hk.json` at the root configures `hk` for the working notes in `usage-gate/docs/`; no kit needs it to be installed. Every kit is installed by copying or running files from this repository; nothing is published as a package.

## handoff

A Claude Code skill for end-of-session handoffs, plus one Python tool, `hk.py`.

- The skill tells the assistant how to close a working session.
- It writes a short, strictly formatted `docs/HANDOFF.md` in your project.
- `hk` makes every check a rule can make, so no language model is needed for them.
- `hk check` reads the handoff from the pushed git state, as a fresh clone would see it.
- A new session on any machine then continues from git alone.

### Install

Copy the `skill/handoff` folder to `~/.claude/skills/handoff/` (all projects) or to
`<your project>/.claude/skills/handoff/` (one project). `hk` runs from anywhere
inside a git repository: `python3 <skill folder>/hk.py <command>`.

### Use

At the end of a working session, type `/handoff` (or say "handoff now" or "make handoff"). The skill settles unfinished work, writes `docs/HANDOFF.md`, runs `hk check`, and pushes. The first time in a project, and only with the owner's agreement, it also creates `hk.json`, adds `.hk/` to `.gitignore`, and adds the two hooks described in `skill/handoff/FORMAT.md` to `.claude/settings.json`. A later session starts with `hk boot`.

### Commands

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

### Tests

From the repository root (about 1.5 minutes on Linux):

    python3 -m unittest discover -s tests

### Requirements

Python 3.8 or newer (standard library only) and git.

### Status

- All 187 tests in `tests/test_hk.py` pass on Linux with Python 3.12. An earlier version of this section recorded a pass on macOS; that run was not repeated.
- Not tested on Windows.
- The hooks have not yet been tried in a live session.

## License

MIT, for the whole repository. See `LICENSE`.
