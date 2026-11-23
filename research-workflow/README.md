# research-workflow

A Claude Code skill that runs an evidence-first research workflow from plain files on an ordinary PC.

You bring papers (PDFs or text). Every fact becomes an evidence card with a locator: the page and the exact quoted text, which a script checks against the page. The paper set is mapped into candidate gaps with a scope line, your own idea is attacked with prior-art searches instead of being generated for you, a method can be assembled from cited steps of different papers as a hypothesis, and text is written so that every sentence links to the cards behind it.

- Machine checks are named for what they prove (`located`, `zone_valid`, `supported`, `consistent`, `cross_read`) and are never called "verified"; only a person makes a card `human_verified`.
- "Not stated" is a valid value and is never filled in by a model.
- A check that examined nothing is "not run", never clean.
- State lives in files in the project folder, so a session can stop at any time and the next one starts from one short handoff file.
- On first use it detects the machine (CPU, RAM, GPU, disk, local model runner), writes `MACHINE.md` and plans from the facts. A local model takes a role (extract, embed, judge support) only after it passes a small test on that machine, and its output is always checked; Claude is kept for reference answers, disagreements and final verification.

## Requirements

- Claude Code, with the skill folder installed as described below.
- Python 3.8 or newer. All scripts use the standard library only; each has `--help`.
- Reading PDFs needs PyMuPDF, which `setup_env.py install` puts in a private virtual environment inside the skill's cache folder (needs internet access and pip; no administrator rights, no compiler). Text files need nothing extra.
- Local models are optional. The runner the skill is written for is Ollama (address from `OLLAMA_HOST`, default `127.0.0.1:11434`). Without a runner, every role stays on Claude.

## Install

1. Copy the `skill` folder of this kit to Claude Code's skills folder, under the name `research-workflow`:
   - Windows: `C:\Users\<you>\.claude\skills\research-workflow`
   - macOS and Linux: `~/.claude/skills/research-workflow`
2. Open Claude Code in a new empty folder for your research project and say: **run the first-run step** (or `rw machine`). Then bring your papers and say `rw start`.

Update: replace the folder with the new version, then say "update my project CLAUDE.md" (it shows the difference and asks first). Remove: delete the folder. Your project folders are not touched.

## Usage

Say the trigger word for a phase, or just describe the task. Phases are gated; a later change marks earlier phases stale. Triggers and what each does:

| Say | Phase or action |
|---|---|
| `rw machine` | first-run step: create the project, detect the machine, write `MACHINE.md` and a plan |
| `rw frame` | write the protocol: question, scope, inclusion rules, confidentiality decision (`claude_allowed` or `local_only`) |
| `rw collect` | register papers (hashes, states), log outside searches, freeze a snapshot |
| `rw roles` | test local models on this machine and assign roles |
| `rw read` | parse papers, make evidence cards, run the `located` and `zone_valid` checks |
| `rw ask` | answer a question with card ids for every claim |
| `rw map` | coverage map, contradictions and candidate gaps, each with a scope line |
| `rw challenge` | attack a candidate gap or your own idea with prior-art searches |
| `rw compose` | assemble a method from cited steps of different papers, as a hypothesis |
| `rw write` | draft text where every sentence links to cards |
| `rw start`, `rw handoff`, `rw status`, `rw gate`, `rw check` | session start, session end, status report, check the current gate, run validate, card and wording checks |

All state is in files in your project folder. `SKILL.md` has the full phase table, the gates and the hard rules.

## What is in the folder

`SKILL.md` (the workflow: session protocol, ten gated phases, hard rules, local-model policy), `scripts/` (Python helpers), `templates/` (project files, JSON schemas with one filled example each, extraction and judging prompts), `candidates.json` (a dated, editable list of small local models worth testing; the on-machine test, not the list, decides). The skill folder ships no ready-made test packs; packs for local-model tests are built per project from pages you approve.

## Status

What was run, on text files only and with no model involved: project set-up, register, text extraction (including the warnings for a text layer with control characters and for a title not found on the first pages), cards with the `located` check, validation, metrics, citability, wording lint, resumable batches, status words, the update of the project `CLAUDE.md` as a proposed difference, and machine detection on one machine. A check that examines nothing (no files, no cards) reports "not a pass" and exits non-zero.

What was not run:

- Reading a real PDF: the PDF branch, including the check for a missing end marker (`eof_marker`) and PyMuPDF text extraction, and the `setup_env.py install` step.
- Anything that calls a local model: `local_run`, `bakeoff`, embeddings, the agreement check on real model output.
- The Windows and macOS branches of machine detection.
- A full project from first paper to written draft.

If something fails, copy the error text to Claude; the skill records it in the project's `LESSONS.md` and works around it.

## Known limitations and open items

- No OCR or vision route. Scanned pages, tables drawn as pictures and formulas read from a text layer stay unverified; the skill flags them, and you must compare a formula with the page image yourself before it counts as evidence. Numbers in picture tables cannot be checked.
- Cards are per page. A sentence that runs across a page break is not captured.
- Local models are asked to copy quotes; the located check then catches a wrong quote, but quoting by sentence id (so a script copies the text) is not implemented.
- The tag vocabulary is not closed at extraction (`metric` and `dataset` are free text), and cards have no field for the direction of a claim, which limits automatic contradiction mining.
- No ready-made test packs ship, so every local-model role needs a reference pack built with you before it can be assigned.
- The list in `candidates.json` is dated 2026-11-18; check model availability and sizes before relying on it.
- A machine check proves where text is, not that a claim is true; the skill does not judge novelty, write papers for you or submit anything.

## License

MIT. See the repository `LICENSE`.
