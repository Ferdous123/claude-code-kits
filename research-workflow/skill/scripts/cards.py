#!/usr/bin/env python3
"""Create, revise, reject and human-verify evidence cards, so nobody hand-edits cards.jsonl.

Usage:
  cards.py add --paper P0001 --page 3 --quote "exact text" --type result --statement "..."
               [--number 0.82 --unit m/s --metric RMSE --dataset "test set"]
               [--attribution own|cited_work|disputed_view] [--attributed-to "ref"]
               [--wording asserted|hedged|speculative|not_stated] [--origin extracted|analyst|user]
               [--role extract] [--models a,b] [--field F]
      The quote is located on the page FIRST. If it is not found the card is refused (not written).
      Zone, block ids and box are filled from the parsed page. The checks are run and recorded.
  cards.py not-stated --paper P0001 --field dataset --type dataset
      Record that the paper does not state something. Never evidence.
  cards.py revise CARD --set statement="..." [--set number=0.82 ...] [--by "Person" --words "their words"]
      Writes a new version. With --by (a person's correction) the new version has origin user, and a
      correction, a regression case and an exemplar are written too. Without --by it is a machine revision.
      A revised card is no longer human-verified.
  cards.py reject CARD --reason "..."     mark a card rejected (never cited).
  cards.py verify CARD --by "Person" --words "what they said" --viewed-source
      Set human_verified. ONLY when the user, in this conversation, said they looked at the source passage.
      Refused unless `located` currently passed. --by must be a person's name, not a model.

Fields settable with --set: statement, type, attribution, attributed_to, wording_strength, number, unit, metric,
dataset, quote, page, note.
Writes only inside the project folder.
"""
import argparse
import copy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import rwlib  # noqa: E402
import check_cards  # noqa: E402
from validate import check_value, load_schema, PERSON_BLOCKLIST  # noqa: E402

TYPES = ["claim", "result", "method_step", "dataset", "assumption", "limitation", "definition", "future_work", "attributed_opinion"]
SETTABLE = ["statement", "type", "attribution", "attributed_to", "wording_strength", "number", "unit", "metric", "dataset", "quote", "page", "note"]


def validate_card(card):
    """Return error strings for a card against the schema."""
    return check_value(card, load_schema("card"))


def write_version(project, card, carry_from=None):
    """Append a card version, run the checks on it, optionally carry supported/cross_read results forward."""
    errs = validate_card(card)
    if errs:
        rwlib.die("Card is not valid:\n  " + "\n  ".join(errs))
    rwlib.append_jsonl(project / "cards" / "cards.jsonl", card)
    res = check_cards.apply_checks(project, card)
    if carry_from:
        checks = rwlib.current_checks(project)
        for name in ("supported", "cross_read"):
            prev = checks.get((card["card_id"], carry_from, name))
            if prev:
                existing = [r.get("check_id") for r in rwlib.read_jsonl(project / "checks" / "checks.jsonl")]
                top = max([int(x[1:]) for x in existing if x and x[1:].isdigit()] or [0])
                new = dict(prev)
                new.update({"check_id": "K%05d" % (top + 1), "card_version": card["version"], "method": prev["method"] + "+carried_over",
                            "detail": ("carried over unchanged content from v%d; %s" % (carry_from, prev.get("detail", "")))[:600], "date": rwlib.now_iso()})
                rwlib.append_jsonl(project / "checks" / "checks.jsonl", new)
    return res


def create_card(project, paper, page, quote, ctype, statement, value, attribution="own", attributed_to=None, wording="asserted",
                origin="extracted", role="extract", models=None, note="", field=None, run_id=None):
    """Create version 1 of a card. Returns (card, results) or (None, reason) when the quote is not located."""
    reg = rwlib.load_register(project)
    r = check_cards.locate_quote(project, paper, page, quote)
    if r["result"] != "passed":
        return None, r["detail"]
    cards = rwlib.current_cards(project)
    cid = rwlib.next_id("C", list(cards.keys()))
    card = {
        "card_id": cid, "version": 1, "paper_id": paper, "type": ctype, "field": field, "stated": True, "statement": statement,
        "value": value, "locators": [{"page": page, "quote": quote, "zone": r["zone"], "block_ids": r["block_ids"], "bbox": r["bbox"]}],
        "origin": origin, "attribution": attribution, "attributed_to": attributed_to,
        "epistemic_class": "researcher_interpreted" if origin == "user" else "machine_extracted",
        "wording_strength": wording, "abstract_only": reg[paper]["state"] == "abstract_only",
        "produced_by": {"role": role, "models": models or ["claude"], "run_id": run_id},
        "review_state": "unreviewed", "reviewed_by": None, "reviewed_on": None, "supersedes_version": None,
        "created": rwlib.now_iso(), "note": note or "",
    }
    return card, write_version(project, card)


def cmd_add(project, a):
    """Add a card, refusing it if the quote cannot be located."""
    reg = rwlib.load_register(project)
    if a.paper not in reg:
        rwlib.die("Unknown paper %s" % a.paper)
    if a.type == "result" and not a.number:
        rwlib.die("A result card needs --number (the value exactly as printed)")
    value = {"number": a.number, "unit": a.unit, "metric": a.metric, "dataset": a.dataset} if a.number else None
    card, res = create_card(project, a.paper, a.page, a.quote, a.type, a.statement, value, a.attribution, a.attributed_to, a.wording,
                            a.origin, a.role, [m for m in a.models.split(",") if m], a.note, a.field)
    if card is None:
        print("REFUSED: %s. The card was not written: a card whose quoted text is not on the page cannot exist." % res)
        return 1
    print("added %s (%s, paper %s page %d): located=%s zone_valid=%s consistent=%s" % (
        card["card_id"], a.type, a.paper, a.page, res["located"], res["zone_valid"], res["consistent"]))
    return 0


def cmd_not_stated(project, a):
    """Record an absence."""
    reg = rwlib.load_register(project)
    if a.paper not in reg:
        rwlib.die("Unknown paper %s" % a.paper)
    cards = rwlib.current_cards(project)
    for c in cards.values():
        if c["paper_id"] == a.paper and c.get("field") == a.field and not c.get("stated"):
            print("already recorded as %s" % c["card_id"])
            return 0
    cid = rwlib.next_id("C", list(cards.keys()))
    card = {
        "card_id": cid, "version": 1, "paper_id": a.paper, "type": a.type, "field": a.field, "stated": False, "statement": "not stated",
        "value": None, "locators": [], "origin": a.origin, "attribution": "own", "attributed_to": None,
        "epistemic_class": "machine_extracted", "wording_strength": "not_stated", "abstract_only": reg[a.paper]["state"] == "abstract_only",
        "produced_by": {"role": a.role, "models": [m for m in a.models.split(",") if m], "run_id": None},
        "review_state": "unreviewed", "reviewed_by": None, "reviewed_on": None, "supersedes_version": None,
        "created": rwlib.now_iso(), "note": "the paper does not state its %s (read in full)" % a.field,
    }
    rwlib.append_jsonl(project / "cards" / "cards.jsonl", card)
    print("recorded %s: %s not stated in %s" % (cid, a.field, a.paper))
    return 0


def parse_sets(items):
    out = {}
    for it in items or []:
        if "=" not in it:
            rwlib.die("--set needs key=value, got %r" % it)
        k, v = it.split("=", 1)
        if k not in SETTABLE:
            rwlib.die("Cannot set %r. Allowed: %s" % (k, ", ".join(SETTABLE)))
        out[k] = v
    return out


def cmd_revise(project, a):
    """Write a new version of a card, with the correction trail when a person made the change."""
    cards = rwlib.current_cards(project)
    if a.card not in cards:
        rwlib.die("Unknown card %s" % a.card)
    old = cards[a.card]
    sets = parse_sets(a.set)
    if not sets:
        rwlib.die("Nothing to change: give --set key=value")
    new = copy.deepcopy(old)
    new["version"] = old["version"] + 1
    new["supersedes_version"] = old["version"]
    new["created"] = rwlib.now_iso()
    new["review_state"], new["reviewed_by"], new["reviewed_on"] = "unreviewed", None, None
    for k in ("statement", "type", "attribution", "attributed_to", "wording_strength", "note"):
        if k in sets:
            new[k] = sets[k]
    for k in ("number", "unit", "metric", "dataset"):
        if k in sets:
            val = dict(new.get("value") or {"number": "", "unit": None, "metric": None, "dataset": None})
            val[k] = sets[k]
            new["value"] = val
    if "quote" in sets or "page" in sets:
        loc0 = dict(new["locators"][0]) if new["locators"] else {"page": 1, "quote": ""}
        page = int(sets.get("page", loc0["page"]))
        quote = sets.get("quote", loc0["quote"])
        r = check_cards.locate_quote(project, new["paper_id"], page, quote)
        if r["result"] != "passed":
            print("REFUSED: %s. Nothing written." % r["detail"])
            return 1
        new["locators"] = [{"page": page, "quote": quote, "zone": r["zone"], "block_ids": r["block_ids"], "bbox": r["bbox"]}] + new["locators"][1:]
    content_changed = any(k in sets for k in ("statement", "type", "number", "unit", "metric", "dataset", "quote", "page", "attribution"))
    if a.by:
        if a.by.strip().lower() in PERSON_BLOCKLIST:
            rwlib.die("--by must be the name of a person")
        new["origin"], new["epistemic_class"] = "user", "researcher_interpreted"
        new["produced_by"] = {"role": "correction", "models": ["person"], "run_id": None}
    res = write_version(project, new, carry_from=None if content_changed else old["version"])
    print("%s v%d written: located=%s zone_valid=%s consistent=%s" % (a.card, new["version"], res["located"], res["zone_valid"], res["consistent"]))
    if a.by:
        corr_all = rwlib.read_jsonl(project / "corrections" / "corrections.jsonl")
        reg_all = rwlib.read_jsonl(project / "regression" / "cases.jsonl")
        ex_all = rwlib.read_jsonl(project / "exemplars" / "exemplars.jsonl")
        xid = rwlib.next_id("X", [c["correction_id"] for c in corr_all])
        before = "; ".join("%s=%s" % (k, old.get(k) if k in old else (old.get("value") or {}).get(k)) for k in sets)
        after = "; ".join("%s=%s" % (k, v) for k, v in sets.items())
        loc0 = new["locators"][0] if new["locators"] else {"page": None, "quote": None}
        rwlib.append_jsonl(project / "corrections" / "corrections.jsonl", {
            "correction_id": xid, "card_id": a.card, "from_version": old["version"], "to_version": new["version"],
            "field": ",".join(sets), "before": before, "after": after, "source_page": loc0["page"], "source_quote": loc0["quote"],
            "by": a.by, "words": a.words or "", "date": rwlib.today(), "disputed": False})
        role = old["produced_by"]["role"]
        role = role if role in ("extract", "judge_support", "navsum") else "extract"
        gid = rwlib.next_id("RG", [c["case_id"] for c in reg_all])
        rwlib.append_jsonl(project / "regression" / "cases.jsonl", {
            "case_id": gid, "role": role, "source": xid, "paper_id": old["paper_id"],
            "input_ref": "%s page %s card %s" % (old["paper_id"], loc0["page"], a.card), "wrong_output": before[:500], "right_output": after[:500],
            "created": rwlib.today(), "suspended": False, "last_run_passed": None})
        eid = rwlib.next_id("EX", [c["exemplar_id"] for c in ex_all])
        rwlib.append_jsonl(project / "exemplars" / "exemplars.jsonl", {
            "exemplar_id": eid, "role": role, "case_id": gid, "paper_id": old["paper_id"], "input_passage": (loc0["quote"] or "")[:600],
            "wrong_output": before[:300], "right_output": after[:300], "created": rwlib.today()})
        print("correction %s, regression case %s and exemplar %s recorded. The user's version stands against any later model output." % (xid, gid, eid))
    return 0


def cmd_reject(project, a):
    """Mark a card rejected."""
    cards = rwlib.current_cards(project)
    if a.card not in cards:
        rwlib.die("Unknown card %s" % a.card)
    old = cards[a.card]
    new = copy.deepcopy(old)
    new.update({"version": old["version"] + 1, "supersedes_version": old["version"], "review_state": "rejected",
                "reviewed_by": None, "reviewed_on": None, "created": rwlib.now_iso(), "note": ("rejected: " + a.reason)[:500]})
    write_version(project, new, carry_from=old["version"])
    print("%s rejected (v%d). Rejected cards are never cited." % (a.card, new["version"]))
    return 0


def cmd_verify(project, a):
    """Set human_verified on a card, only on a person's word."""
    if not a.viewed_source:
        rwlib.die("Refused: --viewed-source is required. Only a person who looked at the source passage may verify a card.")
    if a.by.strip().lower() in PERSON_BLOCKLIST or len(a.by.strip()) < 2:
        rwlib.die("Refused: --by must be the name of a person, not a model or a script.")
    if not a.words.strip():
        rwlib.die("Refused: --words must quote what the person said (it is stored in DECISIONS context).")
    cards = rwlib.current_cards(project)
    if a.card not in cards:
        rwlib.die("Unknown card %s" % a.card)
    old = cards[a.card]
    state = rwlib.check_result(rwlib.current_checks(project), old)
    if not old.get("stated"):
        rwlib.die("A not-stated record cannot be verified as evidence.")
    if state["located"] != "passed":
        rwlib.die("Refused: `located` is %s for %s. Fix or reject the card first." % (state["located"], a.card))
    new = copy.deepcopy(old)
    new.update({"version": old["version"] + 1, "supersedes_version": old["version"], "review_state": "human_verified",
                "reviewed_by": a.by.strip(), "reviewed_on": rwlib.today(), "created": rwlib.now_iso(),
                "note": ((old.get("note") or "") + " | verified by %s: %s" % (a.by.strip(), a.words.strip()))[:500]})
    write_version(project, new, carry_from=old["version"])
    print("%s is now human_verified by %s (v%d)." % (a.card, a.by.strip(), new["version"]))
    return 0


def main():
    rwlib.setup_console()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("add")
    p.add_argument("--paper", required=True)
    p.add_argument("--page", type=int, required=True)
    p.add_argument("--quote", required=True)
    p.add_argument("--type", required=True, choices=TYPES)
    p.add_argument("--statement", required=True)
    p.add_argument("--number")
    p.add_argument("--unit")
    p.add_argument("--metric")
    p.add_argument("--dataset")
    p.add_argument("--attribution", default="own", choices=["own", "cited_work", "disputed_view"])
    p.add_argument("--attributed-to", dest="attributed_to")
    p.add_argument("--wording", default="asserted", choices=["asserted", "hedged", "speculative", "not_stated"])
    p.add_argument("--origin", default="extracted", choices=["extracted", "analyst", "user"])
    p.add_argument("--role", default="extract")
    p.add_argument("--models", default="claude")
    p.add_argument("--field")
    p.add_argument("--note")
    p = sub.add_parser("not-stated")
    p.add_argument("--paper", required=True)
    p.add_argument("--field", required=True)
    p.add_argument("--type", required=True, choices=TYPES)
    p.add_argument("--origin", default="extracted", choices=["extracted", "analyst", "user"])
    p.add_argument("--role", default="extract")
    p.add_argument("--models", default="claude")
    p = sub.add_parser("revise")
    p.add_argument("card")
    p.add_argument("--set", action="append")
    p.add_argument("--by")
    p.add_argument("--words")
    p = sub.add_parser("reject")
    p.add_argument("card")
    p.add_argument("--reason", required=True)
    p = sub.add_parser("verify")
    p.add_argument("card")
    p.add_argument("--by", required=True)
    p.add_argument("--words", required=True)
    p.add_argument("--viewed-source", action="store_true")
    a = ap.parse_args()
    project = rwlib.find_project(explicit=a.project)
    return {"add": cmd_add, "not-stated": cmd_not_stated, "revise": cmd_revise, "reject": cmd_reject, "verify": cmd_verify}[a.cmd](project, a)


if __name__ == "__main__":
    sys.exit(main())
