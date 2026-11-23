#!/usr/bin/env python3
"""Register papers in the corpus register (file hashes, states) and freeze snapshots.

Usage:
  register.py add PATH [PATH ...] [--title T] [--doi D] [--arxiv A] [--source upload]
        Copy each PDF (or .txt/.md text file) into corpus/files/, hash it and
        add it to corpus/register.jsonl. A file already registered (same hash)
        is skipped, so running the command twice adds nothing. A folder is
        searched for *.pdf, *.txt and *.md.
  register.py entry --title T --state abstract_only|inaccessible [--doi D] [--reason R]
        Add a paper that has no file (never counted as evidence).
  register.py set --paper P0001 [--state S] [--screening included|excluded|undecided] [--reason R] [--title T]
        Change a paper's state or screening decision (appends a new line).
  register.py list [--state S]
        Count papers by state and screening decision.
  register.py check
        Re-hash every stored file and report any that changed or are missing.
  register.py snapshot [--note N]
        Freeze the corpus: every paper with screening 'included'. Analyses cite the snapshot id.

Writes only inside the project folder. Never deletes anything.
"""
import argparse
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import rwlib  # noqa: E402

MAX_BYTES = 300 * 1024 * 1024
STATES = ["full_text", "abstract_only", "inaccessible", "scanned_no_text", "failed", "excluded"]


def is_pdf(path):
    """True if the file starts with the PDF signature."""
    with open(path, "rb") as fh:
        return fh.read(1024).lstrip(b"\x00\r\n \t").startswith(b"%PDF-")


def collect(paths):
    """Expand folders to the paper files inside them."""
    out = []
    for p in paths:
        p = Path(p).expanduser()
        if p.is_dir():
            for ext in ("*.pdf", "*.PDF", "*.txt", "*.md"):
                out += sorted(p.rglob(ext))
        else:
            out.append(p)
    seen, uniq = set(), []
    for p in out:
        r = p.resolve()
        if r not in seen:
            seen.add(r)
            uniq.append(p)
    return uniq


def copy_atomic(src, dest):
    """Copy a file so that an interruption never leaves a half-written file under the final name."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(dest.parent), prefix=".tmp_", suffix=".part")
    os.close(fd)
    try:
        shutil.copyfile(src, tmp)
        os.replace(tmp, dest)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def cmd_add(project, a):
    """Register files."""
    reg = rwlib.load_register(project)
    by_hash = {r["sha256"]: r for r in reg.values() if r.get("sha256")}
    by_doi = {(r.get("identifiers") or {}).get("doi"): r for r in reg.values() if (r.get("identifiers") or {}).get("doi")}
    added = skipped = refused = 0
    for f in collect(a.paths):
        if not f.is_file():
            print("REFUSED %s: not a file" % f)
            refused += 1
            continue
        size = f.stat().st_size
        ext = f.suffix.lower()
        if size > MAX_BYTES:
            print("REFUSED %s: larger than %d MB" % (f.name, MAX_BYTES // 1048576))
            refused += 1
            continue
        if size == 0:
            print("REFUSED %s: empty file" % f.name)
            refused += 1
            continue
        if ext == ".pdf" and not is_pdf(f):
            print("REFUSED %s: does not start with the PDF signature (not a PDF)" % f.name)
            refused += 1
            continue
        if ext not in (".pdf", ".txt", ".md"):
            print("REFUSED %s: only .pdf, .txt and .md files are accepted" % f.name)
            refused += 1
            continue
        h = rwlib.sha256_file(f)
        if h in by_hash:
            print("skip %s: same bytes as %s" % (f.name, by_hash[h]["paper_id"]))
            skipped += 1
            continue
        if a.doi and a.doi in by_doi and not a.allow_version:
            print("skip %s: DOI %s is already registered as %s (use --allow-version for a second version)" % (f.name, a.doi, by_doi[a.doi]["paper_id"]))
            skipped += 1
            continue
        reg = rwlib.load_register(project)
        pid = rwlib.next_id("P", reg.keys())
        stored = Path("corpus") / "files" / (pid + (".pdf" if ext == ".pdf" else ".txt"))
        copy_atomic(f, project / stored)
        rec = {
            "paper_id": pid, "sha256": h, "original_name": f.name, "stored_path": stored.as_posix(), "bytes": size,
            "added": rwlib.now_iso(), "source": a.source, "identifiers": {"doi": a.doi, "arxiv": a.arxiv},
            "title": a.title or f.stem, "state": "full_text", "state_reason": "file registered; text layer not yet checked",
            "pages": None, "licence": "unknown", "screening": "undecided", "screening_reason": "",
        }
        rwlib.append_jsonl(project / "corpus" / "register.jsonl", rec)
        by_hash[h] = rec
        if a.doi:
            by_doi[a.doi] = rec
        print("added %s  %s  sha256 %s..." % (pid, f.name, h[:12]))
        added += 1
    print("added %d, skipped %d, refused %d" % (added, skipped, refused))
    return 0 if refused == 0 else 1


def cmd_entry(project, a):
    """Register a paper without a file."""
    reg = rwlib.load_register(project)
    pid = rwlib.next_id("P", reg.keys())
    rec = {
        "paper_id": pid, "sha256": None, "original_name": "(no file)", "stored_path": None, "bytes": None,
        "added": rwlib.now_iso(), "source": "other", "identifiers": {"doi": a.doi, "arxiv": a.arxiv},
        "title": a.title, "state": a.state, "state_reason": a.reason or "no file", "pages": None, "licence": "unknown",
        "screening": "undecided", "screening_reason": "",
    }
    rwlib.append_jsonl(project / "corpus" / "register.jsonl", rec)
    print("added %s (%s, no file)" % (pid, a.state))
    return 0


def cmd_set(project, a):
    """Append an updated register line for one paper."""
    reg = rwlib.load_register(project)
    if a.paper not in reg:
        rwlib.die("Unknown paper %s" % a.paper)
    rec = dict(reg[a.paper])
    if a.state:
        rec["state"] = a.state
        rec["state_reason"] = a.reason or rec.get("state_reason", "")
    if a.screening:
        rec["screening"] = a.screening
        rec["screening_reason"] = a.reason or ""
    if a.title:
        rec["title"] = a.title
    rwlib.append_jsonl(project / "corpus" / "register.jsonl", rec)
    print("updated %s: state=%s screening=%s" % (a.paper, rec["state"], rec["screening"]))
    return 0


def cmd_list(project, a):
    """Counts by state and screening."""
    reg = rwlib.load_register(project)
    by_state, by_scr = {}, {}
    for r in reg.values():
        if a.state and r["state"] != a.state:
            continue
        by_state[r["state"]] = by_state.get(r["state"], 0) + 1
        by_scr[r["screening"]] = by_scr.get(r["screening"], 0) + 1
    print("papers: %d" % sum(by_state.values()))
    print("by state: " + (", ".join("%s=%d" % kv for kv in sorted(by_state.items())) or "none"))
    print("by screening: " + (", ".join("%s=%d" % kv for kv in sorted(by_scr.items())) or "none"))
    for r in sorted(reg.values(), key=lambda x: x["paper_id"]):
        if a.state and r["state"] != a.state:
            continue
        print("  %s  %-15s %-9s %s" % (r["paper_id"], r["state"], r["screening"], r["title"][:70]))
    return 0


def cmd_check(project, a):
    """Re-hash stored files."""
    bad = 0
    reg = rwlib.load_register(project)
    for r in sorted(reg.values(), key=lambda x: x["paper_id"]):
        if not r.get("stored_path"):
            continue
        p = project / r["stored_path"]
        if not p.exists():
            print("MISSING %s %s" % (r["paper_id"], r["stored_path"]))
            bad += 1
        elif rwlib.sha256_file(p) != r["sha256"]:
            print("CHANGED %s %s (hash differs from the register)" % (r["paper_id"], r["stored_path"]))
            bad += 1
    print("checked %d stored file(s): %d problem(s)" % (sum(1 for r in reg.values() if r.get("stored_path")), bad))
    return 1 if bad else 0


def cmd_snapshot(project, a):
    """Freeze the included papers as a snapshot."""
    reg = rwlib.load_register(project)
    ids = sorted(r["paper_id"] for r in reg.values() if r["screening"] == "included" and r["state"] not in ("excluded", "failed"))
    if not ids:
        rwlib.die("No paper has screening 'included'. Set screening first (register.py set).")
    material = "\n".join("%s:%s" % (i, reg[i].get("sha256") or "nofile") for i in ids)
    snaps = rwlib.read_jsonl(project / "corpus" / "snapshots.jsonl")
    h = rwlib.sha256_text(material)
    if snaps and snaps[-1]["corpus_hash"] == h:
        print("snapshot %s already describes exactly this corpus; nothing added" % snaps[-1]["snapshot_id"])
        return 0
    sid = rwlib.next_id("S", [s["snapshot_id"] for s in snaps])
    rwlib.append_jsonl(project / "corpus" / "snapshots.jsonl",
                       {"snapshot_id": sid, "date": rwlib.today(), "paper_ids": ids, "corpus_hash": h, "note": a.note or ""})
    pj = rwlib.read_json(project / "project.json")
    pj["corpus_snapshot"] = sid
    rwlib.write_json(project / "project.json", pj)
    states = {}
    for i in ids:
        states[reg[i]["state"]] = states.get(reg[i]["state"], 0) + 1
    print("snapshot %s frozen: %d papers (%s)" % (sid, len(ids), ", ".join("%s=%d" % kv for kv in sorted(states.items()))))
    return 0


def main():
    rwlib.setup_console()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project", help="project folder (default: search upward)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("add", help="register files")
    p.add_argument("paths", nargs="+")
    p.add_argument("--title")
    p.add_argument("--doi")
    p.add_argument("--arxiv")
    p.add_argument("--source", default="upload", choices=["upload", "doi", "arxiv", "bibtex", "other"])
    p.add_argument("--allow-version", action="store_true")
    p = sub.add_parser("entry", help="register a paper without a file")
    p.add_argument("--title", required=True)
    p.add_argument("--state", required=True, choices=["abstract_only", "inaccessible"])
    p.add_argument("--doi")
    p.add_argument("--arxiv")
    p.add_argument("--reason")
    p = sub.add_parser("set", help="change state or screening")
    p.add_argument("--paper", required=True)
    p.add_argument("--state", choices=STATES)
    p.add_argument("--screening", choices=["undecided", "included", "excluded"])
    p.add_argument("--reason")
    p.add_argument("--title")
    p = sub.add_parser("list", help="counts and list")
    p.add_argument("--state", choices=STATES)
    sub.add_parser("check", help="re-hash stored files")
    p = sub.add_parser("snapshot", help="freeze the corpus")
    p.add_argument("--note")
    a = ap.parse_args()
    project = rwlib.find_project(explicit=a.project)
    return {"add": cmd_add, "entry": cmd_entry, "set": cmd_set, "list": cmd_list, "check": cmd_check, "snapshot": cmd_snapshot}[a.cmd](project, a)


if __name__ == "__main__":
    sys.exit(main())
