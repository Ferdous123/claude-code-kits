"""Shared helpers for the research-workflow scripts (Python standard library only).

Every script imports this module. It keeps the rules that must be identical
everywhere in one place: where the project root is, how files are written
(atomically, inside the project only), how ids are made, how text is
normalised for the `located` check, and how proportions get an interval.
"""
import datetime
import hashlib
import json
import os
import re
import sys
import tempfile
import unicodedata
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = Path(__file__).resolve().parent
DATA_DIR = SCRIPTS_DIR / "data"
SCHEMA_DIR = SKILL_DIR / "templates" / "schemas"

CHECK_NAMES = ["located", "zone_valid", "supported", "consistent", "cross_read"]
STATUS_WORDS = ["written", "passes-with-stub", "ran-on-real-model", "measured", "operator-inspected"]


# ----------------------------------------------------------------- console
def setup_console():
    """Make printing safe on Windows consoles (UTF-8, never crash on odd characters)."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


# ------------------------------------------------------------------- time
def now_iso():
    """Current local time, to the second, with the UTC offset."""
    return datetime.datetime.now().astimezone().replace(microsecond=0).isoformat()


def today():
    """Current local date as YYYY-MM-DD."""
    return datetime.date.today().isoformat()


# ------------------------------------------------------------- project root
def find_project(start=None, explicit=None):
    """Return the project folder (the one holding project.json).

    `explicit` wins. Otherwise search upward from `start` (default: the
    current folder). Exits with a clear message if none is found.
    """
    if explicit:
        p = Path(explicit).expanduser().resolve()
        if not (p / "project.json").exists():
            die("No project.json in %s. Run rw_init.py first." % p)
        return p
    cur = Path(start or os.getcwd()).resolve()
    for folder in [cur] + list(cur.parents):
        if (folder / "project.json").exists():
            return folder
    die("No project found (no project.json here or above). Run rw_init.py first, or pass --project.")


def die(msg, code=2):
    """Print an error to stderr and exit."""
    print("ERROR: " + msg, file=sys.stderr)
    sys.exit(code)


def inside(project, path):
    """True if `path` is inside `project` (after resolving links)."""
    try:
        Path(path).resolve().relative_to(Path(project).resolve())
        return True
    except ValueError:
        return False


def safe_path(project, rel):
    """Join `rel` to the project folder; refuse anything that escapes it."""
    p = (Path(project) / rel).resolve()
    if not inside(project, p):
        die("Refusing to touch a path outside the project: %s" % rel)
    return p


def cache_dir():
    """The skill's private cache folder (virtual environment, nothing else)."""
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = str(Path.home() / "Library" / "Caches")
    else:
        base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "research-workflow"


# ---------------------------------------------------------------- file I/O
def write_text_atomic(path, text):
    """Write a text file so that a power cut leaves either the old or the new file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp_", suffix=".part")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, str(path))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_json(path, default=None):
    """Read a JSON file; return `default` if it does not exist."""
    path = Path(path)
    if not path.exists():
        return default
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def write_json(path, obj):
    """Write a JSON file atomically, indented, keys in the order given."""
    write_text_atomic(path, json.dumps(obj, indent=2, ensure_ascii=False) + "\n")


def read_jsonl(path):
    """Read a JSON Lines file into a list. A torn last line (power cut) is skipped."""
    path = Path(path)
    out = []
    if not path.exists():
        return out
    with open(path, "r", encoding="utf-8") as fh:
        lines = fh.read().split("\n")
    for i, line in enumerate(lines):
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            if i >= len(lines) - 2:
                continue  # torn final line from an interrupted append
            raise
    return out


def append_jsonl(path, rec):
    """Append one record to a JSON Lines file and force it to disk."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    needs_nl = False
    if path.exists() and path.stat().st_size > 0:
        with open(path, "rb") as fh:
            fh.seek(-1, 2)
            needs_nl = fh.read(1) != b"\n"
    with open(path, "a", encoding="utf-8", newline="\n") as fh:
        if needs_nl:
            fh.write("\n")
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def write_jsonl(path, recs):
    """Rewrite a whole JSON Lines file atomically."""
    write_text_atomic(path, "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in recs))


# ------------------------------------------------------------------ hashing
def sha256_file(path):
    """SHA-256 of a file, read in chunks."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_text(text):
    """SHA-256 of a string (UTF-8)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------- ids
def next_id(prefix, existing_ids, width=4):
    """Next id such as C0013 given the ids already used."""
    top = 0
    pat = re.compile(r"^" + re.escape(prefix) + r"(\d+)$")
    for x in existing_ids:
        m = pat.match(str(x))
        if m:
            top = max(top, int(m.group(1)))
    return "%s%0*d" % (prefix, width, top + 1)


# ------------------------------------------------------ cards and checks
def current_cards(project):
    """Latest version of every card (cards.jsonl is append-only, one line per version)."""
    best = {}
    for rec in read_jsonl(Path(project) / "cards" / "cards.jsonl"):
        cid = rec.get("card_id")
        if cid is None:
            continue
        if cid not in best or rec.get("version", 1) >= best[cid].get("version", 1):
            best[cid] = rec
    return best


def current_checks(project):
    """Latest check record per (card_id, card_version, check)."""
    best = {}
    for rec in read_jsonl(Path(project) / "checks" / "checks.jsonl"):
        key = (rec.get("card_id"), rec.get("card_version"), rec.get("check"))
        best[key] = rec  # later lines win
    return best


def check_result(checks, card):
    """Dict check-name -> result for the card's current version."""
    out = {}
    for name in CHECK_NAMES:
        rec = checks.get((card["card_id"], card.get("version", 1), name))
        out[name] = rec["result"] if rec else "not_run"
    return out


def load_register(project):
    """Corpus register as {paper_id: record} (latest line per paper wins)."""
    best = {}
    for rec in read_jsonl(Path(project) / "corpus" / "register.jsonl"):
        best[rec["paper_id"]] = rec
    return best


def load_page(project, paper_id, page):
    """Parsed page record, or None."""
    p = Path(project) / "parsed" / paper_id / "pages" / ("p%04d.json" % page)
    return read_json(p)


# ------------------------------------------------- normalisation (located)
_LIGATURES = {
    "ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi",
    "ﬄ": "ffl", "ﬅ": "st", "ﬆ": "st",
}
_QUOTES = {
    "‘": "'", "’": "'", "‚": "'", "‛": "'",
    "“": '"', "”": '"', "„": '"', "‟": '"',
}
_DROP = {"­", "​", "‌", "‍", "﻿", "⁠"}


def normalise_with_map(text, hyphen_mode):
    """Normalise text and return (normalised, map) where map[i] is the offset in the original.

    Only these changes are made, and nothing else:
      - Unicode composed form (NFC), so accents compare equal
      - typographic ligatures expanded (fi, fl, ...)
      - curly quotes and apostrophes straightened
      - soft hyphens and zero-width characters removed
      - every run of white space becomes one space, ends trimmed
      - hyphenation at a line end: mode "join" removes "-" + newline when a
        letter precedes and a lowercase letter follows; mode "keep" removes
        only the newline and keeps the hyphen.
    Digits, signs (including the true minus sign U+2212 and every dash),
    operators, symbols, letter case and negation words are never altered.
    """
    text = unicodedata.normalize("NFC", text)
    out = []
    mp = []
    n = len(text)
    i = 0
    pending_space = False
    space_at = 0
    while i < n:
        ch = text[i]
        if ch in _DROP:
            i += 1
            continue
        if ch == "-" and i + 1 < n and text[i + 1] in "\r\n":
            j = i + 1
            while j < n and text[j] in "\r\n":
                j += 1
            # the line break may be followed by indentation spaces
            k = j
            while k < n and text[k] in " \t":
                k += 1
            prev_letter = bool(out) and out[-1].isalpha() and not pending_space
            next_lower = k < n and text[k].islower()
            if hyphen_mode == "join" and prev_letter and next_lower:
                i = k
                continue
            if hyphen_mode == "keep":
                if pending_space:
                    if out:
                        out.append(" ")
                        mp.append(space_at)
                    pending_space = False
                out.append("-")
                mp.append(i)
                i = k
                continue
        if ch.isspace():
            if not pending_space:
                pending_space = True
                space_at = i
            i += 1
            continue
        if pending_space:
            if out:
                out.append(" ")
                mp.append(space_at)
            pending_space = False
        if ch in _LIGATURES:
            for c in _LIGATURES[ch]:
                out.append(c)
                mp.append(i)
        elif ch in _QUOTES:
            out.append(_QUOTES[ch])
            mp.append(i)
        else:
            out.append(ch)
            mp.append(i)
        i += 1
    return "".join(out), mp


def norm_simple(text):
    """Normalised form used for comparing short strings (keep-hyphen mode)."""
    return normalise_with_map(text, "keep")[0]


def find_quote(page_text, quote):
    """Locate `quote` in `page_text`. Returns (method, start, end) in original offsets, or None.

    method is "exact" when the quote occurs character for character, else
    "normalised" when it occurs after the permitted normalisations only.
    There is no fuzzy matching of any kind.
    """
    if not quote or not quote.strip():
        return None
    pos = page_text.find(quote)
    if pos >= 0:
        return ("exact", pos, pos + len(quote))
    for pmode in ("keep", "join"):
        ptxt, pmap = normalise_with_map(page_text, pmode)
        for qmode in ("keep", "join"):
            qtxt, _ = normalise_with_map(quote, qmode)
            if not qtxt:
                continue
            at = ptxt.find(qtxt)
            if at >= 0:
                start = pmap[at]
                end = pmap[at + len(qtxt) - 1] + 1
                return ("normalised", start, end)
    return None


# ------------------------------------------------------------- statistics
def wilson(k, n, z=1.96):
    """Wilson score interval for k successes in n trials: (low, high). (None, None) if n == 0."""
    if n <= 0:
        return (None, None)
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def fmt_rate(k, n, min_n=30):
    """Format a rate with its denominator. Below min_n only the count is reported."""
    if n == 0:
        return "n/a (0 items)"
    if n < min_n:
        return "%d of %d (fewer than %d items: count only, no percentage)" % (k, n, min_n)
    lo, hi = wilson(k, n)
    return "%d of %d = %.1f%% (95%% interval %.1f to %.1f)" % (k, n, 100.0 * k / n, 100 * lo, 100 * hi)
