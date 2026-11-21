# Formats `hk` reads

Open this only when writing one of these files. `hk check` enforces all of it.

## Refs

Wherever a file is cited:

| Form | Means |
|---|---|
| `path` | the whole file |
| `path#Heading text` | one section: the heading with exactly that text (case-insensitive, numbering such as `4.` included), or the single heading that starts with it; it runs to the next heading of the same or a higher level |
| `path@tail:N` | the last N lines |
| `branch:path…` | any of the above, read from that branch on the remote (for files that exist only on a parked branch) |

Paths are relative to the repository root. No absolute paths, no `..`, no leading `-`. One exception: an absolute path listed in `hk.json` `external_refs` (a process guide kept outside the repository) may be cited in Read first and Read when. It is read from the local disk; on a machine where the file is absent `hk boot`, `hk pack` and `hk check` print a WARN and go on.

## `docs/HANDOFF.md`

Level-2 headings with exactly these names. The first eight are printed by `hk boot`; the last five are fetched on demand, so put bulk there.

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
Anchor change: <decision id>     (only when Goal, Workflow, Out of scope or Decides changed)
Running: <job, machine, where its output goes>     (zero or more; detached jobs the next session must know; not counted in the 10)
<up to 10 more lines>

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

Rules the checker applies:

- **Size.** The file stays under `budget_handoff` characters; `hk boot` output stays under `budget_boot`.
- **Anchor.** Goal, Workflow, Out of scope and Decides must equal the previous handoff's, unless State has `Anchor change: <id>` and that id is in the decision log. Milestone and Ends when may change when a gate passes.
- **State.** `<sha>` is the commit the main check ran on. Only documentation paths (`doc_paths`: folders ending in `/`, or single files such as `.gitignore`) may change between it and the pushed tip. `check:pass` needs a ledger pass row for that commit with the label in `main_check`.
- **Next.** At least one row. Row 1 names a brief in the brief format and a model. Later rows may leave Brief empty.
- **Read first, Read when.** Every ref must resolve in the pushed state. A file cited here may be opened even if it sits under a Never-read folder.
- **Exists.** Status is a word tied to an artefact, never `done`. Evidence is `<sha>` (the commit exists on the remote) or `check:<sha>` (and the ledger holds a pass row for it). Paths is a comma-separated list of globs the piece covers; if any of them changed after the evidence commit, the row fails as *changed since verified*.
- **Parked.** The branch exists on the remote and Tip (7 or more hex characters) is the start of its tip there.
- **Globs.** `folder/**` covers everything under the folder; anything else is matched with shell-style wildcards on the whole path.

## Brief (`docs/briefs/…`)

```
# Brief: <id>
Model: <smallest|middle|strongest or a model name>
Branch: <branch>
Base: <commit or ref>       (optional; scope is measured from it)
Touch: <glob>, <glob>
Done when: `<command>` `<second command>`
Read: <ref>; <ref>          (optional; added to what the context map gives)

## Task
<free>
```

`hk pack` hands this to the agent with its reading. `hk verify` checks that the branch changed only files matching Touch, then runs each Done-when command in order.

- `## Task` must be the first `##` section; any other first heading is an error that names it.
- Every Touch item must be a glob (no spaces or quotes); prose in Touch fails with the item named.
- `hk verify` measures scope from `Base:` if given, else from the commit where the branch left the integration branch; when the branch later merged the integration branch, only its own commits count, so merged-in files are not SCOPE FAIL.

## `docs/CONTEXT_MAP.md`

One table. It rarely changes; the handoff's read-map feedback step adds rows.

```
| Paths | Read | Lessons |
| src/ingest/** | docs/guide/03_INGEST.md#4. Queue; docs/guide/06_SECURITY.md#2. Uploads | ingest |
```

Paths: globs, comma-separated. Read: refs separated by `;`. Lessons: area names; each loads `docs/lessons/<area>.md`.

## Ledger (`docs/CHECKS.tsv`)

Written only by `hk run <label> --record -- <command>`. Never edit it: each row carries a hash of its own fields, and a row that does not recompute fails the check. A row counts as a pass only if the tree was clean when the command started.

## `hk.json` (repository root, all keys optional)

| Key | Default |
|---|---|
| `remote`, `branch` | `origin`; `dev` if the remote has it, else the remote's default branch |
| `handoff`, `context_map`, `ledger` | `docs/HANDOFF.md`, `docs/CONTEXT_MAP.md`, `docs/CHECKS.tsv` |
| `briefs_dir`, `decisions`, `lessons_dir` | `docs/briefs`, `docs/DECISIONS.md`, `docs/lessons` |
| `doc_paths` | `["docs/", "START_PROMPT.md"]` |
| `budget_handoff`, `budget_boot`, `read_budget` | 12000, 40000, 400000 characters |
| `main_check` | `full` |
| `procedure` | none. A file, or `{"path": ..., "lines": "21-29"}` (absolute or repo-relative); appended to every `hk pack` |
| `checks` | `{}`. Label to command, e.g. `{"full": "./check.sh"}`; `hk run full` with no `--` command runs it. `hk run` with no command and no entry exits non-zero |
| `machines_check` | none. A command run before `hk run`; if it fails, `hk run` refuses to start and prints why |
| `external_refs` | `[]`. Absolute paths that Read first / Read when may cite (see Refs) |
| `budget_ref`, `budget_pack` | 0 (off). `hk pack` WARNs for each ref above `budget_ref` characters (cite `file#section` instead) and FAILs (exit 1) when the pack is above `budget_pack` |
| `inline_ref_max` | 0 (off). `hk pack` lists a ref whose content is above this many characters (size, how to read a section, its headings) instead of inlining it, so whole source files or big plans named in Read do not bloat packs |

`hk facts` also lists the last 10 rows of `.hk/jobs.tsv` (free tab-separated rows for detached jobs) beside the State `Running:` lines.

## Hooks (`.claude/settings.json`)

Adjust the path if the skill is installed in the user's folder, and the interpreter name if the machine calls it `python`.

```json
{"hooks": {
  "PreToolUse": [{"matcher": "Read|Grep|Glob", "hooks": [{"type": "command", "command": "python3", "args": ["${CLAUDE_PROJECT_DIR}/.claude/skills/handoff/hk.py", "hook-read"], "timeout": 10}]}],
  "SessionStart": [{"matcher": "compact", "hooks": [{"type": "command", "command": "python3", "args": ["${CLAUDE_PROJECT_DIR}/.claude/skills/handoff/hk.py", "hook-anchor"], "timeout": 10}]}]
}}
```

The first refuses Never-read paths, logs what is read, and tells the main session when its read budget is used up. The second prints the Anchor and the next step again after the conversation has been summarised.

## Self-tuning files

Written by `hk record` and read by `hk tune`; do not edit the metrics by hand.

- `docs/HK_METRICS.tsv`: one row per handoff (sizes, checks that failed first time, files read outside the map, sections fetched, cold-start score).
- `docs/HK_LEARNED.md` (this project) and `LEARNED.md` beside `hk.py` (travels with the skill): one table `| Id | Added | Note | Evidence | Last seen |`, at most 20 rows and 2 000 characters. The Id starts with the check it is about (`C7-headings`). Every row needs its evidence; rows are added, dropped and retired only on a proposal from `hk tune`.
