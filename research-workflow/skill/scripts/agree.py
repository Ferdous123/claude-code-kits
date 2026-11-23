#!/usr/bin/env python3
"""Turn two independent local passes into cards, queue the disagreements, apply the decisions.

Usage:
  agree.py merge --paper P0001
      Compare candidates/P0001/A.jsonl and B.jsonl. Only candidates whose quote was located count.
        both passes found the same span, type and value  -> a card, with `cross_read` = passed
        the passes overlap but differ in type/value/attribution -> review queue (passes_disagree)
        only one pass found it                            -> review queue (single_pass_only)
        quote not located                                 -> dropped, counted, never a card
  agree.py support
      Combine candidates/support/A.jsonl and B.jsonl into the `supported` check:
        both entails -> passed (label entails); both does_not -> failed; both partly -> flagged (partly);
        any other mix -> flagged and queued (support_disagree). One pass only -> nothing is written.
  agree.py absent --paper P0001 --types dataset,limitation
      For each type with no located candidate from either pass and no card, record a 'not stated' card
      (a deterministic fact about the passes, not a model's guess). Needs both passes complete.
  agree.py apply
      Apply review/decisions.jsonl (written after Claude or the user settled queue items).
  agree.py status
      Counts: open queue items, cards by origin.

Nothing here calls a model. A card created here is never human_verified.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import rwlib  # noqa: E402
import cards as cardlib  # noqa: E402
import check_cards  # noqa: E402


def norm_num(v):
    """Comparable form of a value's number, or None."""
    if not v or not v.get("number"):
        return None
    return rwlib.norm_simple(v["number"]).strip()


def overlap(a, b):
    """Share of the shorter span covered by the overlap of two (start, end) spans."""
    lo, hi = max(a[0], b[0]), min(a[1], b[1])
    if hi <= lo:
        return 0.0
    return (hi - lo) / float(min(a[1] - a[0], b[1] - b[0]))


def queue_add(project, reason, paper, page, cand_ids, card_id=None):
    """Add an item to review/queue.jsonl unless the same candidates are already queued."""
    q = rwlib.read_jsonl(project / "review" / "queue.jsonl")
    for it in q:
        if it["candidates"] == cand_ids and it["reason"] == reason:
            return None
    qid = rwlib.next_id("RQ", [i["queue_id"] for i in q])
    rwlib.append_jsonl(project / "review" / "queue.jsonl", {
        "queue_id": qid, "reason": reason, "paper_id": paper, "page": page, "card_id": card_id, "candidates": cand_ids,
        "state": "open", "created": rwlib.now_iso()})
    return qid


def add_cross_read(project, card_id, version, method, detail):
    """Record a passed cross_read check."""
    existing = [r.get("check_id") for r in rwlib.read_jsonl(project / "checks" / "checks.jsonl")]
    top = max([int(x[1:]) for x in existing if x and x[1:].isdigit()] or [0])
    rwlib.append_jsonl(project / "checks" / "checks.jsonl", {
        "check_id": "K%05d" % (top + 1), "card_id": card_id, "card_version": version, "check": "cross_read", "result": "passed",
        "label": None, "method": method, "detail": detail[:600], "evidence_hash": None, "run_id": None, "date": rwlib.now_iso()})


def card_from_candidate(project, cand, models, method, note, run_id=None):
    """Create a card from a located candidate. Returns the card or None."""
    p = cand["proposed"]
    card, res = cardlib.create_card(project, cand["paper_id"], cand["page"], p["quote"], p["type"], p["statement"], p.get("value"),
                                    p.get("attribution", "own"), p.get("attributed_to"), p.get("wording_strength", "asserted"),
                                    "extracted", "extract", models, note, p.get("field"), run_id)
    if card is None:
        return None
    add_cross_read(project, card["card_id"], card["version"], method, note)
    return card


def cmd_merge(project, a):
    """Merge the two extraction passes of one paper."""
    base = project / "candidates" / a.paper
    A = rwlib.read_jsonl(base / "A.jsonl")
    B = rwlib.read_jsonl(base / "B.jsonl")
    if not A or not B:
        rwlib.die("Both passes are needed (candidates/%s/A.jsonl and B.jsonl). Found A=%d, B=%d candidates." % (a.paper, len(A), len(B)))
    merged_path = base / "merged.json"
    merged = set(rwlib.read_json(merged_path, []))
    dropA = [c for c in A if c["located"]["result"] != "passed"]
    dropB = [c for c in B if c["located"]["result"] != "passed"]
    LA = [c for c in A if c["located"]["result"] == "passed" and c["cand_id"] not in merged]
    LB = [c for c in B if c["located"]["result"] == "passed" and c["cand_id"] not in merged]
    models = sorted({A[0]["model"], B[0]["model"]})
    used_b = set()
    n_agree = n_conf = n_single = 0
    for ca in LA:
        sa = (ca["located"]["start"], ca["located"]["end"])
        best, best_ov = None, 0.0
        for cb in LB:
            if cb["cand_id"] in used_b or cb["page"] != ca["page"]:
                continue
            ov = overlap(sa, (cb["located"]["start"], cb["located"]["end"]))
            if ov > best_ov:
                best, best_ov = cb, ov
        if best is not None and best_ov >= 0.5:
            used_b.add(best["cand_id"])
            pa, pb = ca["proposed"], best["proposed"]
            same = (pa["type"] == pb["type"] and norm_num(pa.get("value")) == norm_num(pb.get("value"))
                    and pa.get("attribution") == pb.get("attribution"))
            if same:
                note = "two independent local passes agree (%s and %s) on span, type, value and attribution" % (ca["model"], best["model"])
                longer = ca if len(pa["quote"]) >= len(pb["quote"]) else best
                card = card_from_candidate(project, longer, models, "local_two_pass", note)
                if card:
                    n_agree += 1
                merged.update([ca["cand_id"], best["cand_id"]])
            else:
                queue_add(project, "passes_disagree", a.paper, ca["page"], [ca["cand_id"], best["cand_id"]])
                n_conf += 1
                merged.update([ca["cand_id"], best["cand_id"]])
        else:
            queue_add(project, "single_pass_only", a.paper, ca["page"], [ca["cand_id"]])
            n_single += 1
            merged.add(ca["cand_id"])
    for cb in LB:
        if cb["cand_id"] not in used_b and cb["cand_id"] not in merged:
            queue_add(project, "single_pass_only", a.paper, cb["page"], [cb["cand_id"]])
            n_single += 1
            merged.add(cb["cand_id"])
    rwlib.write_json(merged_path, sorted(merged))
    print("merge %s: pass A located %d of %d candidates, pass B located %d of %d (the rest were dropped: quote not on the page)" % (
        a.paper, len(A) - len(dropA), len(A), len(B) - len(dropB), len(B)))
    print("  new this run: agreed -> cards %d; disagree -> queue %d; one pass only -> queue %d" % (n_agree, n_conf, n_single))
    return 0


def cmd_support(project, a):
    """Merge the two support passes."""
    A = {(c["proposed"]["card_id"], c["proposed"]["card_version"]): c for c in rwlib.read_jsonl(project / "candidates" / "support" / "A.jsonl")}
    B = {(c["proposed"]["card_id"], c["proposed"]["card_version"]): c for c in rwlib.read_jsonl(project / "candidates" / "support" / "B.jsonl")}
    checks = rwlib.current_checks(project)
    existing = [r.get("check_id") for r in rwlib.read_jsonl(project / "checks" / "checks.jsonl")]
    top = max([int(x[1:]) for x in existing if x and x[1:].isdigit()] or [0])
    counts = {"passed": 0, "failed": 0, "flagged": 0, "queued": 0, "one_pass": 0}
    for key in sorted(set(A) | set(B)):
        if key not in A or key not in B:
            counts["one_pass"] += 1
            continue
        if (key[0], key[1], "supported") in checks:
            continue
        la, lb = A[key]["proposed"]["label"], B[key]["proposed"]["label"]
        if la == lb == "entails":
            res, label = "passed", "entails"
        elif la == lb == "does_not":
            res, label = "failed", "does_not"
        elif la == lb == "partly" or {la, lb} == {"entails", "partly"}:
            res, label = "flagged", "partly"
        else:
            res, label = "flagged", None
        top += 1
        rwlib.append_jsonl(project / "checks" / "checks.jsonl", {
            "check_id": "K%05d" % top, "card_id": key[0], "card_version": key[1], "check": "supported", "result": res, "label": label,
            "method": "local_two_pass", "detail": "A (%s)=%s, B (%s)=%s" % (A[key]["model"], la, B[key]["model"], lb), "evidence_hash": None,
            "run_id": None, "date": rwlib.now_iso()})
        counts[res] += 1
        if label is None:
            queue_add(project, "support_disagree", None, A[key]["page"], [A[key]["cand_id"], B[key]["cand_id"]], key[0])
            counts["queued"] += 1
    print("support merge: passed %(passed)d, failed %(failed)d, flagged %(flagged)d (queued for a second reader: %(queued)d); "
          "cards with only one pass so far: %(one_pass)d" % counts)
    return 0


def cmd_absent(project, a):
    """Record not-stated cards for types neither pass found."""
    meta = rwlib.read_json(project / "parsed" / a.paper / "meta.json")
    if not meta:
        rwlib.die("%s is not parsed." % a.paper)
    for p in ("A", "B"):
        done = rwlib.read_json(project / "candidates" / a.paper / (p + ".done.json"), {"pages": {}})
        if len(done["pages"]) < meta["pages"]:
            rwlib.die("Pass %s has covered %d of %d pages. 'Not stated' needs both passes to have read every page." % (p, len(done["pages"]), meta["pages"]))
    found = set()
    for p in ("A", "B"):
        for c in rwlib.read_jsonl(project / "candidates" / a.paper / (p + ".jsonl")):
            if c["located"]["result"] == "passed":
                found.add(c["proposed"]["type"])
    cards = rwlib.current_cards(project)
    for c in cards.values():
        if c["paper_id"] == a.paper and c.get("stated") and c["review_state"] != "rejected":
            found.add(c["type"])
    wrote = 0
    for t in [x for x in a.types.split(",") if x]:
        if t in found:
            continue
        class NS:
            pass
        ns = NS()
        ns.paper, ns.field, ns.type, ns.origin, ns.role, ns.models = a.paper, t, t, "extracted", "extract", "local_two_pass"
        cardlib.cmd_not_stated(project, ns)
        wrote += 1
    print("absent: %d not-stated record(s) written (types found by the passes: %s)" % (wrote, ", ".join(sorted(found)) or "none"))
    return 0


def cmd_apply(project, a):
    """Apply decisions to queue items."""
    q = rwlib.read_jsonl(project / "review" / "queue.jsonl")
    decisions = {d["queue_id"]: d for d in rwlib.read_jsonl(project / "review" / "decisions.jsonl")}
    cands = {}
    for f in (project / "candidates").glob("*/*.jsonl"):
        for c in rwlib.read_jsonl(f):
            cands[c["cand_id"]] = c
    applied = 0
    for it in q:
        d = decisions.get(it["queue_id"])
        if it["state"] != "open" or not d:
            continue
        if d["decision"] != "reject" and d["by"].strip().lower() in ("", "none"):
            continue
        if it["reason"] in ("passes_disagree", "single_pass_only"):
            chosen = None
            if d["decision"] == "accept_A":
                chosen = next((cands[i] for i in it["candidates"] if cands[i]["pass_name"] == "A"), None) or cands[it["candidates"][0]]
            elif d["decision"] == "accept_B":
                chosen = next((cands[i] for i in it["candidates"] if cands[i]["pass_name"] == "B"), None) or cands[it["candidates"][0]]
            elif d["decision"] == "accept_edited":
                base = dict(cands[it["candidates"][0]])
                prop = dict(base["proposed"])
                prop.update(d.get("edited") or {})
                if "number" in (d.get("edited") or {}):
                    v = dict(prop.get("value") or {})
                    v["number"] = d["edited"]["number"]
                    prop["value"] = v
                base["proposed"] = prop
                chosen = base
            if chosen is not None:
                note = "settled by %s: %s" % (d["by"], d["reason"])
                card = card_from_candidate(project, chosen, [chosen["model"], d["by"]], "claude_adjudicated" if d["by"].lower() == "claude" else "person_adjudicated", note)
                if card is None:
                    print("%s: the chosen quote is not located; no card made" % it["queue_id"])
        elif it["reason"] == "support_disagree":
            label = {"accept_A": None, "accept_B": None}.get(d["decision"])
            pick = None
            if d["decision"] in ("accept_A", "accept_B"):
                want = d["decision"][-1]
                pick = next((cands[i] for i in it["candidates"] if cands[i]["pass_name"] == want), None)
                label = pick["proposed"]["label"] if pick else None
            elif d["decision"] == "accept_edited":
                label = (d.get("edited") or {}).get("label")
            elif d["decision"] == "reject":
                label = "does_not"
            if label in ("entails", "partly", "does_not"):
                cid = it["card_id"]
                cur = rwlib.current_cards(project)[cid]
                existing = [r.get("check_id") for r in rwlib.read_jsonl(project / "checks" / "checks.jsonl")]
                top = max([int(x[1:]) for x in existing if x and x[1:].isdigit()] or [0])
                res = {"entails": "passed", "partly": "flagged", "does_not": "failed"}[label]
                rwlib.append_jsonl(project / "checks" / "checks.jsonl", {
                    "check_id": "K%05d" % (top + 1), "card_id": cid, "card_version": cur["version"], "check": "supported", "result": res,
                    "label": label, "method": "adjudicated_by_" + d["by"].lower().replace(" ", "_"), "detail": d["reason"][:300],
                    "evidence_hash": None, "run_id": None, "date": rwlib.now_iso()})
        it["state"] = "decided"
        applied += 1
    rwlib.write_jsonl(project / "review" / "queue.jsonl", q)
    print("applied %d decision(s); %d queue item(s) still open" % (applied, sum(1 for i in q if i["state"] == "open")))
    return 0


def cmd_status(project, a):
    """Short counts."""
    q = rwlib.read_jsonl(project / "review" / "queue.jsonl")
    by = {}
    for i in q:
        if i["state"] == "open":
            by[i["reason"]] = by.get(i["reason"], 0) + 1
    cards = rwlib.current_cards(project)
    origins = {}
    for c in cards.values():
        origins[c["origin"]] = origins.get(c["origin"], 0) + 1
    print("open queue items: %d (%s)" % (sum(by.values()), ", ".join("%s=%d" % kv for kv in sorted(by.items())) or "none"))
    print("cards: %d (%s)" % (len(cards), ", ".join("%s=%d" % kv for kv in sorted(origins.items())) or "none"))
    return 0


def main():
    rwlib.setup_console()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("merge")
    p.add_argument("--paper", required=True)
    sub.add_parser("support")
    p = sub.add_parser("absent")
    p.add_argument("--paper", required=True)
    p.add_argument("--types", required=True)
    sub.add_parser("apply")
    sub.add_parser("status")
    a = ap.parse_args()
    project = rwlib.find_project(explicit=a.project)
    return {"merge": cmd_merge, "support": cmd_support, "absent": cmd_absent, "apply": cmd_apply, "status": cmd_status}[a.cmd](project, a)


if __name__ == "__main__":
    sys.exit(main())
