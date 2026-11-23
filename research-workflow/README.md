# research-workflow

A Claude Code skill that runs an evidence-first research workflow from plain files on an ordinary PC.

You bring papers (PDFs or text). Every fact becomes an evidence card with a locator: the page and the exact quoted text, which a script checks against the page. The paper set is mapped into candidate gaps with a scope line, your own idea is attacked with prior-art searches instead of being generated for you, a method can be assembled from cited steps of different papers as a hypothesis, and text is written so that every sentence links to the cards behind it.

- Machine checks are named for what they prove (`located`, `zone_valid`, `supported`, `consistent`, `cross_read`) and are never called "verified"; only a person makes a card `human_verified`.
- "Not stated" is a valid value and is never filled in by a model.
- A check that examined nothing is "not run", never clean.
- State lives in files in the project folder, so a session can stop at any time and the next one starts from one short handoff file.
- On first use it detects the machine (CPU, RAM, GPU, disk, local model runner), writes `MACHINE.md` and plans from the facts. A local model takes a role (extract, embed, judge support) only after it passes a small test on that machine, and its output is always checked; Claude is kept for reference answers, disagreements and final verification.

## Install

1. Copy the `skill` folder of this kit to Claude Code's skills folder, under the name `research-workflow`:
   - Windows: `C:\Users\<you>\.claude\skills\research-workflow`
   - macOS and Linux: `~/.claude/skills/research-workflow`
2. Needs Python 3.8 or newer (standard library only; PDF reading installs one small dependency into a private folder on request).
3. Open Claude Code in a new empty folder for your research project and say: **run the first-run step** (or `rw machine`). Then bring your papers and say `rw start`.

Update: replace the folder with the new version, then say "update my project CLAUDE.md" (it shows the difference and asks first). Remove: delete the folder. Your project folders are not touched.

## What is in the folder

`SKILL.md` (the workflow: session protocol, ten gated phases, hard rules, local-model policy), `scripts/` (Python helpers, each with `--help`), `templates/` (project files, JSON schemas with one filled example each, extraction and judging prompts), `candidates.json` (a dated, editable list of small local models worth testing; the on-machine test, not the list, decides).

## Status

- Exercised on a text fixture only: project set-up, register, text extraction, cards with the `located` check, validation, metrics, citability, wording lint, resumable batches, status words, machine detection.
- Not tested: reading real PDFs through the installer, anything that calls a local model (`local_run`, `bakeoff`, embeddings), and the Windows and macOS branches of machine detection. If something fails, copy the error text to Claude; the skill records it in the project's `LESSONS.md` and works around it.
- Known limits: no OCR or vision route (scanned pages and formulas stay unverified and are flagged); cards are per page, so a sentence that runs across a page break is not captured.

## License

MIT. See the repository `LICENSE`.
