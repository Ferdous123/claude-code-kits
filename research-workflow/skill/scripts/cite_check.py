#!/usr/bin/env python3
"""Decide what each card may be cited for, and check drafts, answers and dossiers against that.

Usage:
  cite_check.py cards C0001 C0002 ... [--context answer|dossier|export]
  cite_check.py draft DR0001 [--context export] [--manifest]
  cite_check.py dossier D0001
  cite_check.py table                       print the citability table

Standing of a card (strongest last):
  none             located or zone_valid failed or never ran; a not-stated record; a rejected card
  context_only     the paper was read only as an abstract
  disputed_only    `supported` failed, or the card is disputed: may appear in a dossier only, visibly
  needs_review     `supported`, `consistent` or `cross_read` flagged: answers and dossiers, marked
  located_only     located and zone_valid passed, nothing else checked yet: answers and dossiers, marked
  machine_checked  located, zone_valid, consistent passed and (supported or cross_read passed)
  human_verified   a person looked at the source passage
A card derived from others takes the weakest standing of its inputs. For export, any number the text relies on
(result cards) also needs `cross_read` passed or human_verified.
Export allows only machine_checked and human_verified, and reports how many cards a person verified.
There is no check or standing called "verified" apart from human_verified.

Exit code 0 = everything citable in that context; 1 = something blocks. Read-only except --manifest, which
writes drafts/<id>.manifest.md inside the project when the export check passes.
"""
import argparse
import difflib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import rwlib  # noqa: E402

ORDER = ["none", "context_only", "disputed_only", "needs_review", "located_only", "machine_checked", "human_verified"]
ALLOWED = {
    "answer": {"needs_review", "located_only", "machine_checked", "human_verified"},
    "dossier": {"disputed_only", "needs_review", "located_only", "machine_checked", "human_verified"},
    "export": {"machine_checked", "human_verified"},
}


def standing(card, res, export_numeric=False):
    """Return (standing, reason) for one card given its check results."""
    if not card.get("stated", True):
        return "none", "a not-stated record is not evidence"
    if card["review_state"] == "rejected":
        return "none", "rejected"
    if res["located"] != "passed":
        return "none", "located is %s" % res["located"]
    if res["zone_valid"] != "passed":
        return "none", "zone_valid is %s" % res["zone_valid"]
    if card.get("abstract_only"):
        return "context_only", "abstract-only source"
    if card["review_state"] == "disputed" or res["supported"] == "failed":
        return "disputed_only", "supported failed or card disputed"
    if card["review_state"] == "human_verified":
        return "human_verified", "verified by %s" % card.get("reviewed_by")
    if any(res[k] == "flagged" for k in ("supported", "consistent", "cross_read")) or res["consistent"] == "failed":
        return "needs_review", "flagged: " + ", ".join(k for k in ("supported", "consistent", "cross_read") if res[k] == "flagged")
    if res["consistent"] == "passed" and (res["supported"] == "passed" or res["cross_read"] == "passed"):
        if export_numeric and card["type"] == "result" and res["cross_read"] != "passed":
            return "located_only", "result value needs a second reading (cross_read) or a person's look before export"
        return "machine_checked", "machine checks passed"
    return "located_only", "only located/zone_valid passed so far"


def weakest(standings):
    return min(standings, key=lambda s: ORDER.index(s)) if standings else "none"


def card_states(project):
    cards = rwlib.current_cards(project)
    checks = rwlib.current_checks(project)
    return cards, {cid: rwlib.check_result(checks, c) for cid, c in cards.items()}


def cmd_cards(project, a):
    cards, res = card_states(project)
    bad = 0
    for cid in a.cards:
        if cid not in cards:
            print("%s: no such card -> blocks" % cid)
            bad += 1
            continue
        st, why = standing(cards[cid], res[cid], a.context == "export")
        ok = st in ALLOWED[a.context]
        print("%s  %-15s %s for %s  (%s)" % (cid, st, "OK" if ok else "BLOCKED", a.context, why))
        bad += 0 if ok else 1
    return 1 if bad else 0


def cmd_draft(project, a):
    path = project / "drafts" / (a.draft + ".sentences.jsonl")
    sents = rwlib.read_jsonl(path)
    if not sents:
        rwlib.die("No sentences in %s" % path)
    cards, res = card_states(project)
    bad, n_sup, used = [], 0, set()
    tally = {}
    for s in sorted(sents, key=lambda x: x["order"]):
        sid = s["sentence_id"]
        if s["kind"] == "blocked":
            bad.append("%s: blocked sentence (no support yet): %s" % (sid, s["text"][:70]))
            continue
        if s["kind"] == "author_argument":
            continue
        n_sup += 1
        if not s["card_ids"]:
            bad.append("%s: supported sentence with no cards" % sid)
            continue
        sts = []
        for cid in s["card_ids"]:
            if cid not in cards:
                bad.append("%s: card %s does not exist" % (sid, cid))
                sts.append("none")
                continue
            st, why = standing(cards[cid], res[cid], a.context == "export")
            sts.append(st)
            used.add(cid)
        w = weakest(sts)
        tally[w] = tally.get(w, 0) + 1
        if w not in ALLOWED[a.context]:
            bad.append("%s: weakest card standing is %s (needs %s for %s): %s" % (sid, w, "/".join(sorted(ALLOWED[a.context])), a.context, s["text"][:60]))
    verified = sum(1 for c in used if cards[c]["review_state"] == "human_verified")
    print("draft %s: %d sentence(s), %d rest on cards (standing of the weakest card per sentence: %s)" % (
        a.draft, len(sents), n_sup, ", ".join("%s=%d" % kv for kv in sorted(tally.items())) or "-"))
    print("cards used: %d; verified by a person: %d of %d%s" % (len(used), verified, len(used),
          "  (NOTICE for page one of any export: no card was verified by a person)" if used and verified == 0 else ""))
    for b in bad:
        print("BLOCKS " + b)
    if bad:
        print("RESULT: %s is not ready for %s (%d problem(s))" % (a.draft, a.context, len(bad)))
        return 1
    print("RESULT: every supported sentence is citable for %s" % a.context)
    if a.manifest and a.context == "export":
        snap = (rwlib.read_json(project / "project.json") or {}).get("corpus_snapshot")
        lines = ["# Manifest for %s" % a.draft, "", "Written: %s" % rwlib.now_iso(), "Corpus snapshot: %s" % snap,
                 "Cards used: %d; verified by a person: %d (%.0f%%)" % (len(used), verified, 100.0 * verified / max(1, len(used))), "",
                 "## Provenance of sentences", ""]
        for s in sorted(sents, key=lambda x: x["order"]):
            change = ""
            if s["origin"] == "machine" and s.get("machine_text"):
                ratio = difflib.SequenceMatcher(None, s["machine_text"], s["text"]).ratio()
                change = ", edited by the author: %.0f%% of characters changed" % (100 * (1 - ratio))
            lines.append("- %s: origin %s, %s%s; cards %s" % (s["sentence_id"], s["origin"], s["kind"], change, ",".join(s["card_ids"]) or "-"))
        lines += ["", "## Cards", ""]
        for cid in sorted(used):
            c = cards[cid]
            r = res[cid]
            lines.append("- %s (%s p%s): %s; located=%s zone_valid=%s supported=%s consistent=%s cross_read=%s; models %s" % (
                cid, c["paper_id"], c["locators"][0]["page"], c["review_state"], r["located"], r["zone_valid"], r["supported"], r["consistent"], r["cross_read"],
                ",".join(c["produced_by"]["models"])))
        rwlib.write_text_atomic(project / "drafts" / (a.draft + ".manifest.md"), "\n".join(lines) + "\n")
        print("wrote drafts/%s.manifest.md" % a.draft)
    return 0


def cmd_dossier(project, a):
    d = rwlib.read_json(project / "dossiers" / (a.dossier + ".json"))
    if not d:
        rwlib.die("No such dossier")
    cards, res = card_states(project)
    bad = 0
    ids = d["evidence_for"] + d["evidence_against"]
    for cid in ids:
        if cid not in cards:
            print("BLOCKS %s does not exist" % cid)
            bad += 1
            continue
        st, why = standing(cards[cid], res[cid])
        if st not in ALLOWED["dossier"]:
            print("BLOCKS %s standing %s (%s)" % (cid, st, why))
            bad += 1
    verified = sum(1 for c in ids if c in cards and cards[c]["review_state"] == "human_verified")
    print("dossier %s: %d card(s), %d verified by a person; %d blocking" % (a.dossier, len(ids), verified, bad))
    return 1 if bad else 0


def cmd_table(project, a):
    print("""Card standing  | answer             | dossier              | export
none           | no                 | no                   | no
context_only   | context only       | context only         | no
disputed_only  | no                 | yes, under disputed  | no
needs_review   | yes, marked        | yes, marked          | no, until a person resolves it
located_only   | yes, marked        | yes, marked          | no, until supported/cross_read pass
machine_checked| yes                | yes                  | yes, counted unverified (results need cross_read)
human_verified | yes                | yes                  | yes""")
    return 0


def main():
    rwlib.setup_console()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("cards")
    p.add_argument("cards", nargs="+")
    p.add_argument("--context", choices=["answer", "dossier", "export"], default="answer")
    p = sub.add_parser("draft")
    p.add_argument("draft")
    p.add_argument("--context", choices=["answer", "dossier", "export"], default="export")
    p.add_argument("--manifest", action="store_true")
    p = sub.add_parser("dossier")
    p.add_argument("dossier")
    sub.add_parser("table")
    a = ap.parse_args()
    if a.cmd == "table":
        return cmd_table(None, a)
    project = rwlib.find_project(explicit=a.project)
    return {"cards": cmd_cards, "draft": cmd_draft, "dossier": cmd_dossier}[a.cmd](project, a)


if __name__ == "__main__":
    sys.exit(main())
