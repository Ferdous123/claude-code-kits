#!/usr/bin/env python3
"""Run the deterministic checks on evidence cards: located, zone_valid and (the number part of) consistent.

Usage:
  check_cards.py run [--project DIR] [--cards C0001 C0002 ... | --all] [--recheck]
  check_cards.py explain CARD [--project DIR]

`located`   proves that each locator's quoted text is on the stated page: first character for character
            ("exact"), then after only these changes ("normalised"): composed Unicode form, ligatures,
            curly quotes, soft hyphens, white space, line-end hyphenation. No fuzzy matching of any kind.
            Every digit, sign, symbol and negation word must match exactly. For a result card the card's
            number must itself occur inside a quote, as a whole number (not inside a longer one, and with the
            same sign). The locator's zone and block ids must equal what the parsed page says.
            It does NOT prove the claim is true, and it does not check that the text is visible on the page.
`zone_valid` proves the locator's zone is one the card type may come from (data/zone_rules.json).
`consistent` here only checks that every number in the card's statement also occurs in its quote
            (result: flagged if not). Cross-paper consistency is judged by a person or by Claude, not here.

Results are appended to checks/checks.jsonl. The words used are passed / failed / flagged / not_run.
There is no check called "verified". Cards already checked at their current version, on an unchanged page,
are skipped unless --recheck is given.
"""
import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import rwlib  # noqa: E402

RULES = rwlib.read_json(rwlib.DATA_DIR / "zone_rules.json")


def number_in_text(num, text):
    """True if the number `num` (as printed) occurs in `text` as a whole number with the same sign."""
    num = num.strip()
    if not num:
        return False
    t = rwlib.norm_simple(text)
    n = rwlib.norm_simple(num)
    pat = r"(?<![\d.,\-−+])" + re.escape(n) + r"(?!\d|[.,]\d)"
    return re.search(pat, t) is not None


_NUM = re.compile(r"(?<![\w.])([-−])?(\d+(?:[.,]\d+)*)")


def numbers_in(text):
    """Set of (sign, digits) tokens found in text; the two minus characters are treated alike."""
    out = set()
    for m in _NUM.finditer(rwlib.norm_simple(text)):
        out.add(("-" if m.group(1) else "", m.group(2)))
    return out


def zone_of(blocks, start, end):
    """Most restrictive zone among the blocks overlapping [start, end), plus their ids and union box."""
    hit = [b for b in blocks if b["start"] < end and b["end"] > start]
    if not hit:
        return "unknown", [], None
    order = RULES["restrictiveness_order"]
    zones = [b["zone"] for b in hit]
    zone = sorted(zones, key=lambda z: order.index(z) if z in order else 0)[0]
    boxes = [b["bbox"] for b in hit if b.get("bbox")]
    box = None
    if boxes:
        box = [round(min(x[0] for x in boxes), 2), round(min(x[1] for x in boxes), 2),
               round(max(x[2] for x in boxes), 2), round(max(x[3] for x in boxes), 2)]
    return zone, [b["block_id"] for b in hit], box


def locate_quote(project, paper_id, page_no, quote):
    """Locate one quote on one page. Returns a dict with result, method, offsets, zone, block ids, box and evidence hash."""
    page = rwlib.load_page(project, paper_id, page_no)
    if page is None:
        return {"result": "failed", "method": "none", "detail": "page %s of %s has not been parsed" % (page_no, paper_id)}
    found = rwlib.find_quote(page["text"], quote)
    if not found:
        return {"result": "failed", "method": "none", "detail": "quote not found on page %s" % page_no,
                "evidence_hash": rwlib.sha256_text(quote + "\n" + page["text_sha256"])}
    method, s, e = found
    zone, ids, box = zone_of(page["blocks"], s, e)
    return {"result": "passed", "method": method, "start": s, "end": e, "zone": zone, "block_ids": ids, "bbox": box,
            "detail": "page %d, characters %d-%d" % (page_no, s, e),
            "evidence_hash": rwlib.sha256_text(quote + "\n" + page["text_sha256"])}


def evaluate_card(project, card):
    """Return (located_rec, zone_rec, consistent_rec) as partial dicts for one card (no ids or dates)."""
    if not card.get("stated", True):
        note = "not-stated record: no locator needed, and it is never evidence"
        return ({"result": "not_run", "method": "rule", "detail": note, "evidence_hash": None},
                {"result": "not_run", "method": "rule", "detail": note},
                {"result": "not_run", "method": "rule", "detail": note})
    locs = card.get("locators", [])
    problems, details, hashes, zone_probs, zones = [], [], [], [], []
    quotes = []
    methods = set()
    for l in locs:
        r = locate_quote(project, card["paper_id"], l["page"], l["quote"])
        quotes.append(l["quote"])
        hashes.append(r.get("evidence_hash") or "")
        if r["result"] != "passed":
            problems.append("p%s: %s" % (l["page"], r["detail"]))
            continue
        methods.add(r["method"])
        details.append(r["detail"] + " (%s)" % r["method"])
        if r["zone"] != l.get("zone") or r["block_ids"] != l.get("block_ids"):
            problems.append("p%s: locator zone/blocks (%s %s) differ from the parsed page (%s %s)" %
                            (l["page"], l.get("zone"), l.get("block_ids"), r["zone"], r["block_ids"]))
        zones.append(r["zone"])
    if not locs:
        problems.append("no locator")
    val = card.get("value")
    if not problems and card.get("type") == "result" and val and val.get("number"):
        if not any(number_in_text(val["number"], q) for q in quotes):
            problems.append("the value %s does not occur inside any quote" % val["number"])
    ev = rwlib.sha256_text("|".join(hashes)) if hashes else None
    if problems:
        loc = {"result": "failed", "method": "none", "detail": "; ".join(problems)[:600], "evidence_hash": ev}
    else:
        loc = {"result": "passed", "method": "exact" if methods == {"exact"} else "normalised",
               "detail": "; ".join(details)[:600], "evidence_hash": ev}
    # zone_valid
    if loc["result"] != "passed":
        zone = {"result": "not_run", "method": "rule", "detail": "needs located to pass first"}
    else:
        allowed = RULES["allowed"].get(card["type"], [])
        for z in zones:
            zz = "body" if z == "heading" else z
            if zz == "bibliography":
                zone_probs.append("quote is in the bibliography")
            elif zz == "related_work" and card.get("attribution") == "own":
                zone_probs.append("related-work text cannot be recorded as the paper's own finding")
            elif zz == "unknown":
                zone_probs.append("zone unknown")
            elif zz not in allowed and not (zz == "related_work" and card.get("attribution") != "own" and RULES.get("cited_work_may_use_related_work")):
                zone_probs.append("zone %s is not allowed for a %s card" % (zz, card["type"]))
        if zone_probs:
            res = "flagged" if all(p == "zone unknown" for p in zone_probs) else "failed"
            zone = {"result": res, "method": "rule", "detail": "; ".join(sorted(set(zone_probs)))}
        else:
            zone = {"result": "passed", "method": "rule", "detail": "zones: " + ", ".join(sorted(set(zones)))}
    # consistent (numbers in statement)
    if loc["result"] != "passed":
        cons = {"result": "not_run", "method": "statement_numbers", "detail": "needs located to pass first"}
    else:
        nums = numbers_in(card["statement"])
        have = set()
        for q in quotes:
            have |= numbers_in(q)
        if val and val.get("number"):
            have |= numbers_in(val["number"])
        missing = sorted(n for n in nums if n not in have)
        if missing:
            cons = {"result": "flagged", "method": "statement_numbers",
                    "detail": "numbers in the statement not found in the quote: " + ", ".join(s + d for s, d in missing)}
        else:
            cons = {"result": "passed", "method": "statement_numbers", "detail": "every number in the statement occurs in the quote"}
    return loc, zone, cons


def apply_checks(project, card, run_id=None):
    """Run the three checks for one card version and append the records. Returns {check: result}."""
    existing = [r.get("check_id") for r in rwlib.read_jsonl(project / "checks" / "checks.jsonl")]
    counter = int(max([int(x[1:]) for x in existing if x and x[1:].isdigit()] or [0]))
    run_id = run_id or "R" + rwlib.now_iso()[:19].replace("-", "").replace(":", "").replace("T", "-")
    loc, zone, cons = evaluate_card(project, card)
    out = {}
    for name, rec in (("located", loc), ("zone_valid", zone), ("consistent", cons)):
        counter += 1
        rwlib.append_jsonl(project / "checks" / "checks.jsonl", {
            "check_id": "K%05d" % counter, "card_id": card["card_id"], "card_version": card["version"], "check": name,
            "result": rec["result"], "label": None, "method": rec["method"], "detail": rec["detail"],
            "evidence_hash": rec.get("evidence_hash"), "run_id": run_id, "date": rwlib.now_iso()})
        out[name] = rec["result"]
    return out


def cmd_run(project, a):
    """Run the checks for the chosen cards and append the results."""
    cards = rwlib.current_cards(project)
    checks = rwlib.current_checks(project)
    chosen = sorted(cards) if a.all or not a.cards else a.cards
    if not chosen:
        print("checked 0 cards (denominator 0): there are no cards to check yet. This is not a pass.")
        return 1
    existing_ids = [r.get("check_id") for r in rwlib.read_jsonl(project / "checks" / "checks.jsonl")]
    counter = int(max([int(x[1:]) for x in existing_ids if x and x[1:].isdigit()] or [0]))
    run_id = "R" + rwlib.now_iso()[:19].replace("-", "").replace(":", "").replace("T", "-")
    tally = {"located": {}, "zone_valid": {}, "consistent": {}}
    skipped = 0
    for cid in chosen:
        if cid not in cards:
            print("unknown card %s" % cid)
            continue
        card = cards[cid]
        prev = checks.get((cid, card["version"], "located"))
        if prev and not a.recheck:
            # stale if the page changed since the last check
            loc, _, _ = evaluate_card(project, card)
            if loc.get("evidence_hash") == prev.get("evidence_hash"):
                skipped += 1
                continue
        loc, zone, cons = evaluate_card(project, card)
        for name, rec in (("located", loc), ("zone_valid", zone), ("consistent", cons)):
            counter += 1
            out = {"check_id": "K%05d" % counter, "card_id": cid, "card_version": card["version"], "check": name,
                   "result": rec["result"], "label": None, "method": rec["method"], "detail": rec["detail"],
                   "evidence_hash": rec.get("evidence_hash"), "run_id": run_id, "date": rwlib.now_iso()}
            rwlib.append_jsonl(project / "checks" / "checks.jsonl", out)
            tally[name][rec["result"]] = tally[name].get(rec["result"], 0) + 1
    n = len(chosen) - skipped
    print("checked %d card version(s) (skipped %d already checked and unchanged)" % (n, skipped))
    for name in ("located", "zone_valid", "consistent"):
        print("  %-10s %s" % (name, ", ".join("%s=%d" % kv for kv in sorted(tally[name].items())) or "-"))
    bad = tally["located"].get("failed", 0)
    if bad:
        print("%d card(s) failed `located`: they are not citable until corrected or rejected." % bad)
    return 0


def cmd_explain(project, a):
    """Show how one card scores, with the reasons."""
    cards = rwlib.current_cards(project)
    if a.card not in cards:
        rwlib.die("Unknown card %s" % a.card)
    loc, zone, cons = evaluate_card(project, cards[a.card])
    print(json.dumps({"located": loc, "zone_valid": zone, "consistent": cons}, indent=2, ensure_ascii=False))
    return 0


def main():
    rwlib.setup_console()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="run the checks")
    r.add_argument("--cards", nargs="*")
    r.add_argument("--all", action="store_true")
    r.add_argument("--recheck", action="store_true")
    e = sub.add_parser("explain", help="show reasons for one card")
    e.add_argument("card")
    a = ap.parse_args()
    project = rwlib.find_project(explicit=a.project)
    return cmd_run(project, a) if a.cmd == "run" else cmd_explain(project, a)


if __name__ == "__main__":
    sys.exit(main())
