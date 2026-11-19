# hk: handoff kit — build spec

One file, `hk.py`, Python 3.8+, standard library only, no network except `git fetch`. Works on macOS, Linux, Windows. Plain code: obvious, spelled out, a docstring on every function. It runs from anywhere inside a git repository and finds the root with `git rev-parse --show-toplevel`.

Purpose: every check a rule can make is made here, not by a language model. Output is short by design: the caller pays for every character.

## 1. Files in a project

| Path (default, all overridable in `hk.json` at the root) | Tracked | What |
|---|---|---|
| `docs/HANDOFF.md` | yes | Current state, strict format (section 2). |
| `docs/CONTEXT_MAP.md` | yes | Routing table: code paths → required reading (section 4). Optional. |
| `docs/CHECKS.tsv` | yes | Evidence ledger (section 5). |
| `docs/briefs/*.md` | yes | Task briefs (section 3). |
| `docs/DECISIONS.md` | yes | Decision log, searched for ids. |
| `docs/lessons/<area>.md` | yes | Lessons by area. |
| `.hk/` | no, must be git-ignored | Logs, local ledger, read log. |

`hk.json` keys and defaults: `handoff`, `context_map`, `ledger`, `briefs_dir`, `decisions`, `lessons_dir`, `remote` ("origin"), `branch` ("dev" if it exists on the remote, else the remote's default), `doc_paths` (["docs/", "START_PROMPT.md"]), `budget_handoff` (12000), `budget_boot` (40000), `read_budget` (400000), `main_check` ("full").

## 2. HANDOFF.md, strict format

Level-2 headings, exactly these names. Order: boot sections first, then on-demand sections.

```
# Handoff

## Anchor
Goal: <one sentence>
Milestone: <name>
Ends when: <evidence that ends the milestone>
Workflow: <one line>
Out of scope: <one line>
Decides: <who>

## State
Main: <branch> @ <sha> check:<pass|fail|not-run>
Anchor change: <decision id>        (only when a fixed anchor line changed)
<up to 10 more free lines>

## Next
| # | Step | Brief | Model | Done when |

## Questions
| # | Question | Blocks |

## How to work
<bullets>

## Read first
| Ref |

## Read when
| Trigger | Ref |

## Never read
| Path |

## Exists
| Piece | Status | Evidence | Paths |

## Parked
| Branch | Tip | Verified | Not verified | Next |

## Findings
| Sev | Finding | File |

## Decisions
<free>

## Machines
<free>
```

Boot sections: Anchor, State, Next, Questions, How to work, Read first, Read when, Never read. On-demand: Exists, Parked, Findings, Decisions, Machines. A table may be empty (header and separator only). Table cells may hold backticks; strip them when reading a value.

**Ref syntax** (used everywhere a file is cited):
- `path` — whole file.
- `path#Heading text` — one section: the heading whose text equals the given text (case-insensitive, leading numbering like "5." counts as text), or failing that the single heading that starts with it. Zero or several matches is an error. The section runs to the next heading of the same or a higher level.
- `path@tail:N` — last N lines.

**Evidence cell** in Exists: `<sha>` (commit must exist) or `check:<sha>` (commit must exist and the ledger must hold a pass row for it). **Paths cell**: comma-separated globs the piece covers; may be empty.

**Globs**: a pattern ending in `/**` matches everything under that folder; otherwise `fnmatch.fnmatchcase` on the forward-slash path.

## 3. Brief format

```
# Brief: <id>
Model: <smallest|middle|strongest or a model name>
Branch: <branch>
Touch: <glob>, <glob>
Done when: `<command>`
Read: <ref>; <ref>          (optional, in addition to the routing table)

## Task
<free>
```

## 4. CONTEXT_MAP.md

One table: `| Paths | Read | Lessons |`. Paths: comma-separated globs. Read: refs separated by `;`. Lessons: area names separated by `,`; each means the whole file `<lessons_dir>/<area>.md` if it exists.

## 5. Ledger

`docs/CHECKS.tsv`, tab-separated, header row: `time machine commit dirty label exit secs log_sha cmd row_sha`.
- `time` UTC ISO seconds. `machine` from env `HK_MACHINE`, else hostname. `dirty` is 1 if `git status --porcelain` was non-empty when the command started. `log_sha` first 12 hex of sha256 of the full output. `row_sha` first 12 hex of sha256 of the other nine fields joined by tab. Tabs and newlines in `cmd` become spaces.
- A **pass row** for commit X and label L: commit equals X (full sha), label L, exit 0, dirty 0, and `row_sha` recomputes correctly. A row whose `row_sha` does not recompute is reported as forged by `hk check`.
- The local ledger `.hk/ledger.tsv` has the same format.

## 6. Commands

Exit codes: 0 ok, 1 a check failed, 2 usage or format error. Errors go to stderr as one line. No colour, no banners.

**`hk boot [--measure] [--ref <gitref>]`** — prints, in order: the boot sections of the handoff verbatim; then for each Read-first ref a line `=== <ref> (<n> chars)` and its content; then an index, one line per on-demand section: `<name>: <rows> rows, <chars> chars — hk get <handoff>#<name>`; last line `BOOT <n> chars, budget <b>`. `--measure` prints only a size table (one line per part) and the BOOT line. Exit 1 if over budget. `--ref` reads every file from that git ref instead of the working tree (default: working tree).

**`hk get <ref>... [--ref <gitref>]`** — prints each ref's content under a `=== <ref> (<n> chars)` line. Appends one row per ref to `.hk/reads.tsv` (`time session source path chars`, source `hk`, session from env `HK_SESSION` or `-`).

**`hk for <path>... [--print]`** — matches the paths against CONTEXT_MAP and prints the deduplicated reading list, one ref per line with its size, and a total. `--print` prints the contents too. No CONTEXT_MAP: prints nothing, exit 0.

**`hk pack <brief> [--measure]`** — everything an agent needs, as one output: the brief verbatim; the anchor lines Goal, Workflow, Out of scope; the reading (routing-table matches for the Touch globs — a row matches if any of its globs and any Touch glob could cover a common path; implement as: either glob matches the other's literal prefix, or they are equal — plus the brief's Read refs), each under a `===` line; then the fixed report shape:
```
REPORT (20 lines at most)
status: <word>
branch: <name> @ <sha>
changed: <files>
check: <last lines of the Done-when command>
questions: <none or list>
```
Last line `PACK <n> chars`.

**`hk run <label> [--record] [--tail N] -- <command...>`** — runs the command through the shell from the repository root, writes the full output to `.hk/logs/<time>-<label>.log`, appends a ledger row (`--record`: tracked ledger; otherwise local ledger), prints the last N lines (default 15) and then `RESULT <pass|fail> exit=<n> commit=<sha7> dirty=<0|1> secs=<s> log=<path>`. Exits with the command's exit code (capped to 1 for non-zero).

**`hk verify <brief> [--ref <branch>]`** — scope check and acceptance for a finished task. Branch defaults to the brief's Branch. (1) `git diff --name-only <merge-base of branch and integration branch>..<branch>`; every file must match a Touch glob; print `SCOPE ok (<n> files)` or `SCOPE FAIL <file>` per offender. (2) Only if the branch is the current checkout: run the Done-when command as `hk run verify-<id>`, print its RESULT line. Otherwise print `ACCEPT skipped: check out <branch>`. Exit 1 on any failure.

**`hk facts`** — evidence for whoever writes the handoff, short: integration branch local and remote tip; commits since the last commit that touched the handoff file (`git log --oneline`, at most 40); remote branches not merged into the integration branch, each with its tip; the last 5 rows of each ledger; rows of `.hk/reads.tsv` whose path is not covered by the current Read first / Read when refs (deduplicated, with counts); `git status --porcelain` line count per worktree.

**`hk check [--no-fetch]`** — the handoff checker. Runs `git fetch <remote>` unless told not to. Reads the handoff and everything it cites **from `<remote>/<branch>`**, never from the working tree, so it proves what a fresh clone would see. Checks, each with a stable id:

| Id | Fails when |
|---|---|
| C1 | any worktree has uncommitted or untracked changes |
| C2 | local `<branch>` tip differs from `<remote>/<branch>` |
| C3 | handoff missing at the remote ref, a required section or table header is missing or malformed, or it is over `budget_handoff` |
| C4 | an Anchor key is missing or empty; or Goal, Workflow, Out of scope or Decides differs from the previous version of the handoff (the version before the latest commit that touched it on the remote ref) and State has no `Anchor change: <id>` whose id appears in the decisions file. No previous version: skip the comparison |
| C5 | the State `Main:` line is missing or malformed; its sha is not an ancestor of the remote tip; files changed between that sha and the tip fall outside `doc_paths`; or it says `check:pass` and the tracked ledger has no pass row for that sha with label `main_check` |
| C6 | Next has no rows; or row 1 has no Brief or no Model; or a named brief does not exist at the remote ref or does not parse; or a ref inside it does not resolve |
| C7 | a Read first or Read when ref does not resolve at the remote ref |
| C8 | the boot output built from the remote ref is over `budget_boot` |
| C9 | an Exists row has status `done`; its evidence sha does not exist or is reachable from no remote branch; it says `check:` with no pass row (any label); or its Paths changed between the evidence sha and the remote tip (`changed since verified`) |
| C10 | a Parked branch is not on the remote, or its Tip cell is not a prefix of that remote branch's tip |
| C11 | a CONTEXT_MAP ref does not resolve at the remote ref |
| C12 | `.hk/` is not git-ignored |
| C13 | a ledger row's `row_sha` does not recompute (forged or hand-edited row) |
| C14 | the handoff text matches a secret pattern: `(?i)(api[_-]?key|secret|token|password)\s*[:=]\s*\S{8,}` |

Output: one line per failure, `FAIL <id> <what, with the offending value>`; a `WARN` line when State says `check:fail` or `check:not-run`; last line `HANDOFF OK <branch>@<sha7> boot=<n>` or `HANDOFF FAIL <count>`. All checks run even after a failure. Exit 0 only with no FAIL.

**`hk export <dir>`** — writes the tree of `<remote>/<branch>` into `<dir>` (must not exist or be empty) using `git archive` and the `tarfile` module. For the cold-start test.

**`hk quiz`** — prints the fixed cold-start questions. **`hk key [--ref <gitref>]`** — prints the answers taken mechanically from the handoff: the Goal and Milestone lines; row 1 of Next; the Read-first refs; the Never-read paths; the State line; the number of rows in Parked and in Findings.

Questions, fixed text:
```
1. What is the project's goal and current milestone?
2. What is step 1, which brief holds it, and which model does it?
3. Which files or sections do you read before starting, in order?
4. What must you never read?
5. What is the state of the main branch check?
6. How many parked branches and open findings are there, and how did you find out?
```

**`hk hook-read`** and **`hk hook-anchor`** — hook entry points; specified separately in section 8 (to be built after the hook facts are confirmed; leave stubs that exit 0).

## 7. Tests (`tests/test_hk.py`, `unittest`, standard library)

Each test builds a throwaway repository with a bare "remote" in a temp folder (`git init --bare`, clone, commit, push). A helper `make_project(tmp)` creates a valid project: a handoff in the strict format, one brief, a context map, two doc files with headings, a ledger with a real pass row made by `hk run full --record -- python -c "print('ok')"`, one parked branch, `.hk/` ignored. `hk check` on it must print `HANDOFF OK`.

Fault injection: one test per row, each injects exactly one fault into the valid project, commits and pushes where the fault needs it, and asserts `hk check` exits 1 and names the expected id:

| Fault | Expect |
|---|---|
| untracked file in the working tree | C1 |
| a commit not pushed | C2 |
| a required section deleted | C3 |
| handoff padded past the budget | C3 |
| Goal line reworded, no Anchor change line | C4 |
| Goal reworded with `Anchor change: D-9` and D-9 in the decisions file | passes |
| State sha replaced by one that is not an ancestor | C5 |
| a code file committed after the State sha | C5 |
| `check:pass` with the ledger row removed | C5 |
| `check:pass` where the only row was made on a dirty tree | C5 |
| row 1 of Next names a brief that does not exist | C6 |
| Read-first ref to a heading that was renamed | C7 |
| Read-first ref to a file that exists only untracked in the working tree | C7 |
| Read-first pointing at a file larger than the boot budget | C8 |
| Exists status `done` | C9 |
| Exists `check:<sha>` with no pass row | C9 |
| Exists paths changed after the evidence sha | C9 |
| Parked branch deleted on the remote | C10 |
| Parked tip cell stale | C10 |
| CONTEXT_MAP ref to a missing heading | C11 |
| `.hk/` removed from `.gitignore` | C12 |
| ledger row edited by hand (exit 1 changed to 0) | C13 |
| `password: hunter2hunter2` in the handoff | C14 |

Also test: ref resolution (exact, prefix, ambiguous, missing, tail); `boot` content and index lines; `boot --measure` over budget exits 1; `for` and `pack` routing; `run` writes a correct row and exit code; `verify` scope pass and fail; `get` appends to the read log; `export` writes the remote tree and not untracked files; `key` output.

Run with `python3 -m unittest discover -s tests -v` from the kit folder. All tests must pass on the machine that builds it. Tests must not touch anything outside their temp folders.

## 8. Hooks

Both read one JSON object from stdin (Claude Code hook input) and must never crash the session: on any internal error, exit 0 silently. They find the repository root from the JSON `cwd` field. Fields used: `session_id`, `cwd`, `hook_event_name`, `tool_name`, `tool_input`, and `agent_id` (present only for a subagent's call).

**`hk hook-read`** — for `PreToolUse` on `Read|Grep|Glob`.
1. Take the path: `tool_input.file_path` for Read, `tool_input.path` for Grep and Glob (missing: exit 0). Make it relative to the root with forward slashes. Outside the repository: exit 0.
2. If it lies under a path in the working tree handoff's Never read table (prefix match on folders, glob match otherwise): print to stderr `Never-read path: <path>. It is on the handoff's Never read list. Ask the owner before opening it.` and exit 2. This applies to the main session and to subagents.
3. Otherwise, for Read only: append a row to `.hk/reads.tsv` (`time session source path chars`), source `main` or `agent` (agent when `agent_id` is present), chars = file size, or, when `limit` is given, `limit` × 80 capped at the file size.
4. Main session only: sum `chars` of this session's `main` rows. When the sum first crosses `read_budget` (and again at each further 25%), print this JSON to stdout and exit 0: `{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow", "additionalContext": "Read budget: <n> of <budget> characters read by the main session. Hand off at the next clean point, or delegate further reading."}}`. Record the last level announced in `.hk/budget.<session_id>` so each level is announced once. In every other case print nothing.

**`hk hook-anchor`** — for `SessionStart` with matcher `compact`. Prints the handoff's Anchor section, the State `Main:` line and row 1 of Next, then the line `Re-read the anchor. Work the Next table does not call for goes to the backlog.` Exit 0. No handoff: print nothing.

Settings entry (exec form, so it works on Windows, macOS and Linux; the interpreter name is the project's choice):
```json
{"hooks": {
  "PreToolUse": [{"matcher": "Read|Grep|Glob", "hooks": [{"type": "command", "command": "python3", "args": ["${CLAUDE_PROJECT_DIR}/.claude/skills/handoff/hk.py", "hook-read"], "timeout": 10}]}],
  "SessionStart": [{"matcher": "compact", "hooks": [{"type": "command", "command": "python3", "args": ["${CLAUDE_PROJECT_DIR}/.claude/skills/handoff/hk.py", "hook-anchor"], "timeout": 10}]}]
}}
```

Tests: feed each hook JSON on stdin in a throwaway project and assert: a never-read path exits 2 with the message; an allowed Read exits 0 and appends one row with the right source; an agent call does not count toward the budget; crossing the budget prints the JSON once and not again on the next read; Grep on a never-read folder exits 2; malformed stdin exits 0; `hook-anchor` prints the anchor and row 1.

## 9. Amendments (found by running the tool on a real project)

**A1. Branch-qualified refs.** A ref may start with `<branch>:`, as in `feature-x:docs/reviews/x.md#Findings`. It is branch-qualified when the text before the first `:` is the name of a branch on the remote (`refs/remotes/<remote>/<name>` exists). Its content is always read from `<remote>/<name>`, in every command and whatever `--ref` says. `hk check` C7 (and C6, C11) fail when that branch, file or heading is missing.

**A2. Refs stay inside the repository.** A ref path that is absolute, contains a `..` segment, or begins with `-` is a format error (exit 2; in `hk check`, a FAIL of the check that met it). No command reads a file outside the repository root because of a ref.

**A3. Never read is enforced, with one exception.** `hk get` and `hk hook-read` refuse a path under a Never-read entry, unless that exact file is cited by a ref in the handoff's Read first or Read when tables. `hk get` refusal: one stderr line, exit 2.

**A4. Done when may hold several commands.** Every backticked segment on the `Done when:` line is a command. `hk verify` runs them in order, prints one RESULT line each, and stops at the first failure. A brief with no backticked command on that line does not parse.

**A5. Legacy briefs.** In C6, the brief of row 1 must exist and parse. A brief named in a later row must exist (FAIL if missing); if it exists but does not parse, print `WARN C6 <path>: not in brief format` and do not fail.

## 10. Self-tuning (cheap, evidence-driven, never edits its own code)

Principle: the tool measures itself with no model involved; a rule turns repeated evidence into a short proposal; a person or the handoff session applies it; the next measurements show whether it helped; what did not help is proposed for removal. Only data files change (read map, context map, learned notes). `hk.py`, `SKILL.md` and `FORMAT.md` are never changed by this mechanism.

**Files.** `docs/HK_METRICS.tsv` (tracked, in `doc_paths` by default): header `time machine commit boot_chars handoff_chars first_fails offmap fetched unused_when budget_level coldstart learned_ids`. `docs/HK_LEARNED.md` (tracked, project notes) and `<folder of hk.py>/LEARNED.md` (notes that travel with the skill): one table `| Id | Added | Note | Evidence | Last seen |`, at most 20 rows and 2 000 characters each file. `.hk/check_runs.tsv` (untracked): one row per `hk check` run: time, commit, FAIL ids joined by `,`.

**`hk check`** additionally: appends its row to `.hk/check_runs.tsv`; new check **C15** fails when a learned file exceeds its caps or a row has an empty Evidence cell; prints `WARN version: project copy differs from <path>` when another `hk.py` exists at `~/.claude/skills/handoff/hk.py` with a different sha256.

**`hk record [--coldstart N/M]`** — run once after `HANDOFF OK`. Appends one metrics row: boot and handoff sizes; `first_fails` = the union of FAIL ids from this handoff's check runs before the first OK (rows in `.hk/check_runs.tsv` since the previous metrics row); `offmap` = distinct files read outside the read map this session, joined by `;` (at most 10); `fetched` = refs fetched with `hk get` (at most 10); `unused_when` = number of Read-when refs not fetched this session; `budget_level` = highest read-budget level announced; `coldstart` as given or `-`; `learned_ids` = ids of the active learned rows. Then truncates `.hk/reads.tsv` and `.hk/check_runs.tsv`.

**`hk tune`** — reads the last 10 metrics rows and prints at most 12 lines, each `PROPOSE <action> — evidence: <what, with counts>`; applies nothing; exit 0. Rules:
- T1: a file in `offmap` in 2 or more rows → `add Read-when row for <file>`.
- T2: a Read-when ref absent from `fetched` in all of the last 6 rows (and at least 6 rows exist) → `drop Read-when row <ref>`.
- T3: the same id in `first_fails` in 2 or more of the last 5 rows → `add learned note: <id> keeps failing first time: <the check's one-line meaning>`.
- T4: `boot_chars` rising in each of the last 3 rows, or above 80% of budget → `trim Read first: largest part is <part> (<n> chars)`.
- T5: `coldstart` below full marks in 2 of the last 3 rows → `rewrite the boot sections the cold-start reader got wrong`.
- T6: a learned row whose id has been active for 5 or more rows while the thing it targets did not improve (for a T3 note: the id still appears in `first_fails` in 2 of the last 3 rows) → `remove learned note <id>: no effect`.
- T7: a learned row not seen (its check id absent from `first_fails`) for 10 rows → `retire learned note <id>: no longer needed`.
- T8: `budget_level` of 1 or more in 2 of the last 3 rows → `delegate more reading: main session over its read budget`.

**`hk learned`** — prints the active rows of both learned files, project first, then `LEARNED <n> rows, <chars> chars`.

**`hk version`** — prints the version constant and the first 12 hex of the sha256 of `hk.py`.

**Boot index wording.** The index after the Read-first content starts with the line `NOT LOADED. Fetch a section when a step or question needs it:` and each index line gives the full command exactly as it must be typed, built from how `hk` was invoked (for example `python3 .claude/skills/handoff/hk.py get docs/HANDOFF.md#Parked`), with the section's row count and size.

Tests: one per rule T1 to T8 from a hand-written metrics file (fires with the evidence, silent without); `record` writes the right row from seeded `.hk` logs and truncates them; C15 on an over-long learned file; the version warning; the boot index wording.
