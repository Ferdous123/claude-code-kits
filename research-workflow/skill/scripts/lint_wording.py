#!/usr/bin/env python3
"""Check machine-written text for wording the workflow forbids (novelty claims, "verified", tick marks).

Usage:
  lint_wording.py [--project DIR] [PATH ...]      (default: all machine-written files of the project)
  lint_wording.py --text "some sentence"          check one string

Forbidden always: novel, no prior work, unexplored, overlooked, "has not been studied", open problem, untouched,
white space, "the first to", and a few more (see data/forbidden_wording.json).
"research gap" is allowed only when the file or record also carries a scope line.
"verified" is allowed only as "human-verified". Tick marks are never allowed.
Not checked: quoted source text (Markdown lines starting with ">", fenced blocks, lines starting with [researcher]),
JSON fields that hold quotes or the researcher's own words, and sentences whose origin is "author".
Exit code 1 if any hit.
"""
import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import rwlib  # noqa: E402

RULES = rwlib.read_json(rwlib.DATA_DIR / "forbidden_wording.json")
ALWAYS = [re.compile(p, re.I) for p in RULES["always_forbidden"]]
SCOPE_NEEDED = [re.compile(p, re.I) for p in RULES["needs_scope_line"]]
VERIFIED = re.compile(RULES["verified_without_human"])
TICKS = RULES["tick_marks"]
SCOPE_MD = re.compile(RULES["scope_markers_markdown"], re.I | re.M)
LINTED = set(RULES["json_fields_linted"])
SKIPPED = set(RULES["json_fields_skipped"])


def lint_string(s, has_scope):
    """Return a list of (rule, matched text) for one string."""
    hits = []
    for rx in ALWAYS:
        m = rx.search(s)
        if m:
            hits.append(("forbidden wording", m.group(0)))
    if not has_scope:
        for rx in SCOPE_NEEDED:
            m = rx.search(s)
            if m:
                hits.append(("needs a scope line", m.group(0)))
    m = VERIFIED.search(s)
    if m:
        hits.append(("'verified' is reserved for human-verified", m.group(0)))
    for t in TICKS:
        if t in s:
            hits.append(("tick mark", t))
    return hits


def walk_json(obj, has_scope, path=""):
    """Yield (path, string) for linted string fields."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in SKIPPED:
                continue
            if isinstance(v, str) and k in LINTED:
                yield path + "." + k, v
            elif isinstance(v, (dict, list)):
                for x in walk_json(v, has_scope, path + "." + k):
                    yield x
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            if isinstance(v, str) and path.rsplit(".", 1)[-1] in LINTED:
                yield "%s[%d]" % (path, i), v
            else:
                for x in walk_json(v, has_scope, "%s[%d]" % (path, i)):
                    yield x


def lint_file(path):
    """Lint one file. Returns a list of (location, rule, text)."""
    out = []
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    if path.suffix == ".md":
        has_scope = bool(SCOPE_MD.search(text))
        fenced = False
        for n, line in enumerate(text.split("\n"), 1):
            if line.strip().startswith("```"):
                fenced = not fenced
                continue
            if fenced or line.lstrip().startswith(">") or line.lstrip().startswith("[researcher]"):
                continue
            for rule, m in lint_string(line, has_scope):
                out.append(("%s:%d" % (path, n), rule, m))
    else:
        recs = []
        if path.suffix == ".jsonl":
            for line in text.split("\n"):
                if line.strip():
                    try:
                        recs.append(json.loads(line))
                    except ValueError:
                        pass
        else:
            try:
                recs = [json.loads(text)]
            except ValueError:
                recs = []
        for i, rec in enumerate(recs, 1):
            if isinstance(rec, dict) and rec.get("origin") == "author":
                continue
            has_scope = isinstance(rec, dict) and bool(rec.get("scope"))
            for p, s in walk_json(rec, has_scope):
                for rule, m in lint_string(s, has_scope):
                    out.append(("%s#%d%s" % (path, i, p), rule, m))
    return out


def default_targets(project):
    t = []
    for pat in ["gaps/gaps.jsonl", "conjectures/conjectures.jsonl", "dossiers/D*.json", "mosaics/M*.json", "map/ledgers/*.jsonl",
                "map/coverage.jsonl", "drafts/*.sentences.jsonl", "drafts/*.md", "reports/*.md", "HANDOFF.md", "parsed/*/nav.md"]:
        t += sorted(project.glob(pat))
    return t


def main():
    rwlib.setup_console()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project")
    ap.add_argument("--text")
    ap.add_argument("paths", nargs="*")
    a = ap.parse_args()
    if a.text:
        hits = lint_string(a.text, has_scope=False)
        for rule, m in hits:
            print("HIT %s: %r" % (rule, m))
        print("%d hit(s)" % len(hits))
        return 1 if hits else 0
    paths = [Path(p) for p in a.paths]
    if not paths:
        project = rwlib.find_project(explicit=a.project)
        paths = default_targets(project)
    n = 0
    for p in paths:
        for loc, rule, m in lint_file(p):
            print("HIT %s: %s: %r" % (loc, rule, m))
            n += 1
    print("linted %d file(s): %d hit(s)" % (len(paths), n))
    if not paths:
        print("linted 0 files (denominator 0): there is nothing to lint yet. This is not a pass.")
        return 1
    return 1 if n else 0


if __name__ == "__main__":
    sys.exit(main())
