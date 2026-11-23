#!/usr/bin/env python3
"""Find passages and cards by words (BM25), optionally fused with embedding search.

Usage:
  search.py index [--embed]            (re)build the keyword index over parsed pages and cards;
                                       with --embed also embed every chunk with the `embed` role model
                                       (resumable: chunks already embedded are skipped)
  search.py query "words" [-k 8] [--kind pages|cards|both] [--dense]
  search.py fulltext "exact words"      every page that contains the words (exact, normalised): the
                                        deterministic search used before calling a cell an empty one

Chunks are runs of blocks up to about 900 characters. Bibliography blocks are never indexed for content search.
Keyword search always works. Embedding search is used only if the `embed` role has a model that passed its
bake-off, and the query is embedded by exactly the model that built the index (a mismatch is refused).
With --dense the two rankings are merged by reciprocal rank fusion. Pure Python: suited to a few thousand
chunks, up to about 20,000. Beyond that keyword search only.
Output: one line per hit: rank, id, paper, page, score, start of the text.
"""
import argparse
import math
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import rwlib  # noqa: E402

CHUNK_CHARS = 900
MAX_DENSE_CHUNKS = 20000
TOKEN = re.compile(r"\w+(?:[.\-]\w+)*", re.U)


def tokens(text):
    """Lower-case word tokens; numbers like 0.82 and words like state-of-the-art stay whole."""
    return TOKEN.findall(rwlib.norm_simple(text).lower())


def build_chunks(project):
    """Chunks from parsed pages (not bibliography) and from current cards."""
    reg = rwlib.load_register(project)
    chunks = []
    for pid in sorted(reg):
        r = reg[pid]
        if r["state"] not in ("full_text",) or r["screening"] == "excluded":
            continue
        meta = rwlib.read_json(project / "parsed" / pid / "meta.json")
        if not meta:
            continue
        for pno in range(1, meta["pages"] + 1):
            page = rwlib.load_page(project, pid, pno)
            if not page:
                continue
            cur, cur_start, n = [], None, 0
            def flush():
                nonlocal cur, cur_start, n
                if cur:
                    start, end = cur[0]["start"], cur[-1]["end"]
                    chunks.append({"id": "%s-p%04d-c%02d" % (pid, pno, n + 1), "kind": "page", "paper": pid, "page": pno,
                                   "start": start, "end": end, "text": page["text"][start:end]})
                    n += 1
                cur, cur_start = [], None
            for b in page["blocks"]:
                if b["zone"] == "bibliography":
                    continue
                if cur and (b["end"] - cur[0]["start"]) > CHUNK_CHARS:
                    flush()
                cur.append(b)
            flush()
    for cid, c in sorted(rwlib.current_cards(project).items()):
        if c["review_state"] == "rejected" or not c.get("stated"):
            continue
        q = c["locators"][0]["quote"] if c["locators"] else ""
        chunks.append({"id": cid, "kind": "card", "paper": c["paper_id"], "page": c["locators"][0]["page"] if c["locators"] else 0,
                       "start": 0, "end": 0, "text": c["statement"] + " " + q})
    return chunks


def build_bm25(chunks):
    """BM25 statistics for a list of chunks."""
    post, doclen = {}, []
    for i, ch in enumerate(chunks):
        toks = tokens(ch["text"])
        doclen.append(len(toks))
        tf = {}
        for t in toks:
            tf[t] = tf.get(t, 0) + 1
        for t, f in tf.items():
            post.setdefault(t, []).append([i, f])
    n = len(chunks)
    return {"n": n, "avgdl": (sum(doclen) / n) if n else 0.0, "doclen": doclen, "postings": post}


def bm25_rank(index, query, k=10, k1=1.5, b=0.75):
    """Return [(doc_index, score)] best first."""
    scores = {}
    n, avgdl = index["n"], index["avgdl"] or 1.0
    for t in set(tokens(query)):
        plist = index["postings"].get(t)
        if not plist:
            continue
        idf = math.log(1 + (n - len(plist) + 0.5) / (len(plist) + 0.5))
        for i, f in plist:
            dl = index["doclen"][i]
            scores[i] = scores.get(i, 0.0) + idf * f * (k1 + 1) / (f + k1 * (1 - b + b * dl / avgdl))
    return sorted(scores.items(), key=lambda x: -x[1])[:k]


def rrf(rankings, k=60):
    """Reciprocal rank fusion of several [(doc, score)] lists."""
    fused = {}
    for r in rankings:
        for rank, (d, _) in enumerate(r, 1):
            fused[d] = fused.get(d, 0.0) + 1.0 / (k + rank)
    return sorted(fused.items(), key=lambda x: -x[1])


def cosine(a, b):
    num = sum(x * y for x, y in zip(a, b))
    da = math.sqrt(sum(x * x for x in a))
    db = math.sqrt(sum(y * y for y in b))
    return num / (da * db) if da and db else 0.0


def load_index(project):
    chunks = rwlib.read_jsonl(project / "index" / "chunks.jsonl")
    bm = rwlib.read_json(project / "index" / "bm25.json")
    if not chunks or not bm:
        rwlib.die("No index yet. Run: search.py index")
    return chunks, bm


def embed_model(project):
    """The embed role's model if it is assigned locally and fresh, else None."""
    ent = ((rwlib.read_json(project / "roles.json", {}) or {}).get("roles", {})).get("embed", {})
    if ent.get("assigned") == "local" and not ent.get("stale") and ent.get("primary"):
        return ent["primary"]
    return None


def cmd_index(project, a):
    chunks = build_chunks(project)
    rwlib.write_jsonl(project / "index" / "chunks.jsonl", chunks)
    rwlib.write_json(project / "index" / "bm25.json", build_bm25(chunks))
    print("keyword index: %d chunks (%d from pages, %d from cards)" % (
        len(chunks), sum(1 for c in chunks if c["kind"] == "page"), sum(1 for c in chunks if c["kind"] == "card")))
    if a.embed:
        import ollama_api
        model = embed_model(project)
        if not model:
            print("The embed role has no model that passed its bake-off here: keyword search only (this is fine).")
            return 0
        if len(chunks) > MAX_DENSE_CHUNKS:
            print("%d chunks is more than the %d this script handles for embeddings: keyword search only." % (len(chunks), MAX_DENSE_CHUNKS))
            return 0
        meta_p = project / "index" / "embed_meta.json"
        meta = rwlib.read_json(meta_p)
        vec_p = project / "index" / "embed.jsonl"
        if meta and meta["model"] != model:
            print("Existing embeddings were made by %s; building a new set beside them is needed. Move index/embed.jsonl away (ask the user) first." % meta["model"])
            return 1
        have = {r["id"] for r in rwlib.read_jsonl(vec_p)}
        todo = [c for c in chunks if c["id"] not in have]
        dim = meta["dim"] if meta else None
        for i in range(0, len(todo), 16):
            batch = todo[i:i + 16]
            vecs = ollama_api.embed(model, [c["text"] for c in batch])
            dim = dim or len(vecs[0])
            for c, v in zip(batch, vecs):
                rwlib.append_jsonl(vec_p, {"id": c["id"], "v": [round(x, 6) for x in v]})
            if not meta:
                meta = {"model": model, "dim": dim, "built_on": rwlib.now_iso()}
                rwlib.write_json(meta_p, meta)
        ollama_api.unload(model)
        print("embeddings: %d new, %d already present (model %s, dimension %s)" % (len(todo), len(have), model, dim))
    return 0


def cmd_query(project, a):
    chunks, bm = load_index(project)
    wanted = [i for i, c in enumerate(chunks) if a.kind == "both" or (a.kind == "pages" and c["kind"] == "page") or (a.kind == "cards" and c["kind"] == "card")]
    rank = [(i, s) for i, s in bm25_rank(bm, a.text, k=len(chunks)) if i in set(wanted)][:max(a.k * 3, 30)]
    mode = "keyword"
    if a.dense:
        import ollama_api
        model = embed_model(project)
        meta = rwlib.read_json(project / "index" / "embed_meta.json")
        if not model or not meta:
            print("(dense search unavailable: no qualified embed model or no embeddings; using keyword only)")
        elif meta["model"] != model:
            rwlib.die("The index was embedded by %s but the embed role is now %s. Rebuild the embeddings; mixing them gives nonsense." % (meta["model"], model))
        else:
            vecs = {r["id"]: r["v"] for r in rwlib.read_jsonl(project / "index" / "embed.jsonl")}
            q = ollama_api.embed(model, [a.text])[0]
            dense = sorted(((i, cosine(q, vecs[chunks[i]["id"]])) for i in wanted if chunks[i]["id"] in vecs), key=lambda x: -x[1])[:max(a.k * 3, 30)]
            rank = rrf([rank, dense])
            mode = "keyword+dense (rank fusion)"
    print("%d hit(s) shown, mode %s" % (min(a.k, len(rank)), mode))
    for n, (i, s) in enumerate(rank[:a.k], 1):
        c = chunks[i]
        print("%2d  %-18s %s p%s  %.3f  %s" % (n, c["id"], c["paper"], c["page"], s, re.sub(r"\s+", " ", c["text"])[:110]))
    if not rank:
        print("no match")
    return 0


def cmd_fulltext(project, a):
    reg = rwlib.load_register(project)
    hits = 0
    total_pages = 0
    for pid in sorted(reg):
        meta = rwlib.read_json(project / "parsed" / pid / "meta.json")
        if not meta:
            continue
        for pno in range(1, meta["pages"] + 1):
            page = rwlib.load_page(project, pid, pno)
            total_pages += 1
            f = rwlib.find_quote(page["text"], a.text)
            if f:
                hits += 1
                print("%s p%d (%s) ...%s..." % (pid, pno, f[0], re.sub(r"\s+", " ", page["text"][max(0, f[1] - 40): f[2] + 40])))
    print("fulltext: %d page(s) contain the words, out of %d parsed pages" % (hits, total_pages))
    return 0


def main():
    rwlib.setup_console()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("index")
    p.add_argument("--embed", action="store_true")
    p = sub.add_parser("query")
    p.add_argument("text")
    p.add_argument("-k", type=int, default=8)
    p.add_argument("--kind", choices=["pages", "cards", "both"], default="both")
    p.add_argument("--dense", action="store_true")
    p = sub.add_parser("fulltext")
    p.add_argument("text")
    a = ap.parse_args()
    project = rwlib.find_project(explicit=a.project)
    return {"index": cmd_index, "query": cmd_query, "fulltext": cmd_fulltext}[a.cmd](project, a)


if __name__ == "__main__":
    sys.exit(main())
