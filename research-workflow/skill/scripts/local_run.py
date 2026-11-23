#!/usr/bin/env python3
"""Run a local model on one role and write PROPOSALS (candidates), never cards.

Usage:
  local_run.py extract --paper P0001 --pass A|B [--model TAG] [--pages 1-5] [--types claim,result,...]
                       [--no-exemplars] [--force-unqualified]
  local_run.py support --pass A|B [--model TAG] [--cards C0001 ...] [--force-unqualified]
  local_run.py navsum  --paper P0001 [--model TAG] [--force-unqualified]

What it does
  extract  one model call per page (or per part of a long page). Each proposed card is immediately run through
           the `located` check (exact quote on the stated page). Output: candidates/<paper>/<pass>.jsonl
  support  for each card whose `located` passed: asks the model whether the quote entails the statement.
           Output: candidates/support/<pass>.jsonl
  navsum   writes parsed/<paper>/nav.md, a short navigation note, headed NAVIGATION ONLY. Numbers in it that
           are not in the source text are listed as a warning in the file. Never evidence, never cited.

The model comes from roles.json (primary for pass A, secondary for pass B) unless --model is given. A role that
has no model that passed its bake-off refuses to run, unless --force-unqualified (used only by the bake-off).
Every call is logged to logs/model_calls.jsonl. The run can be stopped at any moment and started again: pages
already finished (candidates/<paper>/<pass>.done.json) are skipped. The model is unloaded at the end so the
next role can use the memory.
Uses only the local model server (see ollama_api.py). Nothing leaves the machine.
"""
import argparse
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import rwlib  # noqa: E402
import ollama_api  # noqa: E402
import check_cards  # noqa: E402
from validate import check_value  # noqa: E402

PROMPTS = rwlib.SKILL_DIR / "templates" / "prompts"
CARD_TYPES = ["claim", "result", "method_step", "dataset", "assumption", "limitation", "definition", "future_work", "attributed_opinion"]

EXTRACT_SCHEMA = {
    "type": "object", "required": ["cards"], "additionalProperties": False,
    "properties": {"cards": {"type": "array", "items": {
        "type": "object", "additionalProperties": False,
        "required": ["type", "statement", "quote", "number", "unit", "metric", "dataset", "attribution", "attributed_to", "wording_strength"],
        "properties": {
            "type": {"type": "string", "enum": CARD_TYPES}, "statement": {"type": "string"}, "quote": {"type": "string"},
            "number": {"type": "string"}, "unit": {"type": "string"}, "metric": {"type": "string"}, "dataset": {"type": "string"},
            "attribution": {"type": "string", "enum": ["own", "cited_work", "disputed_view"]}, "attributed_to": {"type": "string"},
            "wording_strength": {"type": "string", "enum": ["asserted", "hedged", "speculative"]}}}}},
}
SUPPORT_SCHEMA = {
    "type": "object", "required": ["label", "reason"], "additionalProperties": False,
    "properties": {"label": {"type": "string", "enum": ["entails", "partly", "does_not"]}, "reason": {"type": "string"}},
}
NAVSUM_SCHEMA = {
    "type": "object", "required": ["summary", "topics"], "additionalProperties": False,
    "properties": {"summary": {"type": "string"}, "topics": {"type": "array", "items": {"type": "string"}}},
}


def plan_limits(project):
    """num_ctx and chunk size from machine/plan.json, with safe defaults."""
    plan = rwlib.read_json(project / "machine" / "plan.json", {}) or {}
    s = plan.get("settings", {})
    return int(s.get("num_ctx", 4096)), int(s.get("extract_chunk_chars", 6000))


def pick_model(project, role, pass_name, explicit, force):
    """Resolve the model tag for a pass, enforcing the bake-off rule."""
    if explicit:
        if not force:
            roles = (rwlib.read_json(project / "roles.json", {}) or {}).get("roles", {})
            ent = roles.get(role, {})
            if ent.get("assigned") != "local" or explicit not in (ent.get("primary"), ent.get("secondary")) or ent.get("stale"):
                rwlib.die("Model %s has not passed the bake-off for role %s on this machine (or the assignment is stale). "
                          "Run the bake-off, or leave this work to Claude." % (explicit, role))
        return explicit
    roles = (rwlib.read_json(project / "roles.json", {}) or {}).get("roles", {})
    ent = roles.get(role, {})
    if ent.get("assigned") != "local" or ent.get("stale"):
        rwlib.die("Role %s is not assigned to a local model (assigned: %s%s). It stays on Claude until a model passes the bake-off."
                  % (role, ent.get("assigned"), ", stale" if ent.get("stale") else ""))
    tag = ent.get("primary") if pass_name == "A" else ent.get("secondary")
    if not tag:
        rwlib.die("Role %s has no %s model for pass %s." % (role, "primary" if pass_name == "A" else "secondary", pass_name))
    return tag


def log_call(project, role, model, run_id, input_ref, prompt, res, parse_status, call_n):
    """Append one model-call record."""
    rwlib.append_jsonl(project / "logs" / "model_calls.jsonl", {
        "call_id": "%s-%04d" % (run_id, call_n), "date": rwlib.now_iso(), "role": role, "model": model, "run_id": run_id,
        "input_ref": input_ref, "prompt_sha256": rwlib.sha256_text(prompt), "params": res.get("params", {}),
        "output_raw": res.get("content", "")[:20000], "parse_status": parse_status, "finish_reason": res.get("done_reason"),
        "tokens_in": res.get("tokens_in"), "tokens_out": res.get("tokens_out"), "seconds": res.get("seconds", 0.0)})


def call_json(model, prompt, schema, num_ctx, num_predict, project, role, run_id, input_ref, counter):
    """Call the model, parse JSON, retry once with another seed. Returns (obj or None, parse_status)."""
    import json
    status, obj = "error", None
    for attempt, seed in enumerate((0, 1)):
        counter[0] += 1
        try:
            res = ollama_api.chat(model, "", prompt, schema=schema, num_ctx=num_ctx, num_predict=num_predict, seed=seed)
        except ollama_api.OllamaError as exc:
            log_call(project, role, model, run_id, input_ref, prompt, {"content": str(exc), "seconds": 0.0}, "error", counter[0])
            rwlib.die("Local model call failed: %s" % exc)
        try:
            obj = json.loads(res["content"])
        except ValueError:
            log_call(project, role, model, run_id, input_ref, prompt, res, "invalid_json", counter[0])
            status = "invalid_json"
            continue
        errs = check_value(obj, schema)
        if errs:
            log_call(project, role, model, run_id, input_ref, prompt, res, "schema_invalid", counter[0])
            status, obj = "schema_invalid", None
            continue
        log_call(project, role, model, run_id, input_ref, prompt, res, "ok", counter[0])
        return obj, "ok" if attempt == 0 else "ok_after_retry"
    return None, status


def exemplar_text(project, role, text, paper_id, page):
    """Up to 3 stored exemplars with the most word overlap, as delimited data. Same paper and page are excluded."""
    ex = rwlib.read_jsonl(project / "exemplars" / "exemplars.jsonl")
    words = set(re.findall(r"\w+", text.lower()))
    scored = []
    for e in ex:
        if e.get("role") != role or (e.get("paper_id") == paper_id):
            continue
        ew = set(re.findall(r"\w+", e["input_passage"].lower()))
        if ew:
            scored.append((len(ew & words) / len(ew), e))
    scored.sort(key=lambda x: -x[0])
    if not scored or scored[0][0] <= 0.1:
        return "(none)"
    out = []
    for _, e in scored[:3]:
        out.append('- passage: """%s""" | wrong: """%s""" | right: """%s"""' % (e["input_passage"][:300], e["wrong_output"][:150], e["right_output"][:150]))
    return "\n".join(out)


def split_page(page, limit):
    """Split a page's text into parts no longer than `limit`, at block boundaries."""
    text = page["text"]
    if len(text) <= limit:
        return [text]
    parts, cur_start = [], 0
    for b in page["blocks"]:
        if b["end"] - cur_start > limit and b["start"] > cur_start:
            parts.append(text[cur_start:b["start"]].strip())
            cur_start = b["start"]
    parts.append(text[cur_start:].strip())
    return [p for p in parts if p]


def parse_pages(spec, n):
    """'1-5,8' -> [1,2,3,4,5,8]."""
    if not spec:
        return list(range(1, n + 1))
    out = []
    for part in spec.split(","):
        if "-" in part:
            a, b = part.split("-", 1)
            out += list(range(int(a), int(b) + 1))
        else:
            out.append(int(part))
    return [p for p in out if 1 <= p <= n]


def cmd_extract(project, a):
    """Extraction pass for one paper."""
    reg = rwlib.load_register(project)
    if a.paper not in reg:
        rwlib.die("Unknown paper %s" % a.paper)
    meta = rwlib.read_json(project / "parsed" / a.paper / "meta.json")
    if not meta:
        rwlib.die("%s is not parsed yet. Run extract_pages.py first." % a.paper)
    if reg[a.paper]["state"] != "full_text":
        rwlib.die("%s has state %s; only full_text papers are extracted." % (a.paper, reg[a.paper]["state"]))
    model = pick_model(project, "extract", a.pass_name, a.model, a.force_unqualified)
    num_ctx, chunk = plan_limits(project)
    types = [t for t in (a.types.split(",") if a.types else CARD_TYPES) if t in CARD_TYPES]
    tmpl = (PROMPTS / ("extract_a.md" if a.pass_name == "A" else "extract_b.md")).read_text(encoding="utf-8")
    out_path = project / "candidates" / a.paper / (a.pass_name + ".jsonl")
    done_path = project / "candidates" / a.paper / (a.pass_name + ".done.json")
    done = rwlib.read_json(done_path, {"model": model, "pages": {}})
    if done.get("model") != model and done["pages"]:
        rwlib.die("This pass was started with model %s; a pass must not change model midway. Use the other pass name." % done.get("model"))
    done["model"] = model
    run_id = "R" + rwlib.now_iso()[:19].replace("-", "").replace(":", "").replace("T", "-")
    counter = [0]
    stats = {"pages": 0, "skipped": 0, "calls_ok": 0, "calls": 0, "cands": 0, "located": 0}
    existing = rwlib.read_jsonl(out_path)
    for pno in parse_pages(a.pages, meta["pages"]):
        if str(pno) in done["pages"]:
            stats["skipped"] += 1
            continue
        page = rwlib.load_page(project, a.paper, pno)
        if page is None or page["chars"] < 40:
            done["pages"][str(pno)] = {"parse": "skipped_no_text", "cards": 0}
            rwlib.write_json(done_path, done)
            continue
        cands_page = []
        page_status = "ok"
        for part_i, part in enumerate(split_page(page, chunk), 1):
            prompt = (tmpl.replace("{{types}}", ", ".join(types)).replace("{{page}}", str(pno)).replace("{{paper}}", a.paper)
                      .replace("{{exemplars}}", "(none)" if a.no_exemplars else exemplar_text(project, "extract", part, a.paper, pno))
                      .replace("{{text}}", part))
            obj, status = call_json(model, prompt, EXTRACT_SCHEMA, num_ctx, 1500, project, "extract", run_id,
                                    "%s page %d part %d sha=%s" % (a.paper, pno, part_i, page["text_sha256"][:12]), counter)
            stats["calls"] += 1
            if status.startswith("ok"):
                stats["calls_ok"] += 1
            else:
                page_status = status
                continue
            for k, c in enumerate(obj["cards"], 1):
                prop = {"type": c["type"], "field": None, "stated": True, "statement": c["statement"], "quote": c["quote"],
                        "value": ({"number": c["number"], "unit": c["unit"] or None, "metric": c["metric"] or None, "dataset": c["dataset"] or None}
                                  if c["number"].strip() else None),
                        "attribution": c["attribution"], "attributed_to": c["attributed_to"] or None, "wording_strength": c["wording_strength"]}
                if prop["type"] not in types:
                    continue
                r = check_cards.locate_quote(project, a.paper, pno, c["quote"])
                located = {"result": r["result"], "method": r.get("method", "none"), "start": r.get("start"), "end": r.get("end"),
                           "block_ids": r.get("block_ids", []), "zone": r.get("zone", "unknown")}
                cands_page.append({"cand_id": "%s-%d-%s-%d" % (a.paper, pno, a.pass_name, len(existing) + len(cands_page) + 1), "run_id": run_id,
                                   "kind": "card", "pass_name": a.pass_name, "paper_id": a.paper, "page": pno, "model": model,
                                   "date": rwlib.now_iso(), "proposed": prop, "schema_valid": True, "located": located})
        for c in cands_page:
            rwlib.append_jsonl(out_path, c)
        existing += cands_page
        stats["cands"] += len(cands_page)
        stats["located"] += sum(1 for c in cands_page if c["located"]["result"] == "passed")
        done["pages"][str(pno)] = {"parse": page_status, "cards": len(cands_page)}
        rwlib.write_json(done_path, done)   # checkpoint after every page
        stats["pages"] += 1
    ollama_api.unload(model)
    print("extract pass %s with %s on %s: %d page(s) done, %d already done; calls with valid output %d of %d; "
          "candidates %d, quote located %d of %d" % (a.pass_name, model, a.paper, stats["pages"], stats["skipped"],
                                                      stats["calls_ok"], stats["calls"], stats["cands"], stats["located"], stats["cands"]))
    print("These are proposals only. Next: agree.py merge --paper %s" % a.paper)
    return 0


def cmd_support(project, a):
    """Support pass over cards whose quote is located."""
    cards = rwlib.current_cards(project)
    checks = rwlib.current_checks(project)
    model = pick_model(project, "judge_support", a.pass_name, a.model, a.force_unqualified)
    num_ctx, _ = plan_limits(project)
    tmpl = (PROMPTS / "support.md").read_text(encoding="utf-8")
    out_path = project / "candidates" / "support" / (a.pass_name + ".jsonl")
    have = {(c["proposed"].get("card_id"), c["proposed"].get("card_version")) for c in rwlib.read_jsonl(out_path)}
    chosen = a.cards or sorted(cards)
    run_id = "R" + rwlib.now_iso()[:19].replace("-", "").replace(":", "").replace("T", "-")
    counter = [0]
    n = ok = 0
    for cid in chosen:
        c = cards.get(cid)
        if not c or not c.get("stated") or c["review_state"] == "rejected":
            continue
        if rwlib.check_result(checks, c)["located"] != "passed" or (cid, c["version"]) in have:
            continue
        loc = c["locators"][0]
        page = rwlib.load_page(project, c["paper_id"], loc["page"])
        pos = page["text"].find(loc["quote"]) if page else -1
        ctx = page["text"][max(0, pos - 400): pos + len(loc["quote"]) + 400] if pos >= 0 else ""
        prompt = tmpl.replace("{{statement}}", c["statement"]).replace("{{quote}}", loc["quote"]).replace("{{context}}", ctx or "(not available)")
        obj, status = call_json(model, prompt, SUPPORT_SCHEMA, num_ctx, 200, project, "judge_support", run_id, "%s v%d" % (cid, c["version"]), counter)
        n += 1
        if obj is None:
            continue
        ok += 1
        rwlib.append_jsonl(out_path, {
            "cand_id": "%s-v%d-%s" % (cid, c["version"], a.pass_name), "run_id": run_id, "kind": "support", "pass_name": a.pass_name,
            "paper_id": c["paper_id"], "page": loc["page"], "model": model, "date": rwlib.now_iso(),
            "proposed": {"card_id": cid, "card_version": c["version"], "label": obj["label"], "reason": obj["reason"][:300]}, "schema_valid": True})
    ollama_api.unload(model)
    print("support pass %s with %s: %d card(s) judged, valid outputs %d of %d" % (a.pass_name, model, n, ok, n))
    print("Next: agree.py support (needs both passes).")
    return 0


def cmd_navsum(project, a):
    """Navigation note for one paper."""
    meta = rwlib.read_json(project / "parsed" / a.paper / "meta.json")
    if not meta:
        rwlib.die("%s is not parsed yet." % a.paper)
    model = pick_model(project, "navsum", "A", a.model, a.force_unqualified)
    num_ctx, chunk = plan_limits(project)
    text = ""
    for p in range(1, meta["pages"] + 1):
        pg = rwlib.load_page(project, a.paper, p)
        blocks = [b for b in pg["blocks"] if b["zone"] != "bibliography"]
        text += "\n\n".join(pg["text"][b["start"]:b["end"]] for b in blocks) + "\n\n"
        if len(text) >= chunk:
            break
    text = text[:chunk]
    tmpl = (PROMPTS / "navsum.md").read_text(encoding="utf-8")
    run_id = "R" + rwlib.now_iso()[:19].replace("-", "").replace(":", "").replace("T", "-")
    obj, status = call_json(model, tmpl.replace("{{paper}}", a.paper).replace("{{text}}", text), NAVSUM_SCHEMA, num_ctx, 400,
                            project, "navsum", run_id, "%s first %d chars" % (a.paper, len(text)), [0])
    ollama_api.unload(model)
    if obj is None:
        print("no usable output (%s); nothing written" % status)
        return 1
    nums = set(re.findall(r"\d+(?:[.,]\d+)*", obj["summary"]))
    src = set(re.findall(r"\d+(?:[.,]\d+)*", text))
    bad = sorted(nums - src)
    body = "NAVIGATION ONLY. Machine-written (%s, %s). Never evidence, never cited, may be wrong.\n\n%s\n\nTopics: %s\n" % (
        model, rwlib.today(), obj["summary"], "; ".join(obj["topics"]))
    if bad:
        body += "\nWARNING: numbers in the summary that are not in the source text: %s. Treat this note as unreliable.\n" % ", ".join(bad)
    rwlib.write_text_atomic(project / "parsed" / a.paper / "nav.md", body)
    print("wrote parsed/%s/nav.md%s" % (a.paper, " (WARNING: %d number(s) not in the source)" % len(bad) if bad else ""))
    return 0


def main():
    rwlib.setup_console()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("extract", "support", "navsum"):
        p = sub.add_parser(name)
        if name != "support":
            p.add_argument("--paper", required=True)
        if name != "navsum":
            p.add_argument("--pass", dest="pass_name", required=True, choices=["A", "B"])
        p.add_argument("--model")
        p.add_argument("--force-unqualified", action="store_true", help="bake-off use only")
        if name == "extract":
            p.add_argument("--pages")
            p.add_argument("--types")
            p.add_argument("--no-exemplars", action="store_true")
        if name == "support":
            p.add_argument("--cards", nargs="*")
    a = ap.parse_args()
    project = rwlib.find_project(explicit=a.project)
    return {"extract": cmd_extract, "support": cmd_support, "navsum": cmd_navsum}[a.cmd](project, a)


if __name__ == "__main__":
    sys.exit(main())
