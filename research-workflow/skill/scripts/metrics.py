#!/usr/bin/env python3
"""Count things with their denominators, record measured results, sample pages for audits.

Usage:
  metrics.py report [--project DIR]
      Project counts with denominators (operational signals, never accuracy).
  metrics.py record --pack ID --pack-version V --pack-kind smoke|screen|reference --role R --model M [--family F]
                    --measure "name|numerator|denominator[|op|threshold|min_n]" [--measure ...]
                    [--seconds S] [--tokens-per-second T] [--calls N] [--notes TEXT]
      Append one measured result to evals/results.jsonl. REFUSED without a pack id, a pack version, and a
      denominator for every measure. Fewer items than min_n gives the verdict "inconclusive", never "passed".
  metrics.py results [--role R]
      Table of recorded results (each line names its pack, version, denominator).
  metrics.py sample --n 5 --seed 1 [--papers P0001 ...]
      Seeded random pages (full-text papers) for an audit: Claude reads these pages itself and compares
      with the cards. Same seed, same pages.
  metrics.py wilson K N
      95 percent interval for K successes in N.

Rates use the Wilson interval. A measure over fewer than 30 items is shown as a count, not a percentage.
"""
import argparse
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import rwlib  # noqa: E402


def cmd_report(project, a):
    reg = rwlib.load_register(project)
    cards = rwlib.current_cards(project)
    checks = rwlib.current_checks(project)
    print("OPERATIONAL SIGNALS on this project's own papers. They show trends and problems; they are not accuracy.")
    by = {}
    for r in reg.values():
        by[r["state"]] = by.get(r["state"], 0) + 1
    print("papers registered: %d (%s)" % (len(reg), ", ".join("%s=%d" % kv for kv in sorted(by.items())) or "none"))
    stated = [c for c in cards.values() if c.get("stated") and c["review_state"] != "rejected"]
    res = {c["card_id"]: rwlib.check_result(checks, c) for c in stated}
    print("cards (current versions, stated, not rejected): %d; not-stated records: %d; rejected: %d" % (
        len(stated), sum(1 for c in cards.values() if not c.get("stated")), sum(1 for c in cards.values() if c["review_state"] == "rejected")))
    for name in ("located", "zone_valid", "consistent", "supported", "cross_read"):
        ran = [cid for cid in res if res[cid][name] != "not_run"]
        ok = [cid for cid in ran if res[cid][name] == "passed"]
        print("  %-10s passed %s   [denominator: stated cards on which the check ran]" % (name, rwlib.fmt_rate(len(ok), len(ran))))
    ver = sum(1 for c in stated if c["review_state"] == "human_verified")
    print("  human_verified: %s   [denominator: all stated cards]" % rwlib.fmt_rate(ver, len(stated)))
    corr = rwlib.read_jsonl(project / "corrections" / "corrections.jsonl")
    print("corrections: %d (about %.1f per 100 cards)" % (len(corr), 100.0 * len(corr) / max(1, len(stated))))
    q = rwlib.read_jsonl(project / "review" / "queue.jsonl")
    print("review queue: %d open of %d" % (sum(1 for i in q if i["state"] == "open"), len(q)))
    gaps = rwlib.read_jsonl(project / "gaps" / "gaps.jsonl")
    gs = {}
    for g in gaps:
        gs[g["state"]] = gs.get(g["state"], 0) + 1
    print("candidate gaps: %d (%s)" % (len(gaps), ", ".join("%s=%d" % kv for kv in sorted(gs.items())) or "none"))
    return 0


def parse_measure(s):
    parts = s.split("|")
    if len(parts) not in (3, 6):
        rwlib.die("--measure needs 'name|numerator|denominator' or 'name|numerator|denominator|op|threshold|min_n', got %r" % s)
    m = {"name": parts[0].strip(), "numerator": int(parts[1]), "denominator": int(parts[2])}
    if not m["name"]:
        rwlib.die("A measure needs a name that says what it was measured on.")
    if m["denominator"] < 1:
        rwlib.die("REFUSED: measure %r has no denominator. A number without its denominator is not recorded." % m["name"])
    if m["numerator"] < 0 or m["numerator"] > m["denominator"]:
        rwlib.die("numerator must be between 0 and the denominator for %r" % m["name"])
    if len(parts) == 6:
        m["op"], m["threshold"], m["min_n"] = parts[3].strip(), float(parts[4]), int(parts[5])
        if m["op"] not in (">=", "<="):
            rwlib.die("op must be >= or <=")
    return m


def evaluate(m):
    """Fill value, interval, passed for one measure dict."""
    k, n = m["numerator"], m["denominator"]
    lo, hi = rwlib.wilson(k, n)
    out = {"name": m["name"], "numerator": k, "denominator": n, "value": round(k / n, 4),
           "ci_low": round(lo, 4), "ci_high": round(hi, 4), "threshold": m.get("threshold"), "op": m.get("op"), "min_n": m.get("min_n"), "passed": None}
    if "threshold" in m:
        if n < m["min_n"]:
            out["passed"] = None
        elif m["op"] == ">=":
            out["passed"] = (k / n) >= m["threshold"]
        else:
            out["passed"] = (k / n) <= m["threshold"]
    return out


def verdict_of(measures):
    judged = [m for m in measures if m["threshold"] is not None]
    if not judged:
        return "inconclusive"
    if any(m["passed"] is False for m in judged):
        return "failed"
    if any(m["passed"] is None for m in judged):
        return "inconclusive"
    return "passed"


def cmd_record(project, a):
    if not a.pack or not a.pack_version:
        rwlib.die("REFUSED: a result needs --pack and --pack-version.")
    measures = [evaluate(parse_measure(s)) for s in a.measure]
    prev = rwlib.read_jsonl(project / "evals" / "results.jsonl")
    run_id = "R" + rwlib.now_iso()[:19].replace("-", "").replace(":", "").replace("T", "-")
    if any(p.get("run_id") == run_id for p in prev):
        run_id += "-%d" % (len(prev) + 1)
    facts = rwlib.read_json(project / "machine" / "facts.json", {}) or {}
    rec = {"run_id": run_id, "date": rwlib.now_iso(), "pack_id": a.pack, "pack_version": a.pack_version, "pack_kind": a.pack_kind,
           "role": a.role, "model": a.model, "family": a.family or "unknown", "machine_ref": facts.get("fingerprint", "unknown"),
           "measures": measures,
           "speed": {"seconds_total": a.seconds or 0.0, "tokens_per_second": a.tokens_per_second, "calls": a.calls or 0},
           "verdict": verdict_of(measures), "notes": a.notes or ""}
    rwlib.append_jsonl(project / "evals" / "results.jsonl", rec)
    print("recorded %s: verdict %s" % (run_id, rec["verdict"]))
    for m in measures:
        print("  %s: %s%s" % (m["name"], rwlib.fmt_rate(m["numerator"], m["denominator"], m["min_n"] or 30),
                              "" if m["threshold"] is None else "  [threshold %s %s, min items %s -> %s]" % (
                                  m["op"], m["threshold"], m["min_n"], {True: "met", False: "NOT met", None: "too few items"}[m["passed"]])))
    return 0


def cmd_results(project, a):
    rows = rwlib.read_jsonl(project / "evals" / "results.jsonl")
    n = 0
    for r in rows:
        if a.role and r["role"] != a.role:
            continue
        n += 1
        print("%s  %s  role %s  model %s  pack %s v%s (%s)  verdict %s  speed %s tok/s" % (
            r["run_id"], r["date"][:10], r["role"], r["model"], r["pack_id"], r["pack_version"], r["pack_kind"], r["verdict"],
            (r.get("speed") or {}).get("tokens_per_second")))
        for m in r["measures"]:
            print("      %s: %d of %d" % (m["name"], m["numerator"], m["denominator"]))
    if n == 0:
        print("no results recorded yet")
    return 0


def cmd_sample(project, a):
    reg = rwlib.load_register(project)
    pool = []
    for pid in sorted(reg):
        if a.papers and pid not in a.papers:
            continue
        meta = rwlib.read_json(project / "parsed" / pid / "meta.json")
        if meta and reg[pid]["state"] == "full_text":
            for p in range(1, meta["pages"] + 1):
                if p not in meta["low_text_pages"]:
                    pool.append((pid, p))
    rng = random.Random(a.seed)
    pick = sorted(rng.sample(pool, min(a.n, len(pool))))
    print("seed %d: %d page(s) sampled from %d eligible pages" % (a.seed, len(pick), len(pool)))
    for pid, p in pick:
        print("  %s page %d" % (pid, p))
    return 0


def cmd_wilson(project, a):
    print(rwlib.fmt_rate(a.k, a.n))
    return 0


def main():
    rwlib.setup_console()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("report")
    p = sub.add_parser("record")
    p.add_argument("--pack")
    p.add_argument("--pack-version")
    p.add_argument("--pack-kind", default="reference", choices=["smoke", "screen", "reference"])
    p.add_argument("--role", required=True)
    p.add_argument("--model", required=True)
    p.add_argument("--family")
    p.add_argument("--measure", action="append", required=True)
    p.add_argument("--seconds", type=float)
    p.add_argument("--tokens-per-second", type=float, dest="tokens_per_second")
    p.add_argument("--calls", type=int)
    p.add_argument("--notes")
    p = sub.add_parser("results")
    p.add_argument("--role")
    p = sub.add_parser("sample")
    p.add_argument("--n", type=int, default=5)
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--papers", nargs="*")
    p = sub.add_parser("wilson")
    p.add_argument("k", type=int)
    p.add_argument("n", type=int)
    a = ap.parse_args()
    if a.cmd == "wilson":
        return cmd_wilson(None, a)
    project = rwlib.find_project(explicit=a.project)
    return {"report": cmd_report, "record": cmd_record, "results": cmd_results, "sample": cmd_sample}[a.cmd](project, a)


if __name__ == "__main__":
    sys.exit(main())
