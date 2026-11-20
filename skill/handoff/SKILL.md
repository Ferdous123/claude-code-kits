---
name: handoff
description: End-of-session handoff for any project. Run at the end of every working session or when the user says "/handoff", "handoff now", "make handoff" or "session bloated". Closes the session so a fresh session on any machine continues from git alone, with no memory of this one, reading only what its next step needs.
---
# Handoff

Goal: a fresh session, on another machine, with no transcript, runs one command, reads its short output and continues without re-investigating anything and without loading the project. A handoff that is not pushed has not happened. No narration while doing this; work through the steps and report at the end.

`hk` below means `python3 <this skill's folder>/hk.py`. Whatever a rule can check, `hk` checks. Do not repeat its checks by reading files, and do not argue with a `FAIL`: fix the cause. File formats are in `FORMAT.md` beside this file; open it only when writing one of those files. Optional `hk.json` keys (`procedure`, `checks`, `machines_check`, `external_refs`, `budget_ref`, `budget_pack`, `doc_paths`) and the State `Running:` line are described there.

## 0. Project rules first

Follow the project's own handoff rules (`START_PROMPT.md`, `CLAUDE.md`, a build plan) where they are stricter than this file. First use in a project, once, with the owner's agreement: create `hk.json`, add `.hk/` to `.gitignore`, put the context rules of section 8 into the fixed start prompt, and add the two hooks from `FORMAT.md` to `.claude/settings.json`.

## 1. Settle everything in flight

1. List running agents and background commands. For each: wait for it, or stop it and record exactly where it stopped. Never report an agent's result before it arrives.
2. For each finished task run `hk verify <brief>`. Decide its state from that and from the ledger, not from the agent's report:
   - **merged**: reviewed by a different agent, fixes applied, and a pass row for the exact commit made by `hk run` in a checkout no agent was writing to.
   - **parked on a branch**: anything short of that. Push the branch.
3. Remove what this session created: temporary databases, containers, scratch files. List what could not be removed so the owner can.

## 2. Verify the main branch

`hk run full --record -- <the project's full check>` on the integration branch. Read the `RESULT` line and the tail it prints, nothing more. Red or not run goes into the State line as `check:fail` or `check:not-run`; do not soften it. A skipped test is not a pass. Never write a ledger row by hand.

## 3. Write the delta

The only part that comes from this session's memory. `.hk/delta.md`, 40 lines at most: decisions and who made them (mark those taken on the owner's behalf); mistakes, each with an area; what was stopped and where; what the owner said that the next session must know; questions for the owner and which block work. Everything else comes from `hk facts`.

## 4. Records

1. Session log: close the session; times from the system clock.
2. Lessons: one line per mistake with its area. Keep the newest 30 in the lessons file; move older lines to the area files. A lesson seen twice becomes a check or a test, noted as a next step. Offer lessons that apply beyond this project for the assistant's persistent memory.
3. Machines file: update this machine's entry if anything changed.
4. Decision log: this session's decisions, with ids.
5. Tune: run `hk learned` and follow its notes, then `hk tune`. It prints at most 12 proposals, each with its evidence from earlier handoffs. Apply the ones that change the read map, the context map or a learned-notes file (add, drop, retire). Changes to `hk.py`, `SKILL.md` or `FORMAT.md` follow section 10, never a tune proposal applied in place. Do not add a learned note without the evidence `hk tune` printed.

## 5. Write the handoff from evidence

Move the old handoff to `docs/handoffs/<date>.md`. Then:

- If three or more tasks changed state this session, spawn one middle-model agent with a clean context. Give it only: the output of `hk facts`, the delta, the previous handoff, `FORMAT.md`. It writes the new handoff and the brief for row 1 of Next. Review its `git diff`, not the whole file; correct a fact only by pointing at evidence.
- Otherwise edit the handoff yourself.

Rules for the text, whoever writes it: the Anchor is copied unchanged (a fixed line changes only with an owner decision, cited on an `Anchor change:` line). Every number is copied from git, the ledger or a command. Status words are tied to artefacts; never "done". Detail lives in briefs and cited files, not in the handoff. No transcript excerpts, no secrets, no contents of environment files. Say what is not verified as plainly as what is.

## 6. Commit, push, check

1. Run the project's pre-push checks if it has them. Commit. Push the integration branch and every parked branch to the project's own remote only. Never to a public remote, never force.
2. `hk check`. It reads the pushed state, not your working copy. Fix every `FAIL`, push, and run it again until it prints `HANDOFF OK`. Do not raise a budget in `hk.json` to pass; if the project truly needs more, that is a question for the owner.

## 7. Cold-start test

`hk export <temp folder>`, then spawn one agent on the smallest model with only this: "You are in `<temp folder>`. Read `START_PROMPT.md`, follow its reading step and nothing else, then answer:" followed by the output of `hk quiz`. Compare its answers with `hk key`. If an answer is wrong, or it opened files the read map does not list, fix the handoff, repeat section 6, and run the test once more. Delete the temp folder.

Last: `hk record --coldstart <right>/<asked>`, commit the metrics row, push, and `hk check` once more. That row is what the next handoff's `hk tune` learns from.

## 8. Context rules for the working session

These live in the project's fixed start prompt, not in the handoff.

1. Start with `hk boot` and read nothing else.
2. When a Read-when trigger fires or a step names a file, `hk get <ref>` for that section. Not the whole file.
3. The orchestrator plans, briefs, reads verdicts and integrates. It does not explore: a question about the codebase goes to the code map if there is one, otherwise to an agent on the smallest model, answered in 15 lines at most with `file:line`.
4. Every task is a brief file. The spawn prompt is three lines: run `hk pack <brief>`, do what it says, reply in its report shape. State the model on every spawn: smallest for mechanical work, middle for writing code, strongest for review and design.
5. When an agent finishes: `hk verify <brief>`. Read its verdict lines before the agent's report.
6. Long commands go through `hk run` (`hk run full` uses the project's `checks.full`). Read the tail it prints; search the log file if more is needed. A refusal from `machines_check` means a required machine is down: fix that, never skip it.
7. Two failed acceptance runs move a task one model up, with the failing log attached. Never a third try on the same model. Note model and outcome in the session log.
8. Before each step and after each merge, re-read the Anchor. Work the Next table does not call for goes to the backlog.
9. Hand off at the next clean point when the owner says so, when the read-budget notice appears, after the conversation has been summarised once, or when a gate passes.

## 9. Reply

Only: the handoff path; the `HANDOFF OK` line; pushed branches with their commit ids; the cold-start result; what is red or unverified, if anything; the questions for the owner; and the fixed start prompt in one fenced code block for the owner to paste on the next machine.

## Installing

Copy this folder (`SKILL.md`, `hk.py`, `FORMAT.md`) to a project's `.claude/skills/handoff/` or to `~/.claude/skills/handoff/`. It names no project. It needs Python 3.8 or later and git.

## 10. The kit improves itself (owner standing approval, 2026-11-20)

Any session that finds a fault in this kit fixes it, so every iteration is better than the last: at boot (a ref that
does not resolve, a missing fact the next step needed), mid-session (a pack that left out what an agent needed, a
false FAIL or a check that passed when it should not have), or at handoff (anything `hk check` or the cold-start test
exposed). Fix it in the kit's source repository (the `kit_source` key in `hk.json`, else ask where it lives), test
first: a unittest that fails before the fix, the whole suite green after; keep it project-neutral and every new
`hk.json` key optional. Then reinstall the copy the session uses, commit in the kit repository as the configured
user, and record the fault and the fix in the handoff's Decisions line and the project's lessons file. Do not push
the kit repository unless the owner says so. Faults found but not fixed go to the handoff's Findings.

