#!/usr/bin/env python3
"""Keep work status honest: five words, each tied to an artefact that exists.

Usage:
  status_check.py add --item "what" --status WORD --artefact PATH_OR_RUN_ID [--note T] [--inspected-by NAME]
  status_check.py check [--project DIR]     verify every record in status/status.jsonl
  status_check.py show                      table of records
  status_check.py scan FILE [FILE ...]      find claims like "done" or "finished" that name no artefact

The five words and what each needs
  written             the artefact file exists
  passes-with-stub    the artefact exists and the note says it ran against a stub (no real model, no real paper)
  ran-on-real-model   logs/model_calls.jsonl holds a call by a real model (not a stub) and the artefact exists
  measured            the artefact is a run id in evals/results.jsonl whose measures all have a denominator
  operator-inspected  a named person looked: --inspected-by (a person's name, not a model) and the artefact exists
"Done", "finished", "shipped", "complete" and "production-ready" are never used as status without an artefact.
Exit code 1 if any record or scanned line fails.
"""
import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import rwlib  # noqa: E402
from validate import PERSON_BLOCKLIST  # noqa: E402

BAD_WORDS = re.compile(r"\b(done|finished|shipped|production[- ]ready|fully (?:working|tested)|complete[d]?|all set|works perfectly)\b", re.I)
PATHISH = re.compile(r"(`[^`]+\.[A-Za-z0-9]{1,6}`|\]\([^)]+\)|\b[\w./\\-]+\.(?:md|json|jsonl|py|txt|csv)\b|\bR\d{8}-\d{6}\b)")


def check_record(project, rec):
    """Return a list of problems for one status record."""
    probs = []
    word = rec.get("status")
    if word not in rwlib.STATUS_WORDS:
        return ["status %r is not one of: %s" % (word, ", ".join(rwlib.STATUS_WORDS))]
    art = rec.get("artefact", "")
    art_path = (Path(project) / art) if art and not re.match(r"^R\d{8}-\d{6}", art) else None
    exists = bool(art_path and art_path.exists() and rwlib.inside(project, art_path))
    if word in ("written", "passes-with-stub", "ran-on-real-model", "operator-inspected") and not exists:
        probs.append("artefact %r does not exist inside the project" % art)
    if word == "passes-with-stub" and "stub" not in (rec.get("note") or "").lower():
        probs.append("passes-with-stub needs a note saying what the stub was")
    if word == "ran-on-real-model":
        calls = rwlib.read_jsonl(Path(project) / "logs" / "model_calls.jsonl")
        real = [c for c in calls if str(c.get("model", "")).lower() not in ("stub", "fake", "mock", "")]
        if not real:
            probs.append("no real model call is logged in logs/model_calls.jsonl")
    if word == "measured":
        results = {r["run_id"]: r for r in rwlib.read_jsonl(Path(project) / "evals" / "results.jsonl") if "run_id" in r}
        r = results.get(art)
        if not r:
            probs.append("measured needs the run id of a result in evals/results.jsonl; %r is not there" % art)
        elif any(m.get("denominator", 0) < 1 for m in r.get("measures", [])):
            probs.append("a measure in %s has no denominator" % art)
    if word == "operator-inspected":
        who = (rec.get("inspected_by") or "").strip()
        if len(who) < 2 or who.lower() in PERSON_BLOCKLIST:
            probs.append("operator-inspected needs the name of the person who looked (--inspected-by)")
    return probs


def cmd_add(project, a):
    rec = {"status_id": rwlib.next_id("ST", [r["status_id"] for r in rwlib.read_jsonl(project / "status" / "status.jsonl")]),
           "item": a.item, "status": a.status, "artefact": a.artefact, "date": rwlib.today(),
           "inspected_by": a.inspected_by, "note": a.note or ""}
    probs = check_record(project, rec)
    if probs:
        print("REFUSED: " + "; ".join(probs))
        print("Do not report this item with that word. Use a weaker word that its artefact supports.")
        return 1
    rwlib.append_jsonl(project / "status" / "status.jsonl", rec)
    print("%s: %s (%s)" % (rec["status_id"], a.status, a.artefact))
    return 0


def cmd_check(project, a):
    bad = 0
    recs = rwlib.read_jsonl(project / "status" / "status.jsonl")
    for r in recs:
        probs = check_record(project, r)
        for p in probs:
            print("PROBLEM %s (%s): %s" % (r["status_id"], r["item"], p))
            bad += 1
    print("checked %d status record(s): %d problem(s)" % (len(recs), bad))
    return 1 if bad else 0


def cmd_show(project, a):
    for r in rwlib.read_jsonl(project / "status" / "status.jsonl"):
        print("%s  %-18s %-40s %s" % (r["status_id"], r["status"], r["item"][:40], r["artefact"]))
    return 0


def cmd_scan(files):
    bad = 0
    for f in files:
        for n, line in enumerate(Path(f).read_text(encoding="utf-8", errors="replace").split("\n"), 1):
            m = BAD_WORDS.search(line)
            if m and not PATHISH.search(line):
                print("%s:%d: '%s' with no artefact named: %s" % (f, n, m.group(0), line.strip()[:90]))
                bad += 1
    print("scanned %d file(s): %d unsupported claim(s)" % (len(files), bad))
    return 1 if bad else 0


def main():
    rwlib.setup_console()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("add")
    p.add_argument("--item", required=True)
    p.add_argument("--status", required=True)
    p.add_argument("--artefact", required=True)
    p.add_argument("--note")
    p.add_argument("--inspected-by", dest="inspected_by")
    sub.add_parser("check")
    sub.add_parser("show")
    p = sub.add_parser("scan")
    p.add_argument("files", nargs="+")
    a = ap.parse_args()
    if a.cmd == "scan":
        return cmd_scan(a.files)
    project = rwlib.find_project(explicit=a.project)
    return {"add": cmd_add, "check": cmd_check, "show": cmd_show}[a.cmd](project, a)


if __name__ == "__main__":
    sys.exit(main())
