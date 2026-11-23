#!/usr/bin/env python3
"""Run a long job as small items with a checkpoint after every item, so it can stop at any instant and resume.

Usage:
  batch.py init NAME --items FILE         one item per line (text file) or JSON Lines with an "id" field
  batch.py run NAME --cmd "{python} {scripts}/local_run.py extract --paper {item} --pass A" [--timeout 1800] [--stop-after N]
  batch.py status NAME
  batch.py next NAME [--n 5]              print the next pending items
  batch.py done NAME ITEM [--note T]      mark an item done by hand
  batch.py fail NAME ITEM --note T        mark an item failed by hand
  batch.py retry NAME                     put failed items back to pending
  batch.py list                           list the jobs in this project

How it stays safe
  * State is in logs/jobs/NAME/state.json, rewritten atomically after every item (a power cut leaves the old or
    the new file, never half of one).
  * An item found "running" at start-up was interrupted: it goes back to pending and runs again. A finished
    item is never run twice, so there is no duplication as long as the command itself is repeatable.
  * `run` accepts only commands whose script is one of this skill's own scripts, so a job cannot write outside
    the project. Placeholders: {item} {python} {scripts} {project}.
Exit code 0 = all items done, 1 = some failed or pending (stopped early or failures).
"""
import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import rwlib  # noqa: E402


def job_dir(project, name):
    if not name.replace("-", "").replace("_", "").isalnum():
        rwlib.die("Job names may contain letters, digits, - and _ only.")
    return project / "logs" / "jobs" / name


def load(project, name):
    st = rwlib.read_json(job_dir(project, name) / "state.json")
    if st is None:
        rwlib.die("No job called %s. Create it with: batch.py init %s --items FILE" % (name, name))
    return st


def save(project, name, st):
    rwlib.write_json(job_dir(project, name) / "state.json", st)


def counts(st):
    c = {"pending": 0, "running": 0, "done": 0, "failed": 0}
    for it in st["items"].values():
        c[it["status"]] += 1
    return c


def cmd_init(project, a):
    path = Path(a.items)
    ids = []
    for line in path.read_text(encoding="utf-8").split("\n"):
        line = line.strip()
        if not line:
            continue
        if line.startswith("{"):
            ids.append(str(json.loads(line)["id"]))
        else:
            ids.append(line)
    if not ids:
        rwlib.die("No items in %s" % path)
    existing = rwlib.read_json(job_dir(project, a.name) / "state.json")
    if existing:
        added = 0
        for i in ids:
            if i not in existing["items"]:
                existing["items"][i] = {"status": "pending", "attempts": 0, "note": ""}
                existing["order"].append(i)
                added += 1
        save(project, a.name, existing)
        print("job %s exists: %d new item(s) added, %d already known" % (a.name, added, len(ids) - added))
        return 0
    st = {"name": a.name, "created": rwlib.now_iso(), "order": list(dict.fromkeys(ids)),
          "items": {i: {"status": "pending", "attempts": 0, "note": ""} for i in ids}}
    save(project, a.name, st)
    print("job %s created with %d item(s)" % (a.name, len(st["items"])))
    return 0


def safe_command(project, template, item):
    """Split the template into arguments, substitute placeholders, and refuse anything but this skill's scripts."""
    parts = shlex.split(template, posix=(os.name != "nt"))
    sub = {"{item}": item, "{python}": sys.executable, "{scripts}": str(rwlib.SCRIPTS_DIR), "{project}": str(project)}
    out = []
    for p in parts:
        p = p.strip('"') if os.name == "nt" else p
        for k, v in sub.items():
            p = p.replace(k, v)
        out.append(p)
    scripts = [p for p in out if p.lower().endswith(".py")]
    if not scripts:
        rwlib.die("The command must run one of this skill's scripts (a .py file).")
    for s in scripts:
        if not rwlib.inside(rwlib.SCRIPTS_DIR, s):
            rwlib.die("Refused: %s is not one of this skill's scripts." % s)
    return out


def cmd_run(project, a):
    st = load(project, a.name)
    for it in st["items"].values():
        if it["status"] == "running":
            it["status"] = "pending"
            it["note"] = "interrupted earlier; running again"
    save(project, a.name, st)
    started = time.time()
    ran = 0
    for item in st["order"]:
        it = st["items"][item]
        if it["status"] != "pending":
            continue
        if a.stop_after and ran >= a.stop_after:
            print("stopping after %d item(s) as asked (--stop-after)" % ran)
            break
        it["status"], it["attempts"] = "running", it["attempts"] + 1
        save(project, a.name, st)
        cmd = safe_command(project, a.cmdline, item)
        try:
            r = subprocess.run(cmd, cwd=str(project), capture_output=True, text=True, timeout=a.timeout)
            ok = r.returncode == 0
            tail = ((r.stdout or "").strip().splitlines() or [""])[-1][:200] if ok else ((r.stderr or r.stdout or "").strip().splitlines() or ["failed"])[-1][:200]
        except subprocess.TimeoutExpired:
            ok, tail = False, "timed out after %d s" % a.timeout
        it["status"], it["note"] = ("done" if ok else "failed"), tail
        done_log = job_dir(project, a.name) / "results.jsonl"
        if ok and item not in {r.get("id") for r in rwlib.read_jsonl(done_log)}:
            rwlib.append_jsonl(done_log, {"id": item, "date": rwlib.now_iso(), "note": tail})
        save(project, a.name, st)
        ran += 1
        print("%s: %s%s" % (item, it["status"], "" if ok else " (%s)" % tail))
    c = counts(st)
    print("job %s: done %d, failed %d, pending %d of %d  (%.0f s this run)" % (a.name, c["done"], c["failed"], c["pending"], len(st["items"]), time.time() - started))
    return 0 if c["done"] == len(st["items"]) else 1


def cmd_status(project, a):
    st = load(project, a.name)
    c = counts(st)
    print("job %s: done %d, running %d, failed %d, pending %d of %d" % (a.name, c["done"], c["running"], c["failed"], c["pending"], len(st["items"])))
    for i, it in st["items"].items():
        if it["status"] in ("failed", "running"):
            print("  %s: %s (attempts %d) %s" % (i, it["status"], it["attempts"], it["note"]))
    return 0 if c["done"] == len(st["items"]) else 1


def cmd_next(project, a):
    st = load(project, a.name)
    out = [i for i in st["order"] if st["items"][i]["status"] in ("pending", "running")][:a.n]
    print("\n".join(out) if out else "(nothing pending)")
    return 0


def cmd_mark(project, a, status):
    st = load(project, a.name)
    if a.item not in st["items"]:
        rwlib.die("Unknown item %s" % a.item)
    st["items"][a.item]["status"] = status
    st["items"][a.item]["note"] = a.note or ""
    save(project, a.name, st)
    print("%s marked %s" % (a.item, status))
    return 0


def cmd_retry(project, a):
    st = load(project, a.name)
    n = 0
    for it in st["items"].values():
        if it["status"] == "failed":
            it["status"] = "pending"
            n += 1
    save(project, a.name, st)
    print("%d failed item(s) set back to pending" % n)
    return 0


def cmd_list(project, a):
    base = project / "logs" / "jobs"
    for d in sorted(base.glob("*/state.json")) if base.exists() else []:
        st = rwlib.read_json(d)
        c = counts(st)
        print("%-24s done %d / %d  (failed %d, pending %d)" % (st["name"], c["done"], len(st["items"]), c["failed"], c["pending"]))
    return 0


def main():
    rwlib.setup_console()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("init")
    p.add_argument("name")
    p.add_argument("--items", required=True)
    p = sub.add_parser("run")
    p.add_argument("name")
    p.add_argument("--cmd", dest="cmdline", required=True)
    p.add_argument("--timeout", type=int, default=1800)
    p.add_argument("--stop-after", type=int, dest="stop_after")
    for n in ("status", "next", "retry"):
        p = sub.add_parser(n)
        p.add_argument("name")
        if n == "next":
            p.add_argument("--n", type=int, default=5)
    for n in ("done", "fail"):
        p = sub.add_parser(n)
        p.add_argument("name")
        p.add_argument("item")
        p.add_argument("--note")
    sub.add_parser("list")
    a = ap.parse_args()
    project = rwlib.find_project(explicit=a.project)
    if a.cmd in ("done", "fail"):
        return cmd_mark(project, a, "done" if a.cmd == "done" else "failed")
    return {"init": cmd_init, "run": cmd_run, "status": cmd_status, "next": cmd_next, "retry": cmd_retry, "list": cmd_list}[a.cmd](project, a)


if __name__ == "__main__":
    sys.exit(main())
