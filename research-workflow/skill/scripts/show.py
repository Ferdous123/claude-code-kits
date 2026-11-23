#!/usr/bin/env python3
"""Print cards, pages and queue items by id, in short fixed formats (so Claude reads little).

Usage:
  show.py card C0001 [C0002 ...]            one compact block per card
  show.py cards [--paper P0001] [--type T] [--state S] [--limit 30]   one line per card
  show.py page P0001 3 [--around "text"] [--width 600]               page text, or a window around a quote
  show.py paper P0001                        register line and parse summary
  show.py queue [--limit 20]                 open review items, with what Claude needs to settle each

Read-only.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import rwlib  # noqa: E402


def card_line(c, res):
    """One-line summary of a card with its checks."""
    chk = " ".join("%s=%s" % (k[:4], v) for k, v in res.items() if v != "not_run")
    return "%s v%d %s %s p%s [%s] %s | %s" % (c["card_id"], c["version"], c["paper_id"], c["type"],
                                           c["locators"][0]["page"] if c["locators"] else "-", c["review_state"],
                                           c["statement"][:90], chk or "no checks yet")


def cmd_card(project, a):
    cards = rwlib.current_cards(project)
    checks = rwlib.current_checks(project)
    for cid in a.ids:
        c = cards.get(cid)
        if not c:
            print("%s: no such card" % cid)
            continue
        res = rwlib.check_result(checks, c)
        print("%s v%d  paper %s  type %s  origin %s  attribution %s  state %s%s" % (
            cid, c["version"], c["paper_id"], c["type"], c["origin"], c["attribution"], c["review_state"],
            "  ABSTRACT-ONLY" if c.get("abstract_only") else ""))
        print("  statement: %s" % c["statement"])
        if c.get("value"):
            v = c["value"]
            print("  value: %s %s | metric %s | dataset %s" % (v.get("number"), v.get("unit") or "", v.get("metric"), v.get("dataset")))
        for l in c["locators"]:
            print("  locator: page %s, zone %s, blocks %s" % (l["page"], l["zone"], ",".join(l["block_ids"])))
            print('  quote: "%s"' % l["quote"])
        print("  checks: " + ", ".join("%s=%s" % kv for kv in res.items()))
        sup = checks.get((cid, c["version"], "supported"))
        if sup and sup.get("label"):
            print("  supported label: %s (%s)" % (sup["label"], sup["method"]))
    return 0


def cmd_cards(project, a):
    cards = rwlib.current_cards(project)
    checks = rwlib.current_checks(project)
    n = 0
    for cid in sorted(cards):
        c = cards[cid]
        if a.paper and c["paper_id"] != a.paper:
            continue
        if a.type and c["type"] != a.type:
            continue
        if a.state and c["review_state"] != a.state:
            continue
        print(card_line(c, rwlib.check_result(checks, c)))
        n += 1
        if n >= a.limit:
            print("... (more; raise --limit or narrow the filter)")
            break
    if n == 0:
        print("no cards match")
    return 0


def cmd_page(project, a):
    page = rwlib.load_page(project, a.paper, a.page)
    if not page:
        rwlib.die("Page %d of %s is not parsed." % (a.page, a.paper))
    text = page["text"]
    if a.around:
        f = rwlib.find_quote(text, a.around)
        if not f:
            print("(text not found on this page)")
            return 1
        lo, hi = max(0, f[1] - a.width // 2), min(len(text), f[2] + a.width // 2)
        print("%s p%d chars %d-%d of %d (match at %d-%d):" % (a.paper, a.page, lo, hi, len(text), f[1], f[2]))
        print(text[lo:hi])
    else:
        print("%s p%d (%d chars, %d blocks):" % (a.paper, a.page, len(text), len(page["blocks"])))
        print(text)
    return 0


def cmd_paper(project, a):
    reg = rwlib.load_register(project)
    r = reg.get(a.paper)
    if not r:
        rwlib.die("Unknown paper %s" % a.paper)
    print("%s  %s  state %s (%s)  screening %s  pages %s  licence %s" % (r["paper_id"], r["title"], r["state"], r["state_reason"], r["screening"], r.get("pages"), r["licence"]))
    meta = rwlib.read_json(project / "parsed" / a.paper / "meta.json")
    if meta:
        print("parsed: %d pages, %d chars, text layer %s, low-text pages %s" % (meta["pages"], meta["total_chars"], meta["text_layer"], meta["low_text_pages"]))
    return 0


def cmd_queue(project, a):
    q = [i for i in rwlib.read_jsonl(project / "review" / "queue.jsonl") if i["state"] == "open"]
    cands = {}
    for f in (project / "candidates").glob("*/*.jsonl"):
        for c in rwlib.read_jsonl(f):
            cands[c["cand_id"]] = c
    print("%d open item(s); showing %d" % (len(q), min(len(q), a.limit)))
    for it in q[:a.limit]:
        print("--- %s  %s  paper %s page %s" % (it["queue_id"], it["reason"], it.get("paper_id"), it.get("page")))
        for cid in it["candidates"]:
            c = cands.get(cid)
            if not c:
                continue
            p = c["proposed"]
            if c["kind"] == "card":
                v = p.get("value") or {}
                print("  %s (%s): type=%s value=%s attribution=%s" % (c["pass_name"], c["model"], p["type"], v.get("number"), p.get("attribution")))
                print('     statement: %s' % p["statement"][:160])
                print('     quote: "%s"' % p["quote"][:240])
            else:
                print("  %s (%s): card %s -> %s (%s)" % (c["pass_name"], c["model"], p["card_id"], p["label"], p["reason"][:120]))
        if it.get("card_id"):
            print("  (show.py card %s for the card text)" % it["card_id"])
    print("Settle each with a line in review/decisions.jsonl (see SKILL.md (local-model policy), 'Adjudication').")
    return 0


def main():
    rwlib.setup_console()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("card")
    p.add_argument("ids", nargs="+")
    p = sub.add_parser("cards")
    p.add_argument("--paper")
    p.add_argument("--type")
    p.add_argument("--state")
    p.add_argument("--limit", type=int, default=30)
    p = sub.add_parser("page")
    p.add_argument("paper")
    p.add_argument("page", type=int)
    p.add_argument("--around")
    p.add_argument("--width", type=int, default=600)
    p = sub.add_parser("paper")
    p.add_argument("paper")
    p = sub.add_parser("queue")
    p.add_argument("--limit", type=int, default=20)
    a = ap.parse_args()
    project = rwlib.find_project(explicit=a.project)
    return {"card": cmd_card, "cards": cmd_cards, "page": cmd_page, "paper": cmd_paper, "queue": cmd_queue}[a.cmd](project, a)


if __name__ == "__main__":
    sys.exit(main())
