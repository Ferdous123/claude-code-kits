#!/usr/bin/env python3
"""Extract text per page, with block positions, from registered papers.

Usage:
  extract_pages.py [--project DIR] (--paper P0001 [--paper P0002 ...] | --all) [--force] [--timeout SECONDS]

For each paper this writes parsed/<paper_id>/pages/pNNNN.json (one file per page, written one at a time) and
parsed/<paper_id>/meta.json (written last: a paper counts as parsed only when meta.json exists). Re-running is
safe: a paper that is already parsed is skipped unless --force is given.

Each paper is read in a separate child process with a time limit, so a broken or hostile PDF can only fail its
own paper. The child needs PyMuPDF for .pdf files (install it with setup_env.py install); .txt and .md files
need nothing. Text files with form-feed characters are split at them; others are cut into pseudo-pages of about
3000 characters (the page numbers are then not the paper's own, and meta.json says so).

Coordinates are PDF points, origin top-left, y downward, page rotation applied, on the visible page area.
Zones (title block, abstract, body, related work, bibliography, ...) are set by simple heading rules and are
marked "heuristic"; a person can correct them.
"""
import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import rwlib  # noqa: E402

LOW_TEXT = 200
PSEUDO_PAGE_CHARS = 3000
SUSPECT_MIN = 5          # control codes, private-use or replacement characters on one page before it is listed as suspect
TITLE_SHARE = 0.7        # share of the register title's longer words that must occur on pages 1-2

HEAD_WORDS = (
    r"abstract|introduction|related work|related works|background|literature review|preliminaries|"
    r"method|methods|methodology|materials and methods|approach|proposed method|model|experiments?|experimental setup|"
    r"results?|results and discussion|discussion|conclusions?|limitations?|future work|"
    r"references|bibliography|appendix|appendices|supplementary material|acknowledg(?:e)?ments?"
)
HEAD_RE = re.compile(r"^\s*(?:(?:\d+(?:\.\d+)*|[IVXLC]+|[A-Z])[.)]?\s+)?(" + HEAD_WORDS + r")\b", re.I)
ABSTRACT_RE = re.compile(r"^\s*abstract\s*[—–:.\-]\s*\S", re.I)
CAPTION_RE = re.compile(r"^\s*(figure|fig\.|table)\s*[A-Z]?\d+[.:)\s]", re.I)


def section_zone(word):
    """Map a heading word to the zone of the text that follows it."""
    w = word.lower()
    if w == "abstract":
        return "abstract"
    if w in ("related work", "related works", "background", "literature review"):
        return "related_work"
    if w in ("references", "bibliography"):
        return "bibliography"
    if w.startswith("appendi") or w.startswith("supplementary"):
        return "appendix"
    if w.startswith("acknowledg"):
        return "footnote"
    return "body"


def assign_zones(texts, state):
    """Assign a zone to each block text. `state` is a dict carrying the current section across pages."""
    zones = []
    for t in texts:
        flat = t.strip()
        lines = flat.split("\n")
        m = HEAD_RE.match(flat)
        if m and len(flat) <= 80 and len(lines) <= 2:
            state["zone"] = section_zone(m.group(1))
            zones.append("heading")
        elif ABSTRACT_RE.match(flat):
            state["zone"] = "abstract"
            zones.append("abstract")
        elif CAPTION_RE.match(flat):
            zones.append("caption")
        else:
            zones.append(state["zone"])
    return zones


def page_record(paper_id, pno, text_blocks, bboxes, width, height, rotation, backend, version, state):
    """Build the page record: join blocks, remember offsets, assign zones."""
    text = ""
    blocks = []
    zones = assign_zones(text_blocks, state)
    for i, (bt, bb, z) in enumerate(zip(text_blocks, bboxes, zones), 1):
        bt = bt.strip()
        if text:
            text += "\n\n"
        start = len(text)
        text += bt
        blocks.append({"block_id": "%s-p%04d-b%03d" % (paper_id, pno, i), "start": start, "end": len(text),
                       "bbox": bb, "zone": z, "zone_source": "heuristic"})
    return {"paper_id": paper_id, "page": pno, "width": width, "height": height, "rotation": rotation, "text": text,
            "text_sha256": rwlib.sha256_text(text), "chars": len(text), "blocks": blocks, "backend": backend,
            "parser_version": version, "extracted_on": rwlib.now_iso()}


def read_pdf(path, paper_id):
    """Yield page records from a PDF with PyMuPDF."""
    try:
        import pymupdf as fitz
    except ImportError:
        import fitz
    version = getattr(fitz, "VersionBind", "unknown")
    state = {"zone": "title_block"}
    doc = fitz.open(str(path))
    if getattr(doc, "needs_pass", False):
        raise RuntimeError("the PDF is encrypted and needs a passphrase")
    for idx in range(doc.page_count):
        page = doc[idx]
        rot = page.rotation
        rmat = page.rotation_matrix
        texts, boxes = [], []
        for b in page.get_text("blocks", sort=True):
            if len(b) >= 7 and b[6] != 0:
                continue  # image block
            t = (b[4] or "").strip()
            if not t:
                continue
            r = fitz.Rect(b[0], b[1], b[2], b[3])
            if rot:
                r = r * rmat
                r.normalize()
            texts.append(t)
            boxes.append([round(r.x0, 2), round(r.y0, 2), round(r.x1, 2), round(r.y1, 2)])
        yield page_record(paper_id, idx + 1, texts, boxes, round(page.rect.width, 2), round(page.rect.height, 2),
                          rot, "pymupdf", str(version), state)


def read_text_file(path, paper_id):
    """Yield page records from a text file."""
    raw = Path(path).read_text(encoding="utf-8", errors="replace")
    state = {"zone": "title_block"}
    if "\f" in raw:
        pages = raw.split("\f")
        while pages and not pages[-1].strip():
            pages.pop()
        pseudo = False
    else:
        pseudo = True
        paras = [p for p in re.split(r"\n\s*\n", raw) if p.strip()]
        pages, cur, size = [], [], 0
        for p in paras:
            if cur and size + len(p) > PSEUDO_PAGE_CHARS:
                pages.append("\n\n".join(cur))
                cur, size = [], 0
            cur.append(p)
            size += len(p)
        if cur:
            pages.append("\n\n".join(cur))
    for i, ptxt in enumerate(pages, 1):
        paras = [p for p in re.split(r"\n\s*\n", ptxt) if p.strip()]
        yield page_record(paper_id, i, paras, [None] * len(paras), None, None, 0,
                          "text" + ("-pseudo-pages" if pseudo else ""), "1", state)


def suspect_chars(text):
    """Count characters that signal a broken text layer: control codes, private-use glyphs, replacement characters.
    A maths font that the PDF maps to such codes makes the page's formulas (and sometimes words) unreliable."""
    n = 0
    for ch in text:
        o = ord(ch)
        if (o < 32 and ch not in "\n\r\t\f") or o == 127 or 0xE000 <= o <= 0xF8FF or o == 0xFFFD:
            n += 1
    return n


def pdf_eof_ok(path):
    """True if the file's last 1024 bytes hold the PDF end marker; False for a truncated download; None if unreadable."""
    try:
        with open(str(path), "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - 1024))
            return b"%%EOF" in fh.read()
    except OSError:
        return None


def title_check(title, first_pages_text):
    """Does the register title occur on pages 1-2? 'found', 'not_found' or 'not_checked' (title too short to test)."""
    words = [w for w in re.findall(r"[a-z0-9]+", rwlib.norm_simple(title or "").lower()) if len(w) >= 4]
    if len(words) < 2:
        return "not_checked"
    hay = set(re.findall(r"[a-z0-9]+", rwlib.norm_simple(first_pages_text).lower()))
    share = sum(1 for w in words if w in hay) / float(len(words))
    return "found" if share >= TITLE_SHARE else "not_found"


def worker(project, paper_id, force):
    """Parse one paper (runs in the child process). Returns an exit code."""
    reg = rwlib.load_register(project)
    rec = reg[paper_id]
    if not rec.get("stored_path"):
        print("%s: no file to parse (state %s)" % (paper_id, rec["state"]))
        return 0
    meta_path = project / "parsed" / paper_id / "meta.json"
    if meta_path.exists() and not force:
        print("%s: already parsed (use --force to redo)" % paper_id)
        return 0
    src = project / rec["stored_path"]
    pages_dir = project / "parsed" / paper_id / "pages"
    pages_dir.mkdir(parents=True, exist_ok=True)
    gen = read_pdf(src, paper_id) if src.suffix.lower() == ".pdf" else read_text_file(src, paper_id)
    n, total, low, suspect, backend, version = 0, 0, [], [], "", ""
    first_text = ""
    for page in gen:
        rwlib.write_json(pages_dir / ("p%04d.json" % page["page"]), page)
        n += 1
        total += page["chars"]
        backend, version = page["backend"], page["parser_version"]
        if page["chars"] < LOW_TEXT:
            low.append(page["page"])
        if suspect_chars(page["text"]) >= SUSPECT_MIN:
            suspect.append(page["page"])
        if n <= 2:
            first_text += "\n" + page["text"]
    with_text = n - len(low)
    layer = "none" if with_text == 0 else ("partial" if with_text < 0.6 * n else "ok")
    eof = pdf_eof_ok(src) if src.suffix.lower() == ".pdf" else None
    tcheck = title_check(rec.get("title"), first_text)
    meta = {"paper_id": paper_id, "pages": n, "backend": backend, "parser_version": version, "total_chars": total,
            "low_text_pages": low, "text_layer": layer, "eof_marker": eof, "suspect_text_pages": suspect,
            "title_check": tcheck, "parsed_on": rwlib.now_iso()}
    rwlib.write_json(meta_path, meta)
    print("%s: %d pages, %d characters, text layer %s%s" % (paper_id, n, total, layer, ", low-text pages %s" % low if low else ""))
    if eof is False:
        print("%s: WARNING no PDF end marker: the download is probably cut off. Get the file again before reading it." % paper_id)
    if suspect:
        print("%s: WARNING suspect text layer on pages %s (control or private-use characters): formulas and symbols there are not evidence; check the page image." % (paper_id, suspect))
    if tcheck == "not_found":
        print("%s: WARNING the register title was not found on pages 1-2: confirm this file is the paper you registered." % paper_id)
    return 0


def pick_python():
    """An interpreter that can read PDFs: this one, or the private environment."""
    import setup_env
    py = setup_env.find_python()
    return str(py) if py else None


def apply_meta(project, paper_id):
    """Copy the parse result (page count, text layer) into the register. Called by the parent process only."""
    reg = rwlib.load_register(project)
    rec = reg[paper_id]
    meta = rwlib.read_json(project / "parsed" / paper_id / "meta.json")
    if not meta:
        return
    new = dict(rec)
    new["pages"] = meta["pages"]
    if rec["state"] in ("full_text", "scanned_no_text", "failed"):
        with_text = meta["pages"] - len(meta["low_text_pages"])
        if meta["text_layer"] == "none":
            new["state"], new["state_reason"] = "scanned_no_text", "no usable text layer on any page: needs OCR or a text version"
        else:
            new["state"], new["state_reason"] = "full_text", "text layer %s; %d of %d pages have text" % (meta["text_layer"], with_text, meta["pages"])
    if new != rec:
        rwlib.append_jsonl(project / "corpus" / "register.jsonl", new)


def run_one(project, pid, py, force, timeout):
    """Run the child process for one paper. Returns (ok, message)."""
    cmd = [py, str(Path(__file__).resolve()), "--worker", "--project", str(project), "--paper", pid] + (["--force"] if force else [])
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, "timed out after %d s" % timeout
    if r.returncode != 0:
        return False, ((r.stderr or "").strip().splitlines() or ["unknown error"])[-1][:200]
    return True, (r.stdout or "").strip()


def main():
    rwlib.setup_console()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project")
    ap.add_argument("--paper", action="append", default=[])
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--timeout", type=int, default=300, help="seconds allowed per paper (default 300)")
    ap.add_argument("--workers", type=int, help="papers parsed at the same time (default: from machine/plan.json, else 1)")
    ap.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    a = ap.parse_args()
    project = rwlib.find_project(explicit=a.project)
    if a.worker:
        env_site = rwlib.cache_dir() / "site"
        if env_site.exists():
            sys.path.insert(0, str(env_site))
        return worker(project, a.paper[0], a.force)
    reg = rwlib.load_register(project)
    ids = sorted(reg) if a.all else a.paper
    if not ids:
        rwlib.die("Give --paper P0001 or --all")
    ids = [i for i in ids if reg.get(i, {}).get("stored_path")]
    need_pdf = any(reg[i]["stored_path"].lower().endswith(".pdf") for i in ids)
    pdf_py = pick_python() if need_pdf else None
    if need_pdf and not pdf_py:
        print("PyMuPDF is not installed. Run: %s %s install" % (sys.executable, Path(__file__).with_name("setup_env.py")))
    plan = rwlib.read_json(project / "machine" / "plan.json", {}) or {}
    workers = a.workers or int(plan.get("settings", {}).get("parse_workers", 1))
    jobs = []
    failed = 0
    for pid in ids:
        is_pdf = reg[pid]["stored_path"].lower().endswith(".pdf")
        py = pdf_py if is_pdf else sys.executable
        if is_pdf and not py:
            print("%s: skipped (needs PyMuPDF)" % pid)
            failed += 1
            continue
        jobs.append((pid, py))
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = [(pid, pool.submit(run_one, project, pid, py, a.force, a.timeout)) for pid, py in jobs]
        for pid, fut in futures:           # results are collected in order; the register is written here only
            ok, msg = fut.result()
            if ok:
                apply_meta(project, pid)
                print(msg)
            else:
                new = dict(rwlib.load_register(project)[pid])
                new["state"], new["state_reason"] = "failed", "parse failed: " + msg
                rwlib.append_jsonl(project / "corpus" / "register.jsonl", new)
                print("%s: FAILED (%s)" % (pid, msg))
                failed += 1
    print("parsed %d paper(s), %d failed or skipped (%d at a time)" % (len(ids) - failed, failed, max(1, workers)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
