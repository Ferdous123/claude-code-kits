#!/usr/bin/env python3
"""Create a research project folder, and keep its CLAUDE.md block current.

Usage:
  rw_init.py create --dir PATH --title "Title" [--mode related_work]
        Make the folder layout, project.json, roles.json, HANDOFF.md,
        DECISIONS.md, LESSONS.md, SESSION_LOG.md, protocol.md, CLAUDE.md and
        copy the starter test packs. Files that already exist are never
        overwritten (a CLAUDE.md that exists goes through the diff-and-ask
        step below).
  rw_init.py claude-md [--project DIR] [--accept]
        Bring the managed block in CLAUDE.md up to date. If CLAUDE.md exists
        and would change, the difference is printed and NOTHING is written
        until you rerun with --accept (do that only after the user agreed).
        Text outside the managed block is never touched.

Exit codes: 0 done / up to date, 3 = difference shown, waiting for the user's yes, 2 = error.
Writes only inside the project folder.
"""
import argparse
import difflib
import re
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import rwlib  # noqa: E402

BEGIN = "<!-- research-workflow:begin -->"
END = "<!-- research-workflow:end -->"
TEMPLATES = rwlib.SKILL_DIR / "templates" / "project"
FOLDERS = ["corpus/files", "parsed", "cards", "checks", "vocab", "map/ledgers", "gaps", "conjectures", "dossiers",
           "mosaics", "drafts", "corrections", "regression", "exemplars", "packs", "evals", "status", "logs",
           "candidates", "review", "machine/history", "index"]
SKILL_VERSION = "1.0.0"


def quote_if_needed(p):
    """Quote a path only if it contains a space."""
    p = str(p)
    return '"%s"' % p if " " in p else p


def render(text, project, extra=None):
    """Fill {{placeholders}} in a template."""
    info = rwlib.read_json(Path(project) / "project.json", {}) or {}
    vals = {
        "skill_dir": str(rwlib.SKILL_DIR), "scripts": str(rwlib.SCRIPTS_DIR), "python": quote_if_needed(sys.executable),
        "date": rwlib.today(), "title": info.get("title", Path(project).name),
    }
    vals.update(extra or {})
    for k, v in vals.items():
        text = text.replace("{{%s}}" % k, v)
    return text


def block_text(project):
    """The rendered managed block, ending in a newline."""
    return render((TEMPLATES / "CLAUDE.md.block").read_text(encoding="utf-8"), project).rstrip("\n") + "\n"


def claude_md(project, accept):
    """Create or update CLAUDE.md. Returns an exit code."""
    target = Path(project) / "CLAUDE.md"
    block = block_text(project)
    if not target.exists():
        rwlib.write_text_atomic(target, block)
        print("created CLAUDE.md")
        return 0
    old = target.read_text(encoding="utf-8")
    pat = re.compile(re.escape(BEGIN) + r".*?" + re.escape(END) + r"\n?", re.S)
    if pat.search(old):
        new = pat.sub(lambda m: block, old, count=1)
    else:
        new = old.rstrip("\n") + "\n\n" + block
    if new == old:
        print("CLAUDE.md is up to date")
        return 0
    diff = "".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True), "CLAUDE.md (yours now)", "CLAUDE.md (proposed)"))
    if accept:
        backup = Path(project) / ("CLAUDE.md.bak-" + time.strftime("%Y%m%d-%H%M%S"))
        shutil.copyfile(target, backup)
        rwlib.write_text_atomic(target, new)
        print("CLAUDE.md updated; the previous version is kept as %s" % backup.name)
        return 0
    rwlib.write_text_atomic(Path(project) / "CLAUDE.md.proposed", new)
    print(diff)
    print("\nNOTHING WAS CHANGED. The difference above would be applied to CLAUDE.md.")
    print("Show it to the user and ask. If they agree, run again with --accept. The full proposed file is in CLAUDE.md.proposed.")
    return 3


def create(a):
    """Make a new project folder."""
    root = Path(a.dir).expanduser().resolve()
    if (root / "project.json").exists():
        rwlib.die("%s already holds a project (project.json exists). Nothing changed." % root)
    root.mkdir(parents=True, exist_ok=True)
    for f in FOLDERS:
        (root / f).mkdir(parents=True, exist_ok=True)
    pid = re.sub(r"[^a-z0-9_-]+", "-", (a.id or a.title).lower()).strip("-")[:40] or "project"
    if len(pid) < 2:
        pid = pid + "-1"
    proj = {
        "project_id": pid, "title": a.title, "created": rwlib.today(), "review_mode": a.mode, "question": "", "scope": "",
        "inclusion_rules": [], "confidentiality": {"decision": "undecided", "decided_on": None, "words": ""},
        "phase": "setup", "corpus_snapshot": None, "vocabulary_version": 0, "skill_version": SKILL_VERSION,
    }
    rwlib.write_json(root / "project.json", proj)
    roles = {"updated": rwlib.now_iso(), "roles": {}}
    for r in ["extract", "embed", "judge_support", "rerank", "navsum", "adjudicate", "critique", "write", "reference"]:
        roles["roles"][r] = {"assigned": "claude" if r in ("adjudicate", "critique", "write", "reference") else "claude",
                             "primary": None, "secondary": None, "evidence": [], "stale": False,
                             "note": "default: Claude does this until a bake-off passes on this machine"}
    rwlib.write_json(root / "roles.json", roles)
    made = []
    for name in ["HANDOFF.md", "DECISIONS.md", "LESSONS.md", "SESSION_LOG.md", "protocol.md"]:
        dest = root / name
        if dest.exists():
            print("kept your existing %s (not overwritten)" % name)
            continue
        rwlib.write_text_atomic(dest, render((TEMPLATES / name).read_text(encoding="utf-8"), root))
        made.append(name)
    code = claude_md(root, False)
    # starter packs
    for pack in sorted((rwlib.SKILL_DIR / "packs").iterdir()) if (rwlib.SKILL_DIR / "packs").exists() else []:
        if pack.is_dir() and (pack / "pack.json").exists():
            dest = root / "packs" / pack.name
            if not dest.exists():
                shutil.copytree(pack, dest)
                made.append("packs/" + pack.name)
    print("created project '%s' in %s" % (a.title, root))
    print("made: " + ", ".join(made))
    print("next: run detect_machine.py --project %s" % root)
    return code


def main():
    rwlib.setup_console()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create", help="make a new project folder")
    c.add_argument("--dir", required=True, help="folder to create (inside it everything is written)")
    c.add_argument("--title", required=True)
    c.add_argument("--id", help="short id (default: made from the title)")
    c.add_argument("--mode", default="exploratory",
                   choices=["exploratory", "related_work", "systematic", "scoping", "thesis_chapter", "contradiction_audit"])
    m = sub.add_parser("claude-md", help="create or update the managed block in CLAUDE.md")
    m.add_argument("--project")
    m.add_argument("--accept", action="store_true", help="apply the shown difference (only after the user agreed)")
    a = ap.parse_args()
    if a.cmd == "create":
        return create(a)
    return claude_md(rwlib.find_project(explicit=a.project), a.accept)


if __name__ == "__main__":
    sys.exit(main())
