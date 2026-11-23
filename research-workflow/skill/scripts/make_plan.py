#!/usr/bin/env python3
"""Derive the machine PLAN from the detected facts by explicit rules (R01 to R19), and render MACHINE.md.

Usage:
  make_plan.py [--project DIR]                 re-derive the plan from machine/facts.json and rewrite MACHINE.md
  make_plan.py --facts-json FILE --stdout      print the plan for facts in a file (no project needed)

The rules are listed in MACHINE.md (the rules are printed there) in the same order and with the same ids. The plan is never
edited by hand: change the facts (or tell detect_machine.py a fact) and re-derive. Earlier MACHINE.md and plan
files are kept in machine/history/ by detect_machine.py.
Writes only inside the project folder.
"""
import argparse
import math
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import rwlib  # noqa: E402

USER_NOTES_MARK = "## USER NOTES (kept across updates; the only part of this file you may edit by hand)"
MIN_WIN_DRIVER = (551, 61)
FAMILY_NOTE = "a second pass must use a different family"

FACT_ROWS = [
    ("os_name", "Operating system"), ("os_version", "OS version"), ("arch", "Architecture"), ("cpu_model", "CPU"),
    ("cpu_cores_physical", "CPU cores (physical)"), ("cpu_threads", "CPU threads"), ("ram_gb", "RAM (GB)"),
    ("gpu_name", "GPU"), ("gpu_vendor", "GPU vendor"), ("gpu_vram_gb", "GPU video memory (GB)"), ("gpu_driver", "GPU driver"),
    ("gpu_cuda", "CUDA (highest the driver supports)"), ("gpu_compute_cap", "GPU compute capability"),
    ("free_disk_gb", "Free disk (GB)"), ("python_version", "Python"), ("python_venv_ok", "Python can make a private environment"),
    ("pymupdf_ok", "PDF reader (PyMuPDF) usable"), ("runner_name", "Local model runner"), ("runner_version", "Runner version"),
    ("runner_running", "Runner server running"), ("runner_models", "Models already installed"), ("other_runners", "Other runners found"),
    ("net_pypi", "Internet: package index"), ("net_ollama", "Internet: runner's model library"), ("net_anthropic", "Internet: Claude service"),
]


def val(facts, key):
    """Fact value, or None when the fact is unknown."""
    f = facts.get(key)
    if not f or str(f.get("status", "")).startswith("unknown") or f.get("value") == "unknown":
        return None
    return f["value"]


def parse_version(s):
    nums = re.findall(r"\d+", str(s))
    return tuple(int(x) for x in nums[:2]) if nums else None


def derive_plan(facts, cand, roles=None):
    """Return the plan dict. Pure function of the facts and the candidates file."""
    rules, warnings = [], []

    def fire(rid, text):
        rules.append({"rule": rid, "result": text})

    ram = val(facts, "ram_gb")
    cores = val(facts, "cpu_cores_physical")
    disk = val(facts, "free_disk_gb")
    vendor = val(facts, "gpu_vendor")
    vram = val(facts, "gpu_vram_gb")
    arch = val(facts, "arch") or ""
    osn = val(facts, "os_name") or ""
    driver = val(facts, "gpu_driver")

    # R01 accelerator
    accel_kind, accel_gb, why = "none", 0.0, ""
    if vendor == "nvidia" and isinstance(vram, (int, float)) and vram > 0:
        accel_kind, accel_gb = "cuda", float(vram)
        why = "NVIDIA GPU with %.1f GB of video memory" % vram
        if "Windows" in osn and driver and parse_version(driver) and parse_version(driver) < MIN_WIN_DRIVER:
            accel_kind, accel_gb = "none", 0.0
            why = "NVIDIA driver %s is older than 551.61, which the runner needs on Windows: GPU not used until the driver is updated" % driver
            warnings.append("Update the NVIDIA driver (551.61 or newer), then re-run detect_machine.py. Until then the plan is CPU-only.")
    elif "arm" in arch.lower() and "mac" in osn.lower() and isinstance(ram, (int, float)):
        accel_kind, accel_gb = "metal", round(ram * 0.6, 1)
        why = "Apple silicon: video and system memory are shared; 60 percent of RAM (%.1f GB) is treated as usable" % accel_gb
    elif vendor == "nvidia" and vram is None:
        why = "NVIDIA GPU found but its video memory is unknown: treated as CPU-only. Tell the skill with --vram-gb N if you know it"
        warnings.append("GPU video memory unknown. If you know it, run detect_machine.py --vram-gb N.")
    elif vendor in ("amd", "intel", "other"):
        why = "GPU is not NVIDIA or Apple: not used by this skill's plan (treated as CPU-only)"
    else:
        why = "no usable GPU detected"
    fire("R01", "accelerator: %s, %.1f GB (%s)" % (accel_kind, accel_gb, why))

    # R02 tier
    if accel_kind == "none":
        tier = "T0"
    elif accel_gb >= 23:
        tier = "T5"
    elif accel_gb >= 15:
        tier = "T4"
    elif accel_gb >= 9:
        tier = "T3"
    elif accel_gb >= 5.5:
        tier = "T2"
    elif accel_gb >= 3:
        tier = "T1"
    else:
        tier = "T0"
        accel_kind = "none"
    tier_text = {"T5": "24 GB or more", "T4": "15 to 23 GB", "T3": "9 to 14 GB", "T2": "5.5 to 9 GB", "T1": "3 to 5.4 GB", "T0": "under 3 GB or no usable GPU (CPU only)"}[tier]
    fire("R02", "tier %s (accelerator memory %s)" % (tier, tier_text))

    # R03 headroom and budget
    headroom = {"cuda": 1.5, "metal": 2.0, "none": 0.0}[accel_kind]
    budget = round(max(0.0, accel_gb - headroom), 1)
    cpu_budget = round(max(0.0, min((ram or 0) * 0.25, 6.0)), 1) if isinstance(ram, (int, float)) else 0.0
    if accel_kind == "none":
        fire("R03", "model files up to %.1f GB run on the CPU (25 percent of RAM, at most 6 GB)" % cpu_budget)
    else:
        fire("R03", "model files up to %.1f GB fit fully (accelerator memory minus %.1f GB for the display, runtime and context)" % (budget, headroom))
    partial_limit = round(accel_gb * 1.5, 1) if accel_kind != "none" else 0.0

    # R05 context, R06 chunk
    num_ctx = {"T5": 32768, "T4": 16384, "T3": 8192, "T2": 4096, "T1": 4096, "T0": 8192 if (ram or 0) >= 32 else 4096}[tier]
    chunk = int(num_ctx * 4 * 0.4)
    fire("R05", "context window %d tokens (more context costs memory that small cards do not have)" % num_ctx)
    fire("R06", "one extraction call reads at most %d characters (40 percent of the window; the rest holds instructions, schema and answer); a longer page is split at block boundaries" % chunk)

    # R07 concurrency, R08 resident
    conc = 2 if tier == "T5" else 1
    resident = 2 if tier == "T5" else 1
    fire("R07", "local model calls at once: %d (raise only after measuring: throughput must rise and no run slow beyond 1.5x)" % conc)
    fire("R08", "models loaded at once: %d; work is grouped by role and the model is unloaded between roles" % resident)

    # R09 parse workers, R10 papers per batch
    workers = 1
    if isinstance(ram, (int, float)) and ram >= 8 and cores:
        workers = int(min(max(1, cores - 1), 4))
    fire("R09", "PDF parsing runs %d paper(s) at a time (cores %s, RAM %s GB)" % (workers, cores if cores else "unknown", ram if ram else "unknown"))
    if not isinstance(ram, (int, float)):
        ppb = 2
    elif ram < 8:
        ppb = 2
    elif ram < 16:
        ppb = 4
    elif ram < 32:
        ppb = 5
    else:
        ppb = 10
    fire("R10", "a batch job holds %d papers; a checkpoint is written after every item" % ppb)

    # R11 disk
    if isinstance(disk, (int, float)):
        models_budget = round(min(disk * 0.3, 40.0), 1)
        local_ok_disk = disk >= 10
        fire("R11", "free disk %.1f GB: at most %.1f GB for downloaded models%s" % (disk, models_budget, "" if local_ok_disk else "; below 10 GB free no model is downloaded"))
    else:
        models_budget, local_ok_disk = 0.0, False
        fire("R11", "free disk unknown: no model is downloaded until it is known")

    # R13 min speed
    min_tps = 2 if accel_kind == "none" else 8
    fire("R13", "a model must also reach %d tokens per second on this machine in the bake-off to take a role" % min_tps)

    # R04 candidate fit
    installed = val(facts, "runner_models")
    inst_names = {m["name"] for m in installed if isinstance(m, dict)} if isinstance(installed, list) else None
    fit_table = {}
    pools = cand["candidates"]
    for role, lst in pools.items():
        if isinstance(lst, str) and lst.startswith("same_as:"):
            lst = pools[lst.split(":", 1)[1]]
        rows = []
        for c in lst:
            s = c.get("size_gb")
            if s is None:
                fit = "unknown size (small): the bake-off decides" if c.get("cpu_fallback") or role == "embed" else "unknown size"
            elif s == 0:
                fit = "full"
            elif accel_kind == "none":
                fit = "cpu" if s <= cpu_budget else "no"
            elif s <= budget:
                fit = "full"
            elif s <= partial_limit:
                fit = "partial"
            else:
                fit = "no"
            if isinstance(s, (int, float)) and s > 0 and fit != "no" and s > models_budget:
                fit = "no (disk)"
            if c.get("cpu_fallback") and accel_kind != "none" and fit == "full":
                fit = "full (weak: fallback class)"
            if inst_names is None:
                inst = "unknown"
            else:
                inst = "yes" if (c["id"] in inst_names or (c["id"] + ":latest") in inst_names) else "no"
            rows.append({"id": c["id"], "family": c["family"], "size_gb": s, "fit": fit, "installed": inst, "licence": c.get("licence"), "fallback": bool(c.get("cpu_fallback"))})
        fit_table[role] = rows
    fire("R04", "each candidate is 'full' if its file is within the budget, 'partial' (some layers on the CPU, slower) if within 1.5 times the accelerator memory, otherwise 'no'")

    # R12 runner action, R16 python action
    rname, rver, rrun = val(facts, "runner_name"), val(facts, "runner_version"), val(facts, "runner_running")
    if rname in (None, "none"):
        runner_action = "No local model runner is installed. Local models are optional: install Ollama (see INSTALL.md) when the user wants the bake-off, or leave every role on Claude."
    elif not rrun:
        runner_action = "A runner is installed (%s) but its server is not running. Start it before any local-model step." % (rver or "version unknown")
    elif isinstance(installed, list) and not installed:
        runner_action = "Runner %s is running with no models installed. Download only the models the user approves, one at a time (sizes are in the candidate table)." % rver
    else:
        runner_action = "Runner %s is running; installed models are listed in the facts." % rver
    fire("R12", runner_action)
    pym = val(facts, "pymupdf_ok")
    py_action = "PDF reading is ready." if pym else "Run setup_env.py install once (needs internet; installs into the skill's private cache, no administrator rights)."
    if val(facts, "python_venv_ok") is False:
        py_action += " The 'venv' module is missing, so the installer falls back to a plain folder inside the cache."
    pyv = val(facts, "python_version")
    if pyv and parse_version(pyv) and parse_version(pyv) < (3, 8):
        py_action = "Python %s is too old: 3.8 or newer is needed." % pyv
        warnings.append("Python is older than 3.8: the scripts may not run.")
    fire("R16", py_action)

    # R14 scanned
    fire("R14", "scanned papers (no text layer) are marked scanned_no_text: no local OCR is attempted; a text version, or a person, is needed before cards can be cited" if accel_gb < 12 else
         "scanned papers: OCR with a vision model is possible in principle but is outside this skill; they are marked scanned_no_text")

    # R15 network
    net = val(facts, "net_anthropic")
    npypi = val(facts, "net_pypi")
    if net is False:
        warnings.append("The Claude service was not reachable from this computer at detection time.")
    yn = lambda x: "unknown" if x is None else ("reachable" if x else "NOT reachable")
    fire("R15", "internet: package index %s, runner library %s, Claude service %s" % (yn(npypi), yn(val(facts, "net_ollama")), yn(net)))

    # R19 role plan
    role_plan = {}
    for role in ("extract", "judge_support", "navsum", "embed", "rerank"):
        rows = fit_table.get(role, [])
        good = [r["id"] for r in rows if r["fit"].startswith(("full", "cpu")) and not r["fallback"]]
        weak = [r["id"] for r in rows if (r["fit"].startswith(("full", "cpu")) and r["fallback"])]
        part = [r["id"] for r in rows if r["fit"] == "partial"]
        fams = sorted({r["family"] for r in rows if r["fit"].startswith(("full", "cpu")) and r["family"] not in ("none", "same as the model used")})
        if role in ("extract", "judge_support"):
            if not local_ok_disk or (not good and not weak):
                run = "Claude (no local model fits)"
            elif len(fams) >= 2:
                run = "local two-pass possible (families that fit: %s) after a bake-off; Claude settles disagreements" % ", ".join(fams)
            elif good:
                run = "local first pass possible; only one family fits, so Claude is the second reader after a bake-off"
            else:
                run = "Claude (only weak fallback models fit; expect them to fail the pack)"
        elif role == "navsum":
            run = "local possible after a bake-off" if (good or weak) else "Claude"
        elif role == "embed":
            run = "local possible after a bake-off (keyword search works without it)" if (good or weak or any(r["fit"].startswith("unknown") for r in rows)) else "keyword search only"
        else:
            run = "keyword ranking by default; a model reranker is optional and must beat that baseline"
        role_plan[role] = {"runs_on": run, "fit_now": good, "fit_weak": weak, "partial": part, "families": fams}
    for role in ("adjudicate", "critique", "write", "reference"):
        role_plan[role] = {"runs_on": "Claude (never a local model: it needs the strongest reader)", "fit_now": [], "fit_weak": [], "partial": [], "families": []}
    fire("R19", "roles: bulk reading roles are offered to local models only after a bake-off; reference answers, adjudication, critique and writing stay on Claude")

    # R17 not attempted
    na = []
    if accel_gb < 12:
        na.append({"what": "vision and OCR models; reading page images or figures locally", "why": "image input needs memory beyond the text weights (accelerator memory %.1f GB)" % accel_gb, "rule": "R17a"})
    if accel_gb < 24:
        na.append({"what": "reading a whole paper in one local call (deep read)", "why": "context window is %d tokens here; whole papers need far more" % num_ctx, "rule": "R17b"})
        na.append({"what": "two models loaded at once, or two local calls at once", "why": "memory holds one model with its context; concurrency stays 1", "rule": "R17c"})
    na.append({"what": "models larger than %.1f GB%s" % ((partial_limit if accel_kind != "none" else cpu_budget), " (partial offload limit)" if accel_kind != "none" else " on the CPU"), "why": "they do not fit, or run too slowly to be useful", "rule": "R17d"})
    na.append({"what": "training or fine-tuning any model", "why": "outside this skill; corrections are kept as regression cases and examples instead", "rule": "R17e"})
    na.append({"what": "embedding search over more than 20,000 chunks", "why": "the pure-Python search is meant for a few thousand; keyword search is used beyond that", "rule": "R17f"})
    if accel_kind == "none":
        na.append({"what": "local extraction and support judging unless a fallback model passes its pack", "why": "CPU-only speed and small models; expect every role to stay on Claude", "rule": "R17g"})
    fire("R17", "%d item(s) are not attempted on this machine" % len(na))

    rules.sort(key=lambda r: r["rule"])
    settings = [
        {"key": "accelerator", "value": "%s, %.1f GB" % (accel_kind, accel_gb), "rule": "R01"},
        {"key": "tier", "value": tier, "rule": "R02"},
        {"key": "model_file_budget_gb", "value": budget if accel_kind != "none" else cpu_budget, "rule": "R03"},
        {"key": "partial_offload_limit_gb", "value": partial_limit, "rule": "R04"},
        {"key": "num_ctx", "value": num_ctx, "rule": "R05"},
        {"key": "extract_chunk_chars", "value": chunk, "rule": "R06"},
        {"key": "local_concurrency", "value": conc, "rule": "R07"},
        {"key": "models_resident", "value": resident, "rule": "R08"},
        {"key": "parse_workers", "value": workers, "rule": "R09"},
        {"key": "papers_per_batch", "value": ppb, "rule": "R10"},
        {"key": "models_disk_budget_gb", "value": models_budget, "rule": "R11"},
        {"key": "min_tokens_per_second", "value": min_tps, "rule": "R13"},
    ]
    s = {x["key"]: x["value"] for x in settings}
    summary = "%s; local models up to %.1f GB, context %d, %d local call at a time, %d papers per batch" % (
        {"cuda": "NVIDIA GPU %.0f GB" % accel_gb, "metal": "Apple silicon", "none": "CPU only"}[accel_kind], (budget if accel_kind != "none" else cpu_budget), num_ctx, conc, ppb)
    return {"version": 1, "derived_on": rwlib.now_iso(), "tier": tier, "summary": summary, "settings": s, "settings_table": settings,
            "role_plan": role_plan, "candidate_fit": fit_table, "not_attempted": na, "warnings": warnings, "rules_fired": rules,
            "local_models_possible": bool(local_ok_disk and (accel_kind != "none" or cpu_budget >= 1.0))}


# --------------------------------------------------------------- rendering
def fmt_fact(key, f):
    v = f.get("value")
    if key == "runner_models" and isinstance(v, list):
        return ", ".join("%s (%s GB)" % (m["name"], m["size_gb"]) if m.get("size_gb") is not None else m["name"] for m in v) or "none"
    if isinstance(v, list):
        return ", ".join(str(x) for x in v) or "none"
    if isinstance(v, bool):
        return "yes" if v else "no"
    return str(v)


def existing_user_notes(project):
    """Text of the USER NOTES section of the current MACHINE.md, or the default."""
    default = "(nothing yet. Write notes for yourself here; they survive every update.)\n"
    if not project:
        return default
    p = Path(project) / "MACHINE.md"
    if not p.exists():
        return default
    t = p.read_text(encoding="utf-8")
    i = t.find("## USER NOTES")
    if i < 0:
        return default
    body = t[i:].split("\n", 1)[1] if "\n" in t[i:] else ""
    return body.lstrip("\n") or default


def render_markdown(doc, plan, roles, user_notes=""):
    """MACHINE.md text."""
    f = doc["facts"]
    L = ["# MACHINE", "",
         "Generated by detect_machine.py on %s (facts) and make_plan.py (plan). Do not edit by hand except the USER NOTES at the end;" % doc["detected_on"][:10],
         "run `detect_machine.py` again after any hardware or driver change. Earlier versions are in `machine/history/`.", "",
         "Fingerprint of the facts: `%s`. Unknown facts are marked unknown and are never guessed." % doc["fingerprint"], "",
         "## FACTS (detected %s)" % doc["detected_on"], "",
         "| Fact | Value | How obtained | Status |", "|---|---|---|---|"]
    for key, label in FACT_ROWS:
        if key in f:
            L.append("| %s | %s | %s | %s |" % (label, fmt_fact(key, f[key]).replace("|", "/"), str(f[key].get("how", "")).replace("|", "/"), f[key].get("status", "")))
    for key in sorted(k for k in f if k.startswith("error_")):
        L.append("| (%s) | unknown | %s | %s |" % (key, f[key].get("how"), f[key].get("status")))
    L += ["", "## PLAN (derived %s from the facts above)" % plan["derived_on"], "", "**Summary:** " + plan["summary"], "",
          "### Settings", "", "| Setting | Value | Rule |", "|---|---|---|"]
    for s in plan["settings_table"]:
        L.append("| %s | %s | %s |" % (s["key"], s["value"], s["rule"]))
    L += ["", "### Rules that fired (see MACHINE.md (the rules are printed there) for the rule table)", ""]
    for r in plan["rules_fired"]:
        L.append("- **%s**: %s" % (r["rule"], r["result"]))
    L += ["", "### Roles: what runs where", "",
          "| Role | Plan for this machine | Assigned now | Evidence (runs in evals/results.jsonl) |", "|---|---|---|---|"]
    rr = (roles or {}).get("roles", {})
    for role in ("extract", "judge_support", "navsum", "embed", "rerank", "adjudicate", "critique", "write", "reference"):
        e = rr.get(role, {})
        who = e.get("assigned", "claude")
        if who == "local":
            who = "local: %s%s%s" % (e.get("primary"), (" + " + e["secondary"]) if e.get("secondary") else "", " (STALE: retest)" if e.get("stale") else "")
        elif role in ("extract", "judge_support", "navsum", "embed", "rerank"):
            who = "Claude (no local model has passed yet; said plainly: this role is on Claude)"
        L.append("| %s | %s | %s | %s |" % (role, plan["role_plan"][role]["runs_on"], who, ", ".join(e.get("evidence", [])) or "none yet"))
    L += ["", "### Candidate models on this machine (the bake-off is the judge; the list is dated in candidates.json)", ""]
    for role in ("extract", "embed", "rerank"):
        L += ["**%s%s**" % (role, " (also used for judge_support and navsum)" if role == "extract" else ""), "",
              "| Model | Family | Size GB | Fit here | Installed | Licence |", "|---|---|---|---|---|---|"]
        for r in plan["candidate_fit"].get(role, []):
            L.append("| %s | %s | %s | %s | %s | %s |" % (r["id"], r["family"], r["size_gb"] if r["size_gb"] is not None else "not checked", r["fit"], r["installed"], str(r["licence"]).replace("|", "/")))
        L.append("")
    L += ["### Not attempted on this machine", "", "| What | Why | Rule |", "|---|---|---|"]
    for n in plan["not_attempted"]:
        L.append("| %s | %s | %s |" % (n["what"], n["why"], n["rule"]))
    L += ["", "### Warnings", ""]
    L += ["- " + w for w in plan["warnings"]] or ["- none"]
    L += ["", "### Reminders that do not depend on hardware", "",
          "- Anything sent to Claude leaves this computer. Whether paper text may go to Claude is decided per project in `project.json` (confidentiality) and recorded in DECISIONS.md.",
          "- Local models are proposers. Nothing they write is used until the deterministic checks pass (SKILL.md (local-model policy)).", "",
          USER_NOTES_MARK, "", user_notes.rstrip("\n") + "\n"]
    return "\n".join(L)


def mark_stale_roles(project, plan):
    """Mark local role assignments stale when the machine changed. Returns the list of roles marked."""
    p = Path(project) / "roles.json"
    roles = rwlib.read_json(p)
    if not roles:
        return []
    results = {r["run_id"]: r for r in rwlib.read_jsonl(Path(project) / "evals" / "results.jsonl") if "run_id" in r}
    fp = (rwlib.read_json(Path(project) / "machine" / "facts.json", {}) or {}).get("fingerprint")
    marked = []
    for name, e in roles["roles"].items():
        if e.get("assigned") != "local" or e.get("stale"):
            continue
        ev = [results[x] for x in e.get("evidence", []) if x in results]
        if not any(x.get("machine_ref") == fp for x in ev):
            e["stale"] = True
            e["note"] = ("machine facts changed since the bake-off (%s): retest before using this model" % rwlib.today())
            marked.append(name)
            continue
        fits = {r["id"]: r["fit"] for r in plan["candidate_fit"].get("extract" if name in ("extract", "judge_support", "navsum") else name, [])}
        for tag in (e.get("primary"), e.get("secondary")):
            if tag and fits.get(tag, "").startswith("no"):
                e["stale"] = True
                e["note"] = "model %s no longer fits this machine's plan: retest or reassign" % tag
                marked.append(name)
                break
    if marked:
        roles["updated"] = rwlib.now_iso()
        rwlib.write_json(p, roles)
    return marked


def main():
    rwlib.setup_console()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project")
    ap.add_argument("--facts-json")
    ap.add_argument("--stdout", action="store_true")
    a = ap.parse_args()
    cand = rwlib.read_json(rwlib.SKILL_DIR / "candidates.json")
    if a.facts_json:
        doc = rwlib.read_json(a.facts_json)
        if "facts" not in doc:
            doc = {"detected_on": rwlib.now_iso(), "facts": doc, "fingerprint": "from-file"}
        plan = derive_plan(doc["facts"], cand, {})
        print(render_markdown(doc, plan, {}))
        return 0
    project = rwlib.find_project(explicit=a.project)
    doc = rwlib.read_json(project / "machine" / "facts.json")
    if not doc:
        rwlib.die("No facts yet. Run detect_machine.py first.")
    roles = rwlib.read_json(project / "roles.json", {})
    plan = derive_plan(doc["facts"], cand, roles)
    md = render_markdown(doc, plan, roles, existing_user_notes(project))
    if a.stdout:
        print(md)
        return 0
    import shutil
    stamp = rwlib.now_iso()[:19].replace(":", "").replace("-", "").replace("T", "-")
    for name in ("MACHINE.md", "machine/plan.json"):
        pth = project / name
        if pth.exists():
            shutil.copyfile(pth, project / "machine" / "history" / ("%s_%s%s" % (Path(name).stem, stamp, Path(name).suffix)))
    rwlib.write_json(project / "machine" / "plan.json", plan)
    rwlib.write_text_atomic(project / "MACHINE.md", md)
    stale = mark_stale_roles(project, plan)
    print("plan re-derived from the stored facts; MACHINE.md rewritten (previous kept in machine/history/)")
    if stale:
        print("roles marked stale: " + ", ".join(stale))
    return 0


if __name__ == "__main__":
    sys.exit(main())
