#!/usr/bin/env python3
"""Validate every project file against its schema, plus the cross-file rules.

Usage:
  validate.py [--project DIR]            check the whole project
  validate.py --file PATH [--schema NAME] check one file (schema guessed from its place)
  validate.py describe NAME|--all [--md] print the field tables of the schemas

Exit code 0 = no problems, 1 = problems found, 2 = bad usage.
Reads only. Writes nothing.
"""
import argparse
import fnmatch
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import rwlib  # noqa: E402

# (glob relative to the project, schema name, is_jsonl)
FILE_MAP = [
    ("project.json", "project", False),
    ("roles.json", "roles", False),
    ("corpus/register.jsonl", "register", True),
    ("corpus/snapshots.jsonl", "snapshot", True),
    ("corpus/search_log.jsonl", "search_log", True),
    ("parsed/*/pages/p*.json", "page", False),
    ("parsed/*/meta.json", "paper_meta", False),
    ("cards/cards.jsonl", "card", True),
    ("checks/checks.jsonl", "check", True),
    ("vocab/vocabulary.jsonl", "vocab_term", True),
    ("map/coverage.jsonl", "coverage_cell", True),
    ("map/ledgers/*.jsonl", "ledger", True),
    ("gaps/gaps.jsonl", "gap", True),
    ("conjectures/conjectures.jsonl", "conjecture", True),
    ("dossiers/D*.json", "dossier", False),
    ("mosaics/M*.json", "mosaic", False),
    ("drafts/*.sentences.jsonl", "sentence", True),
    ("corrections/corrections.jsonl", "correction", True),
    ("regression/cases.jsonl", "regression_case", True),
    ("exemplars/exemplars.jsonl", "exemplar", True),
    ("packs/*/pack.json", "pack_manifest", False),
    ("packs/*/items.jsonl", "pack_item", True),
    ("packs/*/gold.jsonl", "pack_gold", True),
    ("evals/results.jsonl", "eval_result", True),
    ("status/status.jsonl", "status", True),
    ("logs/model_calls.jsonl", "model_call", True),
    ("candidates/*/*.jsonl", "candidate", True),
    ("review/queue.jsonl", "review_item", True),
    ("review/decisions.jsonl", "review_decision", True),
]

PERSON_BLOCKLIST = {"claude", "model", "local", "machine", "ai", "assistant", "gpt", "llm", "script", "system"}


# ---------------------------------------------------------------- schemas
def load_schema(name):
    """Load a schema by name from the skill's templates/schemas folder."""
    p = rwlib.SCHEMA_DIR / (name + ".schema.json")
    if not p.exists():
        rwlib.die("Unknown schema: %s" % name)
    return rwlib.read_json(p)


def _type_ok(val, t):
    if t == "string":
        return isinstance(val, str)
    if t == "integer":
        return isinstance(val, int) and not isinstance(val, bool)
    if t == "number":
        return isinstance(val, (int, float)) and not isinstance(val, bool)
    if t == "boolean":
        return isinstance(val, bool)
    if t == "array":
        return isinstance(val, list)
    if t == "object":
        return isinstance(val, dict)
    if t == "null":
        return val is None
    return False


def check_value(val, sch, path="$"):
    """Validate `val` against the schema subset used by this skill. Returns a list of error strings."""
    errs = []
    t = sch.get("type")
    if t is not None:
        types = t if isinstance(t, list) else [t]
        if not any(_type_ok(val, x) for x in types):
            return ["%s: expected %s, got %s" % (path, "/".join(types), type(val).__name__)]
    if "enum" in sch and val not in sch["enum"]:
        errs.append("%s: %r is not one of %s" % (path, val, sch["enum"]))
    if isinstance(val, str):
        if "pattern" in sch and not re.search(sch["pattern"], val):
            errs.append("%s: %r does not match %s" % (path, val, sch["pattern"]))
        if "minLength" in sch and len(val) < sch["minLength"]:
            errs.append("%s: shorter than %d characters" % (path, sch["minLength"]))
        if "maxLength" in sch and len(val) > sch["maxLength"]:
            errs.append("%s: longer than %d characters" % (path, sch["maxLength"]))
    if isinstance(val, (int, float)) and not isinstance(val, bool):
        if "minimum" in sch and val < sch["minimum"]:
            errs.append("%s: %s is below the minimum %s" % (path, val, sch["minimum"]))
        if "maximum" in sch and val > sch["maximum"]:
            errs.append("%s: %s is above the maximum %s" % (path, val, sch["maximum"]))
    if isinstance(val, list):
        if "minItems" in sch and len(val) < sch["minItems"]:
            errs.append("%s: needs at least %d items" % (path, sch["minItems"]))
        if "maxItems" in sch and len(val) > sch["maxItems"]:
            errs.append("%s: at most %d items" % (path, sch["maxItems"]))
        if "items" in sch:
            for i, x in enumerate(val):
                errs += check_value(x, sch["items"], "%s[%d]" % (path, i))
    if isinstance(val, dict):
        props = sch.get("properties", {})
        for r in sch.get("required", []):
            if r not in val:
                errs.append("%s: missing required field '%s'" % (path, r))
        for k, v in val.items():
            if k in props:
                errs += check_value(v, props[k], "%s.%s" % (path, k))
            elif sch.get("additionalProperties", True) is False:
                errs.append("%s: unknown field '%s'" % (path, k))
    return errs


def expand_files(project):
    """Yield (path, schema_name, is_jsonl) for every existing project file covered by the map."""
    for pattern, name, is_jsonl in FILE_MAP:
        for p in sorted(Path(project).glob(pattern)):
            if p.is_file():
                yield p, name, is_jsonl


def validate_file(path, name, is_jsonl):
    """Validate one file. Returns a list of (location, message)."""
    sch = load_schema(name)
    problems = []
    try:
        if is_jsonl:
            with open(path, "r", encoding="utf-8") as fh:
                lines = fh.read().split("\n")
            for n, line in enumerate(lines, 1):
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError as exc:
                    if n >= len(lines) - 1:
                        problems.append(("%s:%d" % (path, n), "last line is cut off (interrupted write); it is ignored when reading"))
                    else:
                        problems.append(("%s:%d" % (path, n), "not valid JSON (%s)" % exc))
                    continue
                for m in check_value(rec, sch):
                    problems.append(("%s:%d" % (path, n), m))
        else:
            rec = rwlib.read_json(path)
            for m in check_value(rec, sch):
                problems.append((str(path), m))
    except UnicodeDecodeError:
        problems.append((str(path), "not valid UTF-8"))
    return problems


# ------------------------------------------------------- cross-file rules
def cross_rules(project):
    """Rules that span files. Returns a list of (location, message)."""
    P = Path(project)
    probs = []
    reg = rwlib.load_register(P)
    cards = rwlib.current_cards(P)
    checks = rwlib.current_checks(P)
    all_card_lines = rwlib.read_jsonl(P / "cards" / "cards.jsonl")
    seen = set()
    for c in all_card_lines:
        key = (c.get("card_id"), c.get("version"))
        if key in seen:
            probs.append(("cards", "duplicate card version %s v%s" % key))
        seen.add(key)

    for cid, c in cards.items():
        loc = "card %s" % cid
        if c.get("paper_id") not in reg:
            probs.append((loc, "paper_id %s is not in corpus/register.jsonl" % c.get("paper_id")))
        if c.get("stated") is False:
            if c.get("statement") != "not stated":
                probs.append((loc, "stated is false so statement must be exactly 'not stated'"))
        else:
            if c.get("statement", "").strip().lower() == "not stated":
                probs.append((loc, "statement says 'not stated' but stated is true"))
            if not c.get("locators"):
                probs.append((loc, "stated card without a locator (not citable)"))
        paper = reg.get(c.get("paper_id"))
        for l in c.get("locators", []):
            if paper and paper.get("pages") and l.get("page", 0) > paper["pages"]:
                probs.append((loc, "locator page %s is beyond the paper's %s pages" % (l["page"], paper["pages"])))
        if paper and paper.get("state") == "abstract_only" and not c.get("abstract_only"):
            probs.append((loc, "paper is abstract_only but the card is not marked abstract_only"))
        rs = c.get("review_state")
        if rs == "human_verified":
            by = (c.get("reviewed_by") or "").strip().lower()
            if not by or not c.get("reviewed_on"):
                probs.append((loc, "human_verified needs reviewed_by and reviewed_on"))
            if by in PERSON_BLOCKLIST:
                probs.append((loc, "human_verified cannot be set by '%s': only a person verifies" % by))
        elif c.get("reviewed_by"):
            probs.append((loc, "reviewed_by is set but review_state is %s" % rs))
        if c.get("type") == "result" and c.get("stated") and not c.get("value"):
            probs.append((loc, "result card without a value object"))
        if c.get("attribution") == "cited_work" and not c.get("attributed_to"):
            probs.append((loc, "cited_work card needs attributed_to"))

    for (cid, ver, name), rec in checks.items():
        if cid not in cards:
            probs.append(("check %s" % rec.get("check_id"), "refers to unknown card %s" % cid))
        if name == "supported" and rec.get("result") == "passed" and rec.get("label") != "entails":
            probs.append(("check %s" % rec.get("check_id"), "supported/passed needs label entails"))
    for rec in rwlib.read_jsonl(P / "checks" / "checks.jsonl"):
        if "verified" in json.dumps(rec).lower().replace("human_verified", ""):
            probs.append(("check %s" % rec.get("check_id"), "the word 'verified' appears in a machine check record"))

    gaps = {g["gap_id"]: g for g in rwlib.read_jsonl(P / "gaps" / "gaps.jsonl")}
    for g in gaps.values():
        loc = "gap %s" % g["gap_id"]
        for cid in g.get("card_ids", []):
            if cid not in cards:
                probs.append((loc, "card %s does not exist" % cid))
        if g.get("state") not in ("raw", "under_review") and not g.get("state_decided_by"):
            probs.append((loc, "state %s can only be set by a person: state_decided_by is empty" % g.get("state")))
        if (g.get("state_decided_by") or "").strip().lower() in PERSON_BLOCKLIST:
            probs.append((loc, "state_decided_by names a model, not a person"))
    conj = {c["conjecture_id"]: c for c in rwlib.read_jsonl(P / "conjectures" / "conjectures.jsonl")}
    for d in sorted(P.glob("dossiers/D*.json")):
        try:
            dos = rwlib.read_json(d)
        except Exception:
            continue
        loc = "dossier %s" % dos.get("dossier_id")
        subj = dos.get("subject", {})
        if subj.get("kind") == "gap" and subj.get("id") not in gaps:
            probs.append((loc, "subject gap %s does not exist" % subj.get("id")))
        if subj.get("kind") == "conjecture" and subj.get("id") not in conj:
            probs.append((loc, "subject conjecture %s does not exist" % subj.get("id")))
        for cid in dos.get("evidence_for", []) + dos.get("evidence_against", []):
            if cid not in cards:
                probs.append((loc, "card %s does not exist" % cid))
        dec = dos.get("decision", {})
        if dec.get("state") in ("killed", "parked", "adopted"):
            if not dec.get("by") or (dec.get("by") or "").lower() in PERSON_BLOCKLIST:
                probs.append((loc, "decision %s needs a named person" % dec.get("state")))
            ran = [p.get("ran_on") for p in dos.get("prior_art", [])]
            if not ran or max(ran) < dos.get("statement_edited_on", "9999"):
                probs.append((loc, "no prior-art search ran after the last edit of the statement: cannot be decided"))
        for a in dos.get("attacks", []):
            if a.get("state") == "closed":
                if not a.get("closed_by") or not a.get("closed_reason"):
                    probs.append((loc, "a closed attack needs closed_by and closed_reason"))
                elif a.get("closed_by") == a.get("raised_by"):
                    probs.append((loc, "the one who raised an attack cannot close it"))
                elif (a.get("closed_by") or "").lower() in PERSON_BLOCKLIST:
                    probs.append((loc, "an attack can be closed only by a person"))
    for f in sorted((P / "map" / "ledgers").glob("*.jsonl")) if (P / "map" / "ledgers").exists() else []:
        for r in rwlib.read_jsonl(f):
            for cid in r.get("card_ids", []):
                if cid not in cards:
                    probs.append(("ledger %s" % r.get("ledger_id"), "card %s does not exist" % cid))
            for ex in r.get("explanations_tried", []) or []:
                if ex.get("outcome") == "dissolves" and not ex.get("card_ids"):
                    probs.append(("ledger %s" % r.get("ledger_id"), "an explanation that dissolves a contradiction must cite located cards"))
    for f in sorted(P.glob("drafts/*.sentences.jsonl")):
        for s in rwlib.read_jsonl(f):
            if s.get("kind") == "supported" and not s.get("card_ids"):
                probs.append(("sentence %s" % s.get("sentence_id"), "supported sentence without cards"))
            for cid in s.get("card_ids", []):
                if cid not in cards:
                    probs.append(("sentence %s" % s.get("sentence_id"), "card %s does not exist" % cid))
            if s.get("origin") == "machine" and not s.get("machine_text"):
                probs.append(("sentence %s" % s.get("sentence_id"), "machine sentence must keep machine_text"))
    results = {r["run_id"]: r for r in rwlib.read_jsonl(P / "evals" / "results.jsonl") if "run_id" in r}
    for r in results.values():
        for m in r.get("measures", []):
            if m.get("denominator", 0) < 1:
                probs.append(("result %s" % r["run_id"], "measure %s has no denominator" % m.get("name")))
    roles = rwlib.read_json(P / "roles.json")
    if roles:
        for rname, ent in roles.get("roles", {}).items():
            if ent.get("assigned") == "local":
                if not ent.get("primary"):
                    probs.append(("roles.json", "role %s is local but has no primary model" % rname))
                ok = [results[x] for x in ent.get("evidence", []) if x in results and results[x].get("verdict") == "passed"
                      and results[x].get("pack_kind") in ("reference", "screen")]
                if not ok:
                    probs.append(("roles.json", "role %s is local without a passed bake-off result in evals/results.jsonl" % rname))
                if rname in ("extract", "judge_support") and not any(x.get("pack_kind") == "reference" for x in ok):
                    probs.append(("roles.json", "role %s needs a passed reference pack (built from the user's own pages), not only a synthetic screen" % rname))
    return probs


# ---------------------------------------------------------------- describe
def describe_schema(name, md=False):
    """Return a text or Markdown field table for one schema."""
    sch = load_schema(name)
    rows = []

    def walk(s, prefix, required_here):
        props = s.get("properties", {})
        for k, v in props.items():
            t = v.get("type")
            t = "/".join(t) if isinstance(t, list) else (t or "")
            allowed = ""
            if "enum" in v:
                allowed = ", ".join("null" if x is None else str(x) for x in v["enum"])
            elif "pattern" in v:
                allowed = "pattern " + v["pattern"]
            req = "yes" if k in required_here else "no"
            rows.append((prefix + k, t, req, allowed, v.get("description", "")))
            if v.get("type") == "object" and "properties" in v:
                walk(v, prefix + k + ".", v.get("required", []))
            if v.get("type") == "array" and isinstance(v.get("items"), dict) and "properties" in v["items"]:
                walk(v["items"], prefix + k + "[].", v["items"].get("required", []))

    walk(sch, "", sch.get("required", []))
    out = []
    if md:
        out.append("### %s\n\n%s\n" % (name, sch.get("description", "")))
        out.append("| Field | Type | Required | Allowed values | Meaning |\n|---|---|---|---|---|")
        for r in rows:
            out.append("| `%s` | %s | %s | %s | %s |" % (r[0], r[1], r[2], (r[3].replace("|", "/")), r[4].replace("|", "/")))
        out.append("")
    else:
        out.append("%s: %s" % (name, sch.get("description", "")))
        for r in rows:
            out.append("  %-34s %-14s req=%-3s %s%s" % (r[0], r[1], r[2], ("[" + r[3] + "] ") if r[3] else "", r[4]))
    return "\n".join(out)


# -------------------------------------------------------------------- main
def main(argv=None):
    rwlib.setup_console()
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "describe":
        ap = argparse.ArgumentParser(prog="validate.py describe", description="Print schema field tables.")
        ap.add_argument("name", nargs="?", help="schema name (omit with --all)")
        ap.add_argument("--all", action="store_true")
        ap.add_argument("--md", action="store_true", help="Markdown output")
        a = ap.parse_args(argv[1:])
        names = sorted(p.name.replace(".schema.json", "") for p in rwlib.SCHEMA_DIR.glob("*.schema.json")) if a.all else [a.name]
        if not names or names == [None]:
            ap.error("give a schema name or --all")
        print("\n".join(describe_schema(n, a.md) for n in names))
        return 0
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project", help="project folder (default: search upward from the current folder)")
    ap.add_argument("--file", help="validate just this file")
    ap.add_argument("--schema", help="schema name for --file")
    ap.add_argument("--quiet", action="store_true", help="print only problems and the summary line")
    a = ap.parse_args(argv)
    problems = []
    n_files = 0
    if a.file:
        path = Path(a.file)
        name, is_jsonl = a.schema, path.suffix == ".jsonl"
        if not name:
            for pattern, nm, jl in FILE_MAP:
                if fnmatch.fnmatch(path.name, Path(pattern).name):
                    name, is_jsonl = nm, jl
                    break
        if not name:
            rwlib.die("Cannot guess the schema for %s; use --schema" % path)
        problems += validate_file(path, name, is_jsonl)
        n_files = 1
    else:
        project = rwlib.find_project(explicit=a.project)
        for path, name, is_jsonl in expand_files(project):
            n_files += 1
            problems += validate_file(path, name, is_jsonl)
        problems += cross_rules(project)
    if n_files == 0:
        problems.append(("project", "0 files were validated (denominator 0): a check that examined nothing is not a pass"))
    for loc, msg in problems:
        print("PROBLEM %s: %s" % (loc, msg))
    print("validated %d file(s): %d problem(s)" % (n_files, len(problems)))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
