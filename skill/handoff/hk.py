#!/usr/bin/env python3
"""hk: handoff kit.

Every check a rule can make is made here, not by a language model. Output is
short by design. Python 3.8+, standard library only, no network except
`git fetch` (run by `hk check`). See SPEC.md for the full specification.
"""

import argparse
import datetime
import fnmatch
import hashlib
import io
import json
import os
import re
import shlex
import socket
import subprocess
import sys
import tarfile
import time


class HkError(Exception):
    """A usage or format error. main() prints it on one line and exits 2."""


DEFAULT_PACK_WARN = 50000  # chars; pack warns above this when budget_pack is unset

DEFAULTS = {
    "handoff": "docs/HANDOFF.md",
    "context_map": "docs/CONTEXT_MAP.md",
    "ledger": "docs/CHECKS.tsv",
    "briefs_dir": "docs/briefs",
    "decisions": "docs/DECISIONS.md",
    "lessons_dir": "docs/lessons",
    "remote": "origin",
    "branch": None,
    "doc_paths": ["docs/", "START_PROMPT.md"],
    "budget_handoff": 12000,
    "budget_boot": 40000,
    "read_budget": 400000,
    "main_check": "full",
    "inline_under": 1000,
    "metrics": "docs/HK_METRICS.tsv",
    "learned": "docs/HK_LEARNED.md",
    "procedure": None,
    "checks": {},
    "machines_check": None,
    "external_refs": [],
    "budget_ref": 0,
    "budget_pack": 0,
    "inline_ref_max": 0,
}

VERSION = "4"
LEARNED_HEADER = ["Id", "Added", "Note", "Evidence", "Last seen"]
LEARNED_MAX_ROWS = 20
LEARNED_MAX_CHARS = 2000
METRICS_COLUMNS = ["time", "machine", "commit", "boot_chars", "handoff_chars", "first_fails",
                   "offmap", "fetched", "unused_when", "budget_level", "coldstart", "learned_ids"]
CHECK_RUNS = ".hk/check_runs.tsv"
CHECK_MEANINGS = {
    "C1": "worktree has uncommitted or untracked changes",
    "C2": "local branch differs from the remote branch",
    "C3": "handoff missing, malformed or over budget",
    "C4": "anchor empty or changed without a recorded decision",
    "C5": "State Main line wrong, stale or without proof",
    "C6": "Next table or its briefs wrong",
    "C7": "a Read first or Read when ref does not resolve",
    "C8": "boot output over budget",
    "C9": "an Exists row is not backed by evidence",
    "C10": "a Parked branch is missing or stale",
    "C11": "a CONTEXT_MAP ref does not resolve",
    "C12": ".hk/ is not git-ignored",
    "C13": "a ledger row was edited by hand",
    "C14": "a secret-like value in the handoff",
    "C15": "a learned file is over its caps or lacks evidence",
}
NAME_RE = re.compile(r"^[A-Za-z0-9._/][A-Za-z0-9._/-]*$")
INT_KEYS = ["budget_handoff", "budget_boot", "read_budget", "inline_under", "budget_ref",
            "budget_pack", "inline_ref_max"]
PATH_KEYS = ["handoff", "context_map", "ledger", "briefs_dir", "decisions", "lessons_dir",
             "metrics", "learned"]
LOCAL_LEDGER = ".hk/ledger.tsv"
READS_LOG = ".hk/reads.tsv"
JOBS_LOG = ".hk/jobs.tsv"
LEDGER_COLUMNS = ["time", "machine", "commit", "dirty", "label", "exit",
                  "secs", "log_sha", "cmd", "row_sha"]

BOOT_SECTIONS = ["Anchor", "State", "Next", "Questions", "How to work",
                 "Read first", "Read when", "Never read"]
DEMAND_SECTIONS = ["Exists", "Parked", "Findings", "Decisions", "Machines"]
ALL_SECTIONS = BOOT_SECTIONS + DEMAND_SECTIONS

TABLE_HEADERS = {
    "Next": ["#", "Step", "Brief", "Model", "Done when"],
    "Questions": ["#", "Question", "Blocks"],
    "Read first": ["Ref"],
    "Read when": ["Trigger", "Ref"],
    "Never read": ["Path"],
    "Exists": ["Piece", "Status", "Evidence", "Paths"],
    "Parked": ["Branch", "Tip", "Verified", "Not verified", "Next"],
    "Findings": ["Sev", "Finding", "File"],
}

ANCHOR_KEYS = ["Goal", "Milestone", "Ends when", "Workflow", "Out of scope",
               "Decides"]
FIXED_ANCHOR_KEYS = ["Goal", "Workflow", "Out of scope", "Decides"]

MAIN_RE = re.compile(
    r"^Main:\s*(\S+)\s*@\s*([0-9a-fA-F]{7,40})\s+check:(pass|fail|not-run)\s*$")
SECRET_RE = re.compile(
    r"(?i)(api[_-]?key|secret|token|password)\s*[:=]\s*\S{8,}")
HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.*?)(?:[ \t]+#+)?[ \t]*$")
EVIDENCE_RE = re.compile(r"^(check:)?([0-9a-fA-F]{7,40})$")

QUIZ = """1. What is the project's goal and current milestone?
2. What is step 1, which brief holds it, and which model does it?
3. Which files or sections do you read before starting, in order?
4. What must you never read?
5. What is the state of the main branch check?
6. How many parked branches and open findings are there, and how did you find out?"""

REPORT_SHAPE = """REPORT (20 lines at most)
status: <word>
branch: <name> @ <sha>
changed: <files>
check: <last lines of the Done-when command>
questions: <none or list>"""


# ---------------------------------------------------------------- git helpers

def git_run(cwd, args):
    """Run git with an argument list in cwd. Return (returncode, stdout bytes, stderr text)."""
    try:
        proc = subprocess.run(["git", "-c", "core.quotepath=off"] + list(args),
                              cwd=cwd, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE)
    except OSError:
        raise HkError("git not found")
    return proc.returncode, proc.stdout, proc.stderr.decode("utf-8", "replace")


def git_out(cwd, args):
    """Run git, raise HkError on failure, return stdout text without the final newline."""
    rc, out, err = git_run(cwd, args)
    if rc != 0:
        first = err.strip().split("\n")[0] if err.strip() else "exit %d" % rc
        raise HkError("git %s failed: %s" % (args[0], first))
    return out.decode("utf-8", "replace").rstrip("\r\n")


def git_lines(cwd, args):
    """Run git and return the non-empty lines of its output (empty list on failure)."""
    rc, out, _ = git_run(cwd, args)
    if rc != 0:
        return []
    text = out.decode("utf-8", "replace")
    return [line.rstrip("\r") for line in text.split("\n") if line.strip()]


def rev_parse(root, rev):
    """Return the full commit sha for a revision, or None if it does not resolve."""
    if rev.startswith("-"):
        return None
    rc, out, _ = git_run(root, ["rev-parse", "--verify", "--quiet", rev + "^{commit}"])
    if rc != 0:
        return None
    return out.decode("utf-8", "replace").strip() or None


def locate_root(cwd=None):
    """Return (root, has_git). Inside a git repository the root is its top folder.

    Outside git it is the nearest ancestor folder holding hk.json or docs/HANDOFF.md."""
    try:
        proc = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=cwd,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if proc.returncode == 0:
            return os.path.normpath(proc.stdout.decode("utf-8", "replace").strip()), True
    except OSError:
        pass
    current = os.path.abspath(cwd or os.getcwd())
    while True:
        if (os.path.isfile(os.path.join(current, "hk.json"))
                or os.path.isfile(os.path.join(current, "docs", "HANDOFF.md"))):
            return current, False
        parent = os.path.dirname(current)
        if parent == current:
            raise HkError("not inside a git repository or a handoff folder")
        current = parent


def need_git(has_git):
    """Raise HkError when a command that needs git runs outside a git repository."""
    if not has_git:
        raise HkError("this command needs a git repository")


def default_branch(root, remote):
    """Pick the integration branch: dev if the remote has it, else the remote's default."""
    if rev_parse(root, "refs/remotes/%s/dev" % remote):
        return "dev"
    rc, out, _ = git_run(root, ["symbolic-ref", "--short",
                                "refs/remotes/%s/HEAD" % remote])
    if rc == 0:
        name = out.decode("utf-8", "replace").strip()
        prefix = remote + "/"
        if name.startswith(prefix):
            return name[len(prefix):]
    for candidate in ("main", "master"):
        if rev_parse(root, "refs/remotes/%s/%s" % (remote, candidate)):
            return candidate
    return git_out(root, ["rev-parse", "--abbrev-ref", "HEAD"])


def valid_name(value):
    """True if a remote or branch name is safe to hand to git (no leading '-', no '..')."""
    return (isinstance(value, str) and NAME_RE.match(value) is not None
            and ".." not in value)


def parse_config(text):
    """Parse hk.json text. Return (cfg, errors); a bad key keeps its default and adds an error."""
    cfg = dict(DEFAULTS)
    errors = []
    if text is None:
        return cfg, errors
    try:
        data = json.loads(text)
    except ValueError:
        return cfg, ["hk.json is not valid JSON"]
    if not isinstance(data, dict):
        return cfg, ["hk.json must hold an object"]
    for key, value in data.items():
        if key not in DEFAULTS:
            continue
        problem = None
        if key in ("remote", "branch"):
            if not valid_name(value):
                problem = "must be a safe name matching %s" % NAME_RE.pattern
        elif key == "doc_paths":
            if not (isinstance(value, list) and value
                    and all(isinstance(v, str) and v for v in value)):
                problem = "must be a non-empty list of strings"
        elif key in INT_KEYS:
            if not (isinstance(value, int) and not isinstance(value, bool) and value >= 0):
                problem = "must be a non-negative integer"
        elif key == "external_refs":
            if not (isinstance(value, list)
                    and all(isinstance(v, str) and os.path.isabs(v) for v in value)):
                problem = "must be a list of absolute paths"
        elif key == "checks":
            if not (isinstance(value, dict) and all(
                    isinstance(k, str) and k and isinstance(v, str) and v
                    for k, v in value.items())):
                problem = "must map label to command string"
        elif key == "procedure":
            if isinstance(value, str) and value:
                pass
            elif (isinstance(value, dict) and isinstance(value.get("path"), str)
                  and value["path"]
                  and (value.get("lines") is None
                       or (isinstance(value["lines"], str)
                           and re.match(r"^\d+-\d+$", value["lines"])))):
                pass
            else:
                problem = 'must be a path or {"path": ..., "lines": "a-b"}'
        elif not (isinstance(value, str) and value):
            problem = "must be a non-empty string"
        elif key in PATH_KEYS:
            try:
                check_ref_path(value, norm_path(value))
            except HkError as err:
                problem = str(err)
        if problem:
            errors.append("hk.json %s: %s" % (key, problem))
        else:
            cfg[key] = value
    return cfg, errors


def load_config(root, has_git=True):
    """Return the config from hk.json in the working tree; raise HkError if it is bad."""
    cfg, errors = parse_config(read_at(root, "hk.json", None))
    if errors:
        raise HkError(errors[0])
    if not cfg.get("branch"):
        cfg["branch"] = default_branch(root, cfg["remote"]) if has_git else "main"
    cfg["_git"] = has_git
    return cfg


def setup(cwd=None):
    """Find the root and load its config. Return (root, cfg); cfg['_git'] says if git is available."""
    root, has_git = locate_root(cwd)
    return root, load_config(root, has_git)


def norm_path(path):
    """Normalise a repository path: forward slashes, no leading ./."""
    path = path.strip().replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    return path


def check_safe_path(root, rel):
    """Refuse a working-tree path that passes through a symlink or whose real path leaves the root."""
    current = root
    for part in rel.split("/"):
        if part:
            current = os.path.join(current, part)
            if os.path.islink(current):
                raise HkError("refused: %s is or passes through a symlink" % rel)
    real_root = os.path.realpath(root)
    real = os.path.realpath(current)
    if real != real_root and not real.startswith(real_root + os.sep):
        raise HkError("refused: %s leaves the repository" % rel)


def read_at(root, path, gitref):
    """Return the text of a file, from the working tree (gitref None) or a git ref. None if absent."""
    path = norm_path(path)
    if gitref is None:
        check_safe_path(root, path)
        full = os.path.join(root, *path.split("/"))
        if not os.path.isfile(full):
            return None
        with open(full, "r", encoding="utf-8", errors="replace", newline="") as fh:
            text = fh.read()
    else:
        if gitref.startswith("-"):
            raise HkError("bad git ref '%s'" % gitref)
        rc, data, _ = git_run(root, ["cat-file", "blob", "%s:%s" % (gitref, path)])
        if rc != 0:
            return None
        text = data.decode("utf-8", "replace")
    return text.replace("\r\n", "\n")


def worktree_paths(root):
    """Return the paths of all non-bare worktrees of the repository."""
    paths = []
    current = None
    bare = False
    for line in git_lines(root, ["worktree", "list", "--porcelain"]) + ["worktree "]:
        if line.startswith("worktree "):
            if current is not None and not bare:
                paths.append(current)
            current = line[len("worktree "):]
            bare = False
        elif line == "bare":
            bare = True
    return [p for p in paths if p]


def worktree_of_branch(root, branch):
    """Return the path of the worktree that has the branch checked out, or None."""
    path = None
    for line in git_lines(root, ["worktree", "list", "--porcelain"]):
        if line.startswith("worktree "):
            path = line[len("worktree "):]
        elif line == "branch refs/heads/%s" % branch:
            return path
    return None


def status_count(path):
    """Count dirty paths in a worktree: porcelain lines (all untracked files) plus hidden-change flags."""
    count = len(git_lines(path, ["status", "--porcelain", "--untracked-files=all"]))
    for line in git_lines(path, ["ls-files", "-v"]):
        flag = line[:1]
        if flag == "S" or flag.islower():
            count += 1
    return count


def short(sha):
    """Return the first 7 characters of a sha, or 'none'."""
    return sha[:7] if sha else "none"


def now_utc():
    """Return the current UTC time as ISO seconds, e.g. 2026-11-19T10:00:00Z."""
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ------------------------------------------------------------ text and refs

def parse_headings(lines):
    """Return (line index, level, text) for each markdown heading, skipping code fences."""
    heads = []
    in_fence = False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        match = HEADING_RE.match(line)
        if match:
            heads.append((i, len(match.group(1)), match.group(2).strip()))
    return heads


def trim_blank_tail(lines):
    """Drop blank lines from the end of a list of lines."""
    lines = list(lines)
    while lines and not lines[-1].strip():
        lines.pop()
    return lines


def extract_section(text, wanted):
    """Return the section whose heading equals (case-insensitive) or uniquely starts with wanted."""
    want = wanted.strip().lower()
    if not want:
        raise HkError("empty heading in ref")
    lines = text.split("\n")
    heads = parse_headings(lines)
    exact = [h for h in heads if h[2].lower() == want]
    if len(exact) > 1:
        raise HkError("heading '%s' matches %d sections" % (wanted, len(exact)))
    if len(exact) == 1:
        chosen = exact[0]
    else:
        prefix = [h for h in heads if h[2].lower().startswith(want)]
        if len(prefix) == 0:
            raise HkError("heading '%s' not found" % wanted)
        if len(prefix) > 1:
            raise HkError("heading '%s' is ambiguous (%d matches)" % (wanted, len(prefix)))
        chosen = prefix[0]
    start, level = chosen[0], chosen[1]
    end = len(lines)
    for head in heads:
        if head[0] > start and head[1] <= level:
            end = head[0]
            break
    return "\n".join(trim_blank_tail(lines[start:end]))


def split_ref(refstr):
    """Split a ref into (path, kind, arg); kind is 'file', 'section' or 'tail'."""
    refstr = refstr.strip().strip("`").strip()
    match = re.match(r"^(.*)@tail:(\d+)$", refstr)
    if match:
        return norm_path(match.group(1)), "tail", int(match.group(2))
    if "#" in refstr:
        path, heading = refstr.split("#", 1)
        return norm_path(path), "section", heading
    return norm_path(refstr), "file", None


def check_ref_path(refstr, path):
    """Refuse a ref path that is empty, absolute, has a '..' segment or starts with '-'."""
    if not path:
        raise HkError("ref '%s' has no path" % refstr)
    if path.startswith("/") or re.match(r"^[A-Za-z]:", path) or os.path.isabs(path):
        raise HkError("ref '%s': absolute path not allowed" % refstr)
    if ".." in path.split("/"):
        raise HkError("ref '%s': '..' not allowed" % refstr)
    if path.startswith("-"):
        raise HkError("ref '%s': path must not start with '-'" % refstr)


def parse_ref(root, refstr, cfg=None):
    """Parse a ref. Return (branch gitref or None, path, kind, arg); raise HkError if unsafe.

    A ref starting with '<branch>:' is branch-qualified when that branch exists on the remote."""
    text = refstr.strip().strip("`").strip()
    forced = None
    head = re.split(r"[#@]", text, maxsplit=1)[0]
    if cfg is not None and os.path.isabs(head) and norm_path(head) in [
            norm_path(e) for e in cfg.get("external_refs", [])]:
        path, kind, arg = split_ref(text)
        return None, path, kind, arg
    if cfg is not None and ":" in head:
        if not cfg.get("_git", True):
            raise HkError("ref '%s': branch-qualified refs need a git repository" % refstr)
        name, rest = text.split(":", 1)
        full = "refs/remotes/%s/%s" % (cfg["remote"], name)
        if valid_name(name) and rev_parse(root, full):
            forced = full
            text = rest
    path, kind, arg = split_ref(text)
    check_ref_path(refstr, path)
    return forced, path, kind, arg


class ExternalMissing(HkError):
    """A cited external_refs file does not exist on this machine (a warning, not a failure)."""


def read_external(path):
    """Return the text of an external_refs file; raise ExternalMissing if it is absent."""
    if not os.path.isfile(path):
        raise ExternalMissing("external ref %s not found on this machine" % path)
    with open(path, "r", encoding="utf-8", errors="replace", newline="") as fh:
        return fh.read().replace("\r\n", "\n")


def is_external(cfg, path):
    """True if path is an absolute file listed in cfg external_refs."""
    return (cfg is not None and os.path.isabs(path)
            and norm_path(path) in [norm_path(e) for e in cfg.get("external_refs", [])])


def resolve_ref(root, refstr, gitref, cfg=None):
    """Return (path, content) for a ref; raise HkError if it does not resolve."""
    forced, path, kind, arg = parse_ref(root, refstr, cfg)
    if is_external(cfg, path):
        text = read_external(path)
        if kind == "file":
            return path, text
        if kind == "tail":
            lines = text.split("\n")
            if lines and lines[-1] == "":
                lines.pop()
            return path, "\n".join(lines[-arg:] if arg > 0 else [])
        try:
            return path, extract_section(text, arg)
        except HkError as err:
            raise HkError("ref '%s': %s" % (refstr, err))
    if forced is not None:
        gitref = forced
    text = read_at(root, path, gitref)
    if text is None:
        raise HkError("ref '%s': file not found" % refstr)
    if gitref is not None:
        listing = git_lines(root, ["ls-tree", gitref, "--", path])
        if listing and listing[0].startswith("120000 "):
            raise HkError("ref '%s': symlink not allowed" % refstr)
    if kind == "file":
        return path, text
    if kind == "tail":
        lines = text.split("\n")
        if lines and lines[-1] == "":
            lines.pop()
        return path, "\n".join(lines[-arg:] if arg > 0 else [])
    try:
        return path, extract_section(text, arg)
    except HkError as err:
        raise HkError("ref '%s': %s" % (refstr, err))


def ref_path(root, cfg, refstr):
    """Return only the file path of a ref (the raw text if the ref is unsafe)."""
    try:
        return parse_ref(root, refstr, cfg)[1]
    except HkError:
        return norm_path(refstr)


def glob_match(pattern, path):
    """Match a forward-slash path against a glob; a trailing /** means everything under that folder."""
    pattern = norm_path(pattern)
    path = norm_path(path)
    if pattern.endswith("/**"):
        return path.startswith(pattern[:-2])
    return fnmatch.fnmatchcase(path, pattern)


def literal_prefix(pattern):
    """Return the part of a glob before its first wildcard character."""
    pattern = norm_path(pattern)
    cut = len(pattern)
    for ch in "*?[":
        pos = pattern.find(ch)
        if pos != -1 and pos < cut:
            cut = pos
    return pattern[:cut]


def globs_overlap(a, b):
    """True if two globs could cover a common path (equal, or one matches the other's literal prefix)."""
    a = norm_path(a)
    b = norm_path(b)
    if a == b:
        return True
    return glob_match(a, literal_prefix(b)) or glob_match(b, literal_prefix(a))


def split_list(value, sep):
    """Split a cell on sep, strip backticks and blanks, drop empty items."""
    items = [item.replace("`", "").strip() for item in value.split(sep)]
    return [item for item in items if item]


# -------------------------------------------------------- handoff and tables

def split_row(line):
    """Split one markdown table row into cells, with backticks removed."""
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|") and not line.endswith("\\|"):
        line = line[:-1]
    cells = re.split(r"(?<!\\)\|", line)
    return [c.replace("\\|", "|").replace("`", "").strip() for c in cells]


def parse_table(lines, expected_header):
    """Parse a markdown table. Return (rows, errors); rows are lists of cell strings."""
    errors = []
    table_lines = [l.strip() for l in lines if l.strip().startswith("|")]
    if len(table_lines) < 2:
        return [], ["table header or separator missing"]
    header = split_row(table_lines[0])
    if [h.lower() for h in header] != [h.lower() for h in expected_header]:
        errors.append("header is '%s', expected '%s'"
                      % (" | ".join(header), " | ".join(expected_header)))
    if not re.match(r"^\|?[\s:\-|]+\|?$", table_lines[1]) or "-" not in table_lines[1]:
        errors.append("separator row missing")
    rows = []
    for number, line in enumerate(table_lines[2:], start=1):
        cells = split_row(line)
        if len(cells) != len(expected_header):
            errors.append("row %d has %d cells, expected %d"
                          % (number, len(cells), len(expected_header)))
            continue
        if any(cells):
            rows.append(cells)
    return rows, errors


def parse_handoff(text):
    """Parse a handoff. Return (sections, errors); sections maps name to {'heading', 'body'}."""
    lines = text.split("\n")
    heads = parse_headings(lines)
    errors = []
    sections = {}
    if not any(h[1] == 1 and h[2].lower() == "handoff" for h in heads):
        errors.append("title '# Handoff' missing")
    top = [h for h in heads if h[1] <= 2]
    for pos, head in enumerate(top):
        index, level, name = head
        if level != 2:
            continue
        end = top[pos + 1][0] if pos + 1 < len(top) else len(lines)
        if name in sections:
            errors.append("duplicate section '%s'" % name)
        elif name not in ALL_SECTIONS:
            errors.append("unknown section '%s'" % name)
        else:
            sections[name] = {"heading": lines[index], "body": lines[index + 1:end]}
    for name in ALL_SECTIONS:
        if name not in sections:
            errors.append("section '%s' missing" % name)
    in_file_order = []
    for head in top:
        if head[1] == 2 and head[2] in ALL_SECTIONS and head[2] not in in_file_order:
            in_file_order.append(head[2])
    expected_order = [n for n in ALL_SECTIONS if n in in_file_order]
    if in_file_order != expected_order:
        errors.append("sections out of order: found %s" % ", ".join(in_file_order))
    for name, header in TABLE_HEADERS.items():
        if name in sections:
            for line in sections[name]["body"]:
                if line.strip() and not line.strip().startswith("|"):
                    errors.append("%s: line is not a table row: %s" % (name, line.strip()[:40]))
            _, table_errors = parse_table(sections[name]["body"], header)
            for err in table_errors:
                errors.append("%s: %s" % (name, err))
    errors.extend(duplicate_errors(sections))
    if "State" in sections:
        info = state_info(sections)
        if info["extra"] > 10:
            errors.append("State: more than 10 free lines")
    return sections, errors


def duplicate_errors(sections):
    """Return errors for a repeated Anchor key, a second Main: line or a second Anchor change: line."""
    errors = []
    if "Anchor" in sections:
        counts = {}
        for line in sections["Anchor"]["body"]:
            match = re.match(r"^([A-Za-z ]+?):[ \t]*(.*)$", line.strip())
            if match and match.group(1) in ANCHOR_KEYS:
                counts[match.group(1)] = counts.get(match.group(1), 0) + 1
        for key in ANCHOR_KEYS:
            if counts.get(key, 0) > 1:
                errors.append("Anchor: '%s' appears %d times" % (key, counts[key]))
    if "State" in sections:
        body = [l.strip() for l in sections["State"]["body"]]
        if len([l for l in body if l.startswith("Main:")]) > 1:
            errors.append("State: more than one Main line")
        if len([l for l in body if l.startswith("Anchor change:")]) > 1:
            errors.append("State: more than one Anchor change line")
    return errors


def section_rows(sections, name):
    """Return the table rows of a section (empty list if the section is missing or malformed)."""
    if name not in sections:
        return []
    rows, _ = parse_table(sections[name]["body"], TABLE_HEADERS[name])
    return rows


def section_text(sections, name):
    """Return the body text of a section without surrounding blank lines."""
    if name not in sections:
        return ""
    return "\n".join(trim_blank_tail(sections[name]["body"])).strip("\n")


def anchor_values(sections):
    """Return a dict of the Anchor keys found in the handoff (missing keys are absent)."""
    values = {}
    if "Anchor" not in sections:
        return values
    for line in sections["Anchor"]["body"]:
        match = re.match(r"^([A-Za-z ]+?):[ \t]*(.*)$", line.strip())
        if match and match.group(1) in ANCHOR_KEYS:
            values[match.group(1)] = match.group(2).strip()
    return values


def state_info(sections):
    """Return the State facts: main_line (first), main_lines, match (or None), anchor_change id, extra count."""
    info = {"main_line": "", "main_lines": [], "match": None, "anchor_change": "", "extra": 0,
            "running": []}
    if "State" not in sections:
        return info
    for line in sections["State"]["body"]:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("Main:"):
            info["main_lines"].append(stripped)
            if not info["main_line"]:
                info["main_line"] = stripped
        elif stripped.startswith("Running:"):
            info["running"].append(stripped[len("Running:"):].strip())
        elif stripped.startswith("Anchor change:"):
            if not info["anchor_change"]:
                info["anchor_change"] = stripped[len("Anchor change:"):].strip()
        else:
            info["extra"] += 1
    if info["main_line"]:
        info["match"] = MAIN_RE.match(info["main_line"])
    return info


def load_handoff(root, cfg, gitref):
    """Read and parse the handoff from the tree or a git ref. Return (text, sections, errors)."""
    text = read_at(root, cfg["handoff"], gitref)
    if text is None:
        raise HkError("handoff %s not found" % cfg["handoff"])
    sections, errors = parse_handoff(text)
    return text, sections, errors


# ------------------------------------------------------------------- briefs

def brief_path(cfg, name):
    """Turn a brief cell or argument (path or bare id) into a repository path."""
    name = norm_path(name.replace("`", ""))
    if name.endswith(".md") or "/" in name:
        return name
    return "%s/%s.md" % (cfg["briefs_dir"].rstrip("/"), name)


def check_pipe_lines(lines, label):
    """Raise HkError for a line holding ' | ' that does not start with '|' (a broken table row).

    Only for files that are tables (the context map). Briefs are prose, so they never use it.
    Lines inside code fences are ignored."""
    in_fence = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            continue
        if in_fence or " | " not in line or stripped.startswith("|"):
            continue
        raise HkError("%s: line is not a table row: %s" % (label, stripped[:40]))


def parse_brief(text):
    """Parse a brief. Return a dict with id, model, branch, touch, done, read; raise HkError."""
    lines = text.split("\n")
    match = re.match(r"^# Brief:\s*(\S.*?)\s*$", lines[0]) if lines else None
    if not match:
        raise HkError("brief: first line must be '# Brief: <id>'")
    fields = {}
    has_task = False
    first_heading = None
    for line in lines[1:]:
        if line.startswith("## "):
            if line.strip() == "## Task":
                has_task = True
            else:
                first_heading = line.strip()
            break
        field = re.match(r"^(Model|Branch|Base|Touch|Done when|Read):[ \t]*(.*?)\s*$", line)
        if field:
            fields[field.group(1)] = field.group(2)
    for key in ("Model", "Branch", "Touch", "Done when"):
        if not fields.get(key):
            raise HkError("brief %s: field '%s' missing or empty" % (match.group(1), key))
    if not has_task:
        if first_heading:
            raise HkError("brief %s: '## Task' must be the first '##' section; the first is '%s'"
                          % (match.group(1), first_heading))
        raise HkError("brief %s: '## Task' section missing" % match.group(1))
    done = [c.strip() for c in re.findall(r"`([^`]*)`", fields["Done when"]) if c.strip()]
    if not done:
        raise HkError("brief %s: 'Done when' has no backticked command" % match.group(1))
    branch = fields["Branch"].replace("`", "").strip()
    if not valid_name(branch):
        raise HkError("brief %s: bad branch name '%s'" % (match.group(1), branch))
    touch = split_list(fields["Touch"], ",")
    for item in touch:
        if re.search(r"[\s()\"'`;|<>]", item):
            raise HkError("brief %s: Touch item '%s' is not a glob (globs only, comma-separated)"
                          % (match.group(1), item))
    return {
        "id": match.group(1),
        "model": fields["Model"],
        "branch": branch,
        "base": fields.get("Base", "").replace("`", "").strip(),
        "touch": touch,
        "done": done,
        "read": split_list(fields.get("Read", ""), ";"),
    }


def load_brief(root, cfg, name, gitref):
    """Read and parse a brief by path or id. Return (path, text, parsed)."""
    candidate = name.strip()
    if gitref is None and os.path.isfile(candidate):
        rel = os.path.relpath(os.path.abspath(candidate), root)
        path = norm_path(rel)
    else:
        path = brief_path(cfg, candidate)
    text = read_at(root, path, gitref)
    if text is None:
        raise HkError("brief %s not found" % path)
    return path, text, parse_brief(text)


# --------------------------------------------------------------- context map

def load_context_map(root, cfg, gitref):
    """Return the CONTEXT_MAP rows as dicts (paths, read, lessons); empty if no map exists."""
    text = read_at(root, cfg["context_map"], gitref)
    if text is None:
        return []
    check_pipe_lines(text.split("\n"), "context map")
    rows, errors = parse_table(text.split("\n"), ["Paths", "Read", "Lessons"])
    if errors:
        raise HkError("context map: %s" % errors[0])
    result = []
    for cells in rows:
        result.append({"paths": split_list(cells[0], ","),
                       "read": split_list(cells[1], ";"),
                       "lessons": split_list(cells[2], ",")})
    return result


def map_row_refs(root, cfg, row, gitref):
    """Return the refs of a map row: its Read refs, then each existing lesson file."""
    refs = list(row["read"])
    for area in row["lessons"]:
        lesson = "%s/%s.md" % (cfg["lessons_dir"].rstrip("/"), area)
        if read_at(root, lesson, gitref) is not None:
            refs.append(lesson)
    return refs


def dedupe(items):
    """Remove duplicates from a list, keeping the first occurrence of each."""
    seen = set()
    result = []
    for item in items:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


# -------------------------------------------------------------------- ledger

def sha12(text):
    """Return the first 12 hex characters of the sha256 of a text."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def row_sha_of(fields):
    """Return the row_sha for the nine fields before row_sha."""
    return sha12("\t".join(fields))


def parse_ledger(text):
    """Return the ledger rows (lists of fields), skipping the header and blank lines."""
    rows = []
    for line in text.split("\n"):
        line = line.rstrip("\r")
        if not line.strip() or line.startswith("time\t"):
            continue
        rows.append(line.split("\t"))
    return rows


def row_valid(fields):
    """True if a ledger row has ten fields and its row_sha recomputes."""
    return len(fields) == 10 and row_sha_of(fields[:9]) == fields[9]


def is_pass_row(fields, commit, label=None):
    """True if a row is a pass row for the full commit sha (and label, when given)."""
    if not row_valid(fields):
        return False
    if fields[2] != commit or fields[5] != "0" or fields[3] != "0":
        return False
    return label is None or fields[4] == label


def append_ledger(root, rel_path, fields):
    """Append one row to a ledger file, writing the header first if the file is new."""
    full = os.path.join(root, *rel_path.split("/"))
    folder = os.path.dirname(full)
    if folder and not os.path.isdir(folder):
        os.makedirs(folder)
    new = not os.path.isfile(full) or os.path.getsize(full) == 0
    with open(full, "a", encoding="utf-8", newline="\n") as fh:
        if new:
            fh.write("\t".join(LEDGER_COLUMNS) + "\n")
        fh.write("\t".join(fields) + "\n")


def clean_field(value):
    """Replace tabs and newlines with spaces so a field fits in one ledger cell."""
    return re.sub(r"[\t\r\n]", " ", value)


# ------------------------------------------------------------------ run/exec

def join_command(parts):
    """Join command words into one shell string (a single word is used as is)."""
    if len(parts) == 1:
        return parts[0]
    if os.name == "nt":
        return subprocess.list2cmdline(parts)
    return " ".join(shlex.quote(p) for p in parts)


def execute_run(root, label, command, record, ledger_path):
    """Run a command through the shell at the root, log it, add a ledger row.

    Return (exit code, output text, RESULT line)."""
    dirty = 1 if status_count(root) > 0 else 0
    commit = rev_parse(root, "HEAD") or "none"
    started = time.time()
    try:
        proc = subprocess.run(command, shell=True, cwd=root,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    except OSError as err:
        raise HkError("cannot run command: %s" % err)
    secs = "%.1f" % (time.time() - started)
    output = proc.stdout.decode("utf-8", "replace")
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    safe_label = re.sub(r"[^A-Za-z0-9._-]", "_", label)
    log_rel = ".hk/logs/%s-%s.log" % (stamp, safe_label)
    log_full = os.path.join(root, *log_rel.split("/"))
    if not os.path.isdir(os.path.dirname(log_full)):
        os.makedirs(os.path.dirname(log_full))
    with open(log_full, "w", encoding="utf-8", newline="") as fh:
        fh.write(output)
    machine = os.environ.get("HK_MACHINE") or socket.gethostname()
    fields = [now_utc(), clean_field(machine), commit, str(dirty),
              clean_field(label), str(proc.returncode), secs,
              sha12(output), clean_field(command)]
    fields.append(row_sha_of(fields))
    append_ledger(root, ledger_path if record else LOCAL_LEDGER, fields)
    verdict = "pass" if proc.returncode == 0 else "fail"
    result = "RESULT %s exit=%d commit=%s dirty=%d secs=%s log=%s" % (
        verdict, proc.returncode, short(commit), dirty, secs, log_rel)
    return proc.returncode, output, result


# -------------------------------------------------------------------- output

def set_utf8():
    """Make stdout and stderr write UTF-8 with plain newlines, where the Python version allows."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", newline="\n")
        except (AttributeError, ValueError):
            pass


def emit(text):
    """Write text to stdout, ending with exactly one newline."""
    sys.stdout.write(text.rstrip("\n") + "\n")


def ref_block(ref, content):
    """Return a '=== ref (n chars)' line followed by the content."""
    return "=== %s (%d chars)\n%s" % (ref, len(content), content)


def listed_ref_block(ref, content, limit, prefix):
    """Return a '=== ref NOT INLINED' block: the size, how to read parts, and the Markdown headings."""
    file_part = ref.split("#", 1)[0]
    headings = [line for line in content.split("\n") if re.match(r"#{1,3} ", line)]
    lines = ["=== %s (%d chars) NOT INLINED (over inline_ref_max %d)" % (ref, len(content), limit),
             "Read only the parts your task needs: `%s get %s#<heading>` for a section, "
             "or your file tools for code." % (prefix, file_part)]
    if headings:
        lines.append("Headings:")
        lines.extend(headings[:60])
    return "\n".join(lines)


# ------------------------------------------------------------------ commands

def hk_command(root):
    """Return the command prefix that runs this hk.py as it must be typed, e.g. python3 .claude/skills/handoff/hk.py."""
    script = os.path.abspath(__file__)
    try:
        rel = os.path.relpath(script, root)
        if not rel.startswith("..") and not os.path.isabs(rel):
            script = rel
    except ValueError:
        pass
    script = script.replace("\\", "/")
    if os.name == "nt":
        interpreter = "python"
        if " " in script:
            script = '"%s"' % script
    else:
        interpreter = "python3"
        script = shlex.quote(script)
    return "%s %s" % (interpreter, script)


def build_boot(root, cfg, gitref, strict):
    """Build the boot output. Return (chunks, sizes, total chars).

    chunks are text pieces joined by newlines; sizes are (name, chars) pairs.
    With strict False, refs that do not resolve are skipped."""
    text, sections, errors = load_handoff(root, cfg, gitref)
    if strict and errors:
        raise HkError("handoff malformed: %s" % errors[0])
    blocks = []
    for name in BOOT_SECTIONS:
        if name in sections:
            body = trim_blank_tail(sections[name]["body"])
            blocks.append("\n".join([sections[name]["heading"]] + body))
    boot_part = "\n\n".join(blocks)
    chunks = [boot_part]
    sizes = [("boot sections", len(boot_part))]
    for cells in section_rows(sections, "Read first"):
        ref = cells[0]
        try:
            _, content = resolve_ref(root, ref, gitref, cfg)
        except ExternalMissing as err:
            warn = "WARN %s" % err
            chunks.append(warn)
            sizes.append((ref, len(warn)))
            continue
        except HkError:
            if strict:
                raise
            continue
        block = ref_block(ref, content)
        chunks.append(block)
        sizes.append((ref, len(block)))
    index_lines = []
    command = hk_command(root)
    for name in DEMAND_SECTIONS:
        if name not in sections:
            continue
        body = section_text(sections, name)
        if len(body) < cfg["inline_under"]:
            block = "\n".join([sections[name]["heading"]] + trim_blank_tail(sections[name]["body"]))
            chunks.append(block)
            sizes.append(("inline " + name, len(block)))
            continue
        if name in TABLE_HEADERS:
            count = len(section_rows(sections, name))
        else:
            count = len([l for l in body.split("\n") if l.strip()])
        index_lines.append("%s: %d rows, %d chars \u2014 %s get %s#%s"
                           % (name, count, len(body), command, cfg["handoff"], name))
    if index_lines:
        index_part = "\n".join(
            ["NOT LOADED. Fetch a section when a step or question needs it:"] + index_lines)
        chunks.append(index_part)
        sizes.append(("index", len(index_part)))
    total = len("\n".join(chunks)) + 1
    return chunks, sizes, total


def cmd_boot(args):
    """hk boot: print the boot sections, Read-first files and index, or only sizes."""
    root, cfg = setup()
    if args.ref:
        need_git(cfg["_git"])
    chunks, sizes, total = build_boot(root, cfg, args.ref, True)
    budget = cfg["budget_boot"]
    if args.measure:
        for name, size in sizes:
            emit("%s: %d" % (name, size))
    else:
        emit("\n".join(chunks))
    emit("BOOT %d chars, budget %d" % (total, budget))
    return 1 if total > budget else 0


def log_read(root, path, chars, source="hk", session=None, ref=""):
    """Append one row to the read log .hk/reads.tsv (time session source path chars ref)."""
    full = os.path.join(root, *READS_LOG.split("/"))
    if not os.path.isdir(os.path.dirname(full)):
        os.makedirs(os.path.dirname(full))
    new = not os.path.isfile(full) or os.path.getsize(full) == 0
    if session is None:
        session = os.environ.get("HK_SESSION") or "-"
    with open(full, "a", encoding="utf-8", newline="\n") as fh:
        if new:
            fh.write("time\tsession\tsource\tpath\tchars\tref\n")
        fh.write("\t".join([now_utc(), session, source, path, str(chars),
                            clean_field(ref)]) + "\n")


def never_read_entries(root, cfg):
    """Return (Never-read entries, exact file paths cited by Read first / Read when) from the working-tree handoff."""
    text = read_at(root, cfg["handoff"], None)
    if text is None:
        return [], set()
    sections, _ = parse_handoff(text)
    entries = [cells[0] for cells in section_rows(sections, "Never read") if cells[0]]
    cited = set()
    for name in ("Read first", "Read when"):
        for cells in section_rows(sections, name):
            cited.add(ref_path(root, cfg, cells[-1]))
    return entries, cited


def is_never_read(path, entries, cited):
    """True if a path is under a Never-read entry and not cited exactly by a Read first/Read when ref."""
    path = norm_path(path)
    if path in cited:
        return False
    for entry in entries:
        entry = norm_path(entry)
        if not entry:
            continue
        probes = [path, path + "/"]
        if entry.endswith("/"):
            if any(q.startswith(entry) for q in probes):
                return True
        elif any(glob_match(entry, q) for q in probes):
            return True
        elif not any(ch in entry for ch in "*?[") and path.startswith(entry + "/"):
            return True
    return False


def cmd_get(args):
    """hk get: print each ref's content and log the read."""
    root, cfg = setup()
    if args.ref:
        need_git(cfg["_git"])
    entries, cited = never_read_entries(root, cfg)
    for ref in args.refs:
        path = parse_ref(root, ref, cfg)[1]
        if is_never_read(path, entries, cited):
            raise HkError("refused: %s is on the Never read list" % path)
    results = []
    for ref in args.refs:
        try:
            results.append((ref,) + resolve_ref(root, ref, args.ref, cfg))
        except ExternalMissing as err:
            emit("WARN %s" % err)
    for ref, path, content in results:
        emit(ref_block(ref, content))
        log_read(root, path, len(content), ref=ref)
    return 0


def cmd_for(args):
    """hk for: list the reading the context map requires for the given paths."""
    root, cfg = setup()
    rows = load_context_map(root, cfg, None)
    paths = [norm_path(p) for p in args.paths]
    refs = []
    for row in rows:
        if any(glob_match(g, p) for g in row["paths"] for p in paths):
            refs.extend(map_row_refs(root, cfg, row, None))
    refs = dedupe(refs)
    if not refs:
        return 0
    total = 0
    out = []
    for ref in refs:
        try:
            _, content = resolve_ref(root, ref, None, cfg)
        except ExternalMissing as err:
            out.append("WARN %s" % err)
            continue
        total += len(content)
        if args.print:
            out.append(ref_block(ref, content))
        else:
            out.append("%s (%d chars)" % (ref, len(content)))
    out.append("TOTAL %d chars" % total)
    emit("\n".join(out))
    return 0


def procedure_block(root, cfg, warns):
    """Return the hk.json procedure text as a ref block, or '' (a WARN when an absolute file is absent)."""
    proc = cfg.get("procedure")
    if not proc:
        return ""
    if isinstance(proc, str):
        path, lines = proc, None
    else:
        path, lines = proc["path"], proc.get("lines")
    if os.path.isabs(path):
        try:
            text = read_external(path)
        except ExternalMissing as err:
            warns.append("WARN procedure: %s" % err)
            return ""
    else:
        check_ref_path(path, norm_path(path))
        text = read_at(root, path, None)
        if text is None:
            raise HkError("procedure file %s not found" % path)
    label = "procedure %s" % path
    if lines:
        first, last = [int(n) for n in lines.split("-")]
        text = "\n".join(text.split("\n")[max(first - 1, 0):last])
        label += " lines %s" % lines
    return ref_block(label, text)


def pack_size_warning(total, limit, sizes, map_refs):
    """Return one WARN line naming the three biggest auto-added refs, so the lead can narrow Touch."""
    auto = sorted(((size, ref) for ref, size in sizes if ref in map_refs), reverse=True)
    biggest = ", ".join("%s (%d)" % (ref, size) for size, ref in auto[:3])
    return "WARN pack %d chars > %d; biggest map refs: %s" % (total, limit, biggest or "none")


def cmd_pack(args):
    """hk pack: print a brief, the anchor lines, its reading and the report shape."""
    root, cfg = setup()
    path, brief_text, brief = load_brief(root, cfg, args.brief, None)
    _, sections, _ = load_handoff(root, cfg, None)
    anchor = anchor_values(sections)
    anchor_text = "\n".join("%s: %s" % (k, anchor.get(k, ""))
                            for k in ("Goal", "Workflow", "Out of scope"))
    map_refs = []
    for row in load_context_map(root, cfg, None):
        if any(globs_overlap(g, t) for g in row["paths"] for t in brief["touch"]):
            map_refs.extend(map_row_refs(root, cfg, row, None))
    refs = dedupe(map_refs + brief["read"])
    chunks = [brief_text.rstrip("\n"), anchor_text]
    sizes = [("brief", len(brief_text)), ("anchor", len(anchor_text))]
    warns = []
    for ref in refs:
        try:
            _, content = resolve_ref(root, ref, None, cfg)
        except ExternalMissing as err:
            warns.append("WARN %s" % err)
            continue
        if cfg["inline_ref_max"] and len(content) > cfg["inline_ref_max"]:
            block = listed_ref_block(ref, content, cfg["inline_ref_max"], hk_command(root))
            chunks.append(block)
            sizes.append((ref, len(block)))
            continue
        block = ref_block(ref, content)
        if cfg["budget_ref"] and len(block) > cfg["budget_ref"]:
            warns.append("WARN ref %s is %d chars > budget_ref %d; cite a file#section"
                         % (ref, len(block), cfg["budget_ref"]))
        chunks.append(block)
        sizes.append((ref, len(block)))
    block = procedure_block(root, cfg, warns)
    if block:
        chunks.append(block)
        sizes.append(("procedure", len(block)))
    chunks.append(REPORT_SHAPE)
    sizes.append(("report", len(REPORT_SHAPE)))
    total = len("\n".join(chunks)) + 1
    limit = cfg["budget_pack"] or DEFAULT_PACK_WARN
    if total > limit:
        warns.append(pack_size_warning(total, limit, sizes, set(map_refs)))
    if args.measure:
        for name, size in sizes:
            emit("%s: %d" % (name, size))
    else:
        emit("\n".join(chunks))
    for warn in warns:
        emit(warn)
    if cfg["budget_pack"] and total > cfg["budget_pack"]:
        emit("PACK %d chars > budget_pack %d" % (total, cfg["budget_pack"]))
        return 1
    emit("PACK %d chars" % total)
    return 0


def cmd_run(args):
    """hk run: run a command, log it, record a ledger row, print the tail and RESULT line."""
    root, cfg = setup()
    need_git(cfg["_git"])
    if args.command_tail:
        command = join_command(args.command_tail)
    elif args.label in cfg["checks"]:
        command = cfg["checks"][args.label]
    else:
        raise HkError("run needs a command after -- (or a '%s' entry in hk.json checks); "
                      "nothing was run" % args.label)
    if cfg["machines_check"]:
        proc = subprocess.run(cfg["machines_check"], shell=True, cwd=root,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        if proc.returncode != 0:
            tail = proc.stdout.decode("utf-8", "replace").strip().split("\n")[-3:]
            raise HkError("machines_check failed (exit %d), not running: %s"
                          % (proc.returncode, " / ".join(t.strip() for t in tail if t.strip())))
    code, output, result = execute_run(root, args.label, command, args.record,
                                       cfg["ledger"])
    lines = output.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    tail = lines[-args.tail:] if args.tail > 0 else []
    if tail:
        emit("\n".join(tail))
    emit(result)
    return 0 if code == 0 else 1


def branch_ref(root, cfg, name):
    """Return the best ref for a branch name: the local branch, else the remote one, else None."""
    if rev_parse(root, "refs/heads/" + name):
        return "refs/heads/" + name
    if rev_parse(root, "refs/remotes/%s/%s" % (cfg["remote"], name)):
        return "refs/remotes/%s/%s" % (cfg["remote"], name)
    return None


def scope_files(root, brief, integration, task_ref):
    """Return the files a branch changed, measured from the brief's Base or the branch's fork point.

    When the branch later merged the integration branch, only its own first-parent commits count."""
    if brief.get("base"):
        base = rev_parse(root, brief["base"])
        if base is None:
            raise HkError("brief %s: Base '%s' is not a commit" % (brief["id"], brief["base"]))
    else:
        own = git_lines(root, ["rev-list", "--first-parent", task_ref, "--not", integration])
        base = None
        if own:
            parents = git_out(root, ["rev-list", "--parents", "-n", "1", own[-1]]).split()
            base = parents[1] if len(parents) > 1 else None
        if base is None:
            base = git_out(root, ["merge-base", integration, task_ref])
    span = "%s..%s" % (base, task_ref)
    merges = git_lines(root, ["rev-list", "--first-parent", "--merges", span])
    if not merges:
        return git_lines(root, ["diff", "--name-only", "--no-renames", span, "--"])
    files = []
    for sha in git_lines(root, ["rev-list", "--first-parent", "--no-merges", span]):
        for f in git_lines(root, ["diff-tree", "-r", "--no-commit-id", "--name-only",
                                  "--no-renames", "--root", sha]):
            if f not in files:
                files.append(f)
    return sorted(files)


def cmd_verify(args):
    """hk verify: check a finished branch stays inside the brief's Touch globs, then run Done when."""
    root, cfg = setup()
    need_git(cfg["_git"])
    _, _, brief = load_brief(root, cfg, args.brief, None)
    name = args.ref or brief["branch"]
    if not valid_name(name):
        raise HkError("bad branch name '%s'" % name)
    task_ref = branch_ref(root, cfg, name)
    if task_ref is None:
        raise HkError("branch %s not found" % name)
    integration = branch_ref(root, cfg, cfg["branch"])
    if integration is None:
        raise HkError("integration branch %s not found" % cfg["branch"])
    files = scope_files(root, brief, integration, task_ref)
    failed = False
    offenders = [f for f in files if not any(glob_match(g, f) for g in brief["touch"])]
    for offender in offenders:
        emit("SCOPE FAIL %s" % offender)
        failed = True
    if not offenders:
        emit("SCOPE ok (%d files)" % len(files))
    # Accept where the branch is checked out: here, else in its own worktree.
    current = git_out(root, ["rev-parse", "--abbrev-ref", "HEAD"])
    accept_root = root if current == name else worktree_of_branch(root, name)
    if accept_root:
        for command in brief["done"]:
            code, _, result = execute_run(accept_root, "verify-%s" % brief["id"], command,
                                          False, cfg["ledger"])
            emit(result)
            if code != 0:
                failed = True
                break
    else:
        emit("ACCEPT skipped: check out %s" % name)
    return 1 if failed else 0


def format_ledger_row(fields):
    """Return a short one-line description of a ledger row."""
    if not row_valid(fields):
        return "BAD ROW %s" % " ".join(fields[:3])
    return "%s %s exit=%s commit=%s dirty=%s %s" % (
        fields[0], fields[4], fields[5], fields[2][:7], fields[3], fields[1])


def cmd_facts(args):
    """hk facts: print the evidence needed to write the handoff."""
    root, cfg = setup()
    need_git(cfg["_git"])
    out = []
    branch, remote = cfg["branch"], cfg["remote"]
    local = rev_parse(root, "refs/heads/" + branch)
    remote_tip = rev_parse(root, "refs/remotes/%s/%s" % (remote, branch))
    out.append("integration %s: local %s remote %s"
               % (branch, short(local), short(remote_tip)))
    base = local or remote_tip
    if base:
        rc, data, _ = git_run(root, ["log", "-n", "1", "--format=%H", base, "--",
                                     cfg["handoff"]])
        last = data.decode("utf-8", "replace").strip() if rc == 0 else ""
        span = "%s..%s" % (last, base) if last else base
        count = git_out(root, ["rev-list", "--count", span, "--"])
        out.append("since handoff: %s commits" % count)
        for line in git_lines(root, ["log", "--oneline", "-n", "40", span, "--"]):
            out.append("  " + line)
    unmerged = []
    prefix = "refs/remotes/%s/" % remote
    for line in git_lines(root, ["for-each-ref", "--format=%(refname) %(objectname)",
                                 "refs/remotes/" + remote]):
        refname, sha = line.split(" ", 1)
        name = refname[len(prefix):]
        if name == "HEAD" or name == branch:
            continue
        if remote_tip:
            rc, _, _ = git_run(root, ["merge-base", "--is-ancestor", sha, remote_tip])
            if rc == 0:
                continue
        unmerged.append("  %s %s" % (name, sha[:7]))
    out.append("unmerged remote branches: %d" % len(unmerged))
    out.extend(unmerged)
    for rel in (cfg["ledger"], LOCAL_LEDGER):
        text = read_at(root, rel, None)
        rows = parse_ledger(text) if text else []
        out.append("ledger %s (last 5 of %d)" % (rel, len(rows)))
        for fields in rows[-5:]:
            out.append("  " + format_ledger_row(fields))
    out.append("reads outside Read first/Read when:")
    covered = set()
    handoff_text = read_at(root, cfg["handoff"], None)
    if handoff_text:
        sections, _ = parse_handoff(handoff_text)
        for name in ("Read first", "Read when"):
            for cells in section_rows(sections, name):
                covered.add(ref_path(root, cfg, cells[-1]))
    counts = {}
    order = []
    reads_text = read_at(root, READS_LOG, None)
    for line in (reads_text or "").split("\n"):
        fields = line.split("\t")
        if len(fields) < 5 or fields[0] == "time":
            continue
        if fields[3] not in covered:
            if fields[3] not in counts:
                counts[fields[3]] = 0
                order.append(fields[3])
            counts[fields[3]] += 1
    for path in order:
        out.append("  %s x%d" % (path, counts[path]))
    jobs_text = read_at(root, JOBS_LOG, None)
    jobs = [l for l in (jobs_text or "").split("\n") if l.strip()]
    if jobs:
        out.append("jobs (%s, last 10 of %d)" % (JOBS_LOG, len(jobs)))
        for line in jobs[-10:]:
            out.append("  " + line.replace("\t", " | "))
    out.append("worktrees:")
    for path in worktree_paths(root):
        out.append("  %s %d changed" % (path, status_count(path)))
    emit("\n".join(out))
    return 0


# --------------------------------------------------------------------- check

def check_c1(ctx):
    """C1: any worktree has uncommitted or untracked changes."""
    msgs = []
    for path in worktree_paths(ctx["root"]):
        count = status_count(path)
        if count:
            msgs.append("%s has %d uncommitted or untracked paths" % (path, count))
    return msgs


def check_c2(ctx):
    """C2: the local branch tip differs from the remote branch tip."""
    local = rev_parse(ctx["root"], "refs/heads/" + ctx["branch"])
    if local is None:
        return ["local branch %s missing" % ctx["branch"]]
    if local != ctx["tip"]:
        return ["local %s %s != %s %s" % (ctx["branch"], short(local),
                                          ctx["rref"], short(ctx["tip"]))]
    return []


def check_c3(ctx):
    """C3: bad hk.json, handoff missing, malformed (structure, order, duplicates), or over budget."""
    cfg = ctx["cfg"]
    msgs = list(ctx["config_errors"])
    if ctx["text"] is None:
        msgs.append("handoff %s missing at %s" % (cfg["handoff"], ctx["rref"]))
        return msgs
    msgs.extend(ctx["errors"])
    if len(ctx["text"]) > cfg["budget_handoff"]:
        msgs.append("handoff %d chars > budget %d" % (len(ctx["text"]), cfg["budget_handoff"]))
    return msgs


def valid_anchor_change(change_id, decisions):
    """True if an Anchor change id has a digit and appears in the decisions text as a whole token."""
    if not change_id or not re.search(r"\d", change_id):
        return False
    pattern = r"(?<![\w-])" + re.escape(change_id) + r"(?![\w-])"
    return re.search(pattern, decisions) is not None


def check_c4(ctx):
    """C4: an Anchor key is missing/empty, or a fixed key differs from an older version without a valid decision."""
    if ctx["text"] is None:
        return []
    root, cfg, tip = ctx["root"], ctx["cfg"], ctx["tip"]
    msgs = []
    current = anchor_values(ctx["sections"])
    for key in ANCHOR_KEYS:
        if not current.get(key):
            msgs.append("Anchor '%s' missing or empty" % key)
    commits = git_lines(root, ["log", "-n", "30", "--format=%H", tip, "--", cfg["handoff"]])
    previous_id = ""
    if len(commits) > 1:
        previous_text = read_at(root, cfg["handoff"], commits[1])
        if previous_text is not None:
            previous_id = state_info(parse_handoff(previous_text)[0])["anchor_change"]
    # Baseline: the older version that recorded the last Anchor change, else the oldest in the window.
    # Versions in between do not count, so a mistake that a later commit reverted is not a change.
    baseline = {}
    for commit in commits[1:]:
        old_text = read_at(root, cfg["handoff"], commit)
        if old_text is None:
            continue
        old_sections, _ = parse_handoff(old_text)
        baseline = anchor_values(old_sections)
        if state_info(old_sections)["anchor_change"]:
            break
    changed = [key for key in FIXED_ANCHOR_KEYS
               if key in baseline and current.get(key, "") != baseline[key]]
    if not changed:
        return msgs
    decisions = read_at(root, cfg["decisions"], tip) or ""
    change_id = state_info(ctx["sections"])["anchor_change"]
    if not valid_anchor_change(change_id, decisions):
        msgs.append("%s changed without a valid 'Anchor change: <id>' found in %s"
                    % (", ".join(changed), cfg["decisions"]))
    elif change_id == previous_id:
        msgs.append("%s changed but Anchor change %s was already in the previous version"
                    % (", ".join(changed), change_id))
    return msgs


def in_doc_paths(path, doc_paths):
    """True if a changed file is inside one of the allowed doc paths."""
    for entry in doc_paths:
        if entry.endswith("/"):
            if path.startswith(entry):
                return True
        elif path == entry:
            return True
    return False


def resolve_commit(root, text, remote=None):
    """Resolve a 7-40 hex commit id. Return the full sha, or None.

    The resolved commit's full id must start with the given characters (so a tag or branch named like
    hex cannot pass). With remote, the commit must be reachable from a remote-tracking branch of it."""
    if not re.match(r"^[0-9a-fA-F]{7,40}$", text):
        return None
    full = rev_parse(root, text.lower())
    if full is None or not full.startswith(text.lower()):
        return None
    if remote is not None:
        refs = git_lines(root, ["for-each-ref", "--contains", full, "--format=%(refname)",
                                "refs/remotes/%s" % remote])
        if not [r for r in refs if not r.endswith("/HEAD")]:
            return None
    return full


def ref_exists(root, fullref):
    """True if the exact full ref name exists."""
    rc, _, _ = git_run(root, ["show-ref", "--verify", "--quiet", fullref])
    return rc == 0


def check_c5(ctx):
    """C5: State Main line bad, wrong branch, sha not an ancestor, code changed since, or no ledger proof."""
    if ctx["text"] is None:
        return []
    root, cfg, tip = ctx["root"], ctx["cfg"], ctx["tip"]
    info = state_info(ctx["sections"])
    if info["match"] is None:
        if info["main_line"]:
            return ["State Main line malformed: %s" % info["main_line"]]
        return ["State Main line missing"]
    line_branch, sha_text, state = info["match"].group(1), info["match"].group(2), info["match"].group(3)
    msgs = []
    if line_branch != cfg["branch"]:
        msgs.append("Main branch '%s' is not the integration branch '%s'" % (line_branch, cfg["branch"]))
    full = resolve_commit(root, sha_text)
    if full is None:
        msgs.append("Main sha %s is not a commit id" % sha_text)
        return msgs
    rc, _, _ = git_run(root, ["merge-base", "--is-ancestor", full, tip])
    if rc != 0:
        msgs.append("Main sha %s is not an ancestor of %s" % (sha_text, ctx["rref"]))
        return msgs
    changed = git_lines(root, ["diff", "--name-only", "--no-renames", full, tip, "--"])
    outside = [f for f in changed if not in_doc_paths(f, cfg["doc_paths"])]
    if outside:
        extra = " (+%d more)" % (len(outside) - 1) if len(outside) > 1 else ""
        msgs.append("code changed since Main %s: %s%s" % (sha_text, outside[0], extra))
    if state == "pass":
        label = cfg["main_check"]
        if not any(is_pass_row(r, full, label) for r in ctx["ledger_rows"]):
            msgs.append("check:pass but no clean pass row for %s label %s in %s"
                        % (sha_text, label, cfg["ledger"]))
    return msgs


def check_c6(ctx):
    """C6: Next empty, row 1 lacks Brief or Model, or a named brief is missing, unparsable or has bad refs."""
    if ctx["text"] is None:
        return []
    root, cfg, tip = ctx["root"], ctx["cfg"], ctx["tip"]
    rows = section_rows(ctx["sections"], "Next")
    if not rows:
        return ["Next has no rows"]
    msgs = []
    first = rows[0]
    if first[2] in ("", "-"):
        msgs.append("Next row 1 has no Brief")
    if first[3] in ("", "-"):
        msgs.append("Next row 1 has no Model")
    for number, cells in enumerate(rows, start=1):
        if cells[2] in ("", "-"):
            continue
        path = brief_path(cfg, cells[2])
        text = read_at(root, path, tip)
        if text is None:
            msgs.append("brief %s missing at %s" % (path, ctx["rref"]))
            continue
        try:
            brief = parse_brief(text)
        except HkError as err:
            if number == 1:
                msgs.append("%s (%s)" % (err, path))
            else:
                ctx["warns"].append("WARN C6 %s: not in brief format" % path)
            continue
        for ref in brief["read"]:
            try:
                resolve_ref(root, ref, tip, cfg)
            except HkError as err:
                msgs.append("brief %s: %s" % (path, err))
    return msgs


def check_c7(ctx):
    """C7: a Read first or Read when ref does not resolve at the remote ref."""
    if ctx["text"] is None:
        return []
    msgs = []
    for name in ("Read first", "Read when"):
        for cells in section_rows(ctx["sections"], name):
            try:
                resolve_ref(ctx["root"], cells[-1], ctx["tip"], ctx["cfg"])
            except ExternalMissing as err:
                ctx["warns"].append("WARN C7 %s" % err)
            except HkError as err:
                msgs.append("%s: %s" % (name, err))
    return msgs


def check_c8(ctx):
    """C8: the boot output built from the remote ref is over budget_boot."""
    if ctx["text"] is None:
        return []
    chunks, _, total = build_boot(ctx["root"], ctx["cfg"], ctx["tip"], False)
    ctx["boot"] = total
    if total > ctx["cfg"]["budget_boot"]:
        return ["boot %d chars > budget %d" % (total, ctx["cfg"]["budget_boot"])]
    return []


def check_c9(ctx):
    """C9: an Exists row is 'done', has bad evidence, or its paths changed since verified."""
    if ctx["text"] is None:
        return []
    root, cfg, tip = ctx["root"], ctx["cfg"], ctx["tip"]
    msgs = []
    for cells in section_rows(ctx["sections"], "Exists"):
        piece, status, evidence, paths = cells
        if status.lower() == "done":
            msgs.append("%s: status 'done' is not allowed" % piece)
        match = EVIDENCE_RE.match(evidence)
        if match is None:
            msgs.append("%s: evidence '%s' is not <sha> or check:<sha>" % (piece, evidence))
            continue
        full = resolve_commit(root, match.group(2), cfg["remote"])
        if full is None:
            msgs.append("%s: evidence %s is not a commit on a %s branch"
                        % (piece, match.group(2), cfg["remote"]))
            continue
        if match.group(1) and not any(row_valid(r) and r[2] == full and r[5] == "0"
                                      and r[3] == "0" for r in ctx["ledger_rows"]):
            msgs.append("%s: check:%s has no pass row" % (piece, match.group(2)))
        globs = split_list(paths, ",")
        if globs:
            changed = git_lines(root, ["diff", "--name-only", "--no-renames", full, tip, "--"])
            hit = [f for f in changed if any(glob_match(g, f) for g in globs)]
            if hit:
                msgs.append("%s: changed since verified: %s" % (piece, hit[0]))
    return msgs


def check_c10(ctx):
    """C10: a Parked branch name is unsafe or not on the remote, or its Tip is not a prefix of the remote tip."""
    if ctx["text"] is None:
        return []
    root, cfg = ctx["root"], ctx["cfg"]
    msgs = []
    for cells in section_rows(ctx["sections"], "Parked"):
        name, tip_cell = cells[0], cells[1].lower()
        if not valid_name(name):
            msgs.append("parked branch name '%s' is not a safe name" % name)
            continue
        fullref = "refs/remotes/%s/%s" % (cfg["remote"], name)
        if not ref_exists(root, fullref):
            msgs.append("parked branch %s not on %s" % (name, cfg["remote"]))
            continue
        remote_tip = rev_parse(root, fullref)
        if (remote_tip is None or not re.match(r"^[0-9a-f]{7,40}$", tip_cell)
                or not remote_tip.startswith(tip_cell)):
            msgs.append("parked %s tip %s is not a prefix of %s" % (name, cells[1], short(remote_tip)))
    return msgs


def check_c11(ctx):
    """C11: a CONTEXT_MAP ref does not resolve at the remote ref."""
    root, cfg, tip = ctx["root"], ctx["cfg"], ctx["tip"]
    try:
        rows = load_context_map(root, cfg, tip)
    except HkError as err:
        return [str(err)]
    msgs = []
    for row in rows:
        for ref in row["read"]:
            try:
                resolve_ref(root, ref, tip, cfg)
            except HkError as err:
                msgs.append(str(err))
    return msgs


def check_c12(ctx):
    """C12: .hk/ is not in .gitignore at the remote ref, or git does not ignore .hk/probe here."""
    root = ctx["root"]
    text = read_at(root, ".gitignore", ctx["tip"]) or ""
    accepted = (".hk", ".hk/", "/.hk", "/.hk/", ".hk/*", "/.hk/*", ".hk/**", "/.hk/**")
    if not any(line.strip() in accepted for line in text.split("\n")):
        return [".hk/ is not in .gitignore at %s" % ctx["rref"]]
    rc, _, _ = git_run(root, ["check-ignore", "-q", ".hk/probe"])
    if rc != 0:
        return ["git check-ignore says .hk/probe is not ignored"]
    return []


def check_c13(ctx):
    """C13: a ledger row's row_sha does not recompute."""
    cfg, root = ctx["cfg"], ctx["root"]
    msgs = []
    sources = [(cfg["ledger"], ctx["ledger_text"]),
               (LOCAL_LEDGER, read_at(root, LOCAL_LEDGER, None))]
    for rel, text in sources:
        if not text:
            continue
        for number, fields in enumerate(parse_ledger(text), start=1):
            if not row_valid(fields):
                msgs.append("%s row %d: row_sha does not recompute" % (rel, number))
    return msgs


def check_c14(ctx):
    """C14: the handoff text matches the secret pattern (searched over the whole text)."""
    if ctx["text"] is None:
        return []
    msgs = []
    for match in SECRET_RE.finditer(ctx["text"]):
        number = ctx["text"].count("\n", 0, match.start()) + 1
        msgs.append("secret-like value after '%s' at handoff line %d" % (match.group(1), number))
    return msgs


def cmd_check(args):
    """hk check: run all handoff checks against the remote branch and print the verdict."""
    root, has_git = locate_root()
    need_git(has_git)
    wt_text = read_at(root, "hk.json", None)
    wt_vals = {}
    if wt_text is not None:
        try:
            data = json.loads(wt_text)
        except ValueError:
            data = None
        if isinstance(data, dict):
            wt_vals = data
    remote = wt_vals.get("remote", DEFAULTS["remote"])
    explicit_branch = wt_vals.get("branch")
    early = []
    if not valid_name(remote):
        early.append("hk.json remote is not a safe name")
    if "branch" in wt_vals and not valid_name(explicit_branch):
        early.append("hk.json branch is not a safe name")
    if early:
        emit("\n".join(["FAIL C3 %s" % m for m in early] + ["HANDOFF FAIL %d" % len(early)]))
        return 1
    if not args.no_fetch:
        rc, _, err = git_run(root, ["fetch", "--quiet", "--prune", "--", remote])
        if rc != 0:
            first = err.strip().split("\n")[0] if err.strip() else "exit %d" % rc
            raise HkError("git fetch %s failed: %s" % (remote, first))
    branch = explicit_branch or default_branch(root, remote)
    tip = rev_parse(root, "refs/remotes/%s/%s" % (remote, branch))
    if tip is None:
        raise HkError("no remote branch %s/%s" % (remote, branch))
    cfg, config_errors = parse_config(read_at(root, "hk.json", tip))
    file_remote = cfg["remote"]
    file_branch = cfg["branch"] or default_branch(root, file_remote)
    if file_remote != remote:
        config_errors.append("hk.json remote differs: working tree '%s', %s/%s file '%s'"
                             % (remote, remote, branch, file_remote))
    if file_branch != branch:
        config_errors.append("hk.json branch differs: working tree '%s', %s/%s file '%s'"
                             % (branch, remote, branch, file_branch))
    cfg["remote"], cfg["branch"], cfg["_git"] = remote, branch, True
    text = read_at(root, cfg["handoff"], tip)
    sections, errors = parse_handoff(text) if text is not None else ({}, [])
    ledger_text = read_at(root, cfg["ledger"], tip) or ""
    ctx = {"root": root, "cfg": cfg, "branch": branch, "tip": tip,
           "rref": "%s/%s" % (remote, branch), "text": text,
           "sections": sections, "errors": errors, "boot": None, "warns": [],
           "config_errors": config_errors,
           "ledger_text": ledger_text, "ledger_rows": parse_ledger(ledger_text)}
    checks = [("C1", check_c1), ("C2", check_c2), ("C3", check_c3), ("C4", check_c4),
              ("C5", check_c5), ("C6", check_c6), ("C7", check_c7), ("C8", check_c8),
              ("C9", check_c9), ("C10", check_c10), ("C11", check_c11),
              ("C12", check_c12), ("C13", check_c13), ("C14", check_c14),
              ("C15", check_c15)]
    out = []
    fails = 0
    failed_ids = []
    for check_id, func in checks:
        for msg in func(ctx):
            out.append("FAIL %s %s" % (check_id, msg))
            fails += 1
            if check_id not in failed_ids:
                failed_ids.append(check_id)
    append_tsv(root, CHECK_RUNS, None, [now_utc(), tip, ",".join(failed_ids)])
    out.extend(ctx["warns"])
    version_line = version_warning()
    if version_line:
        out.append(version_line)
    info = state_info(sections)
    if info["match"] is not None and info["match"].group(3) in ("fail", "not-run"):
        out.append("WARN State says check:%s" % info["match"].group(3))
    if fails:
        out.append("HANDOFF FAIL %d" % fails)
    else:
        out.append("HANDOFF OK %s@%s boot=%s" % (
            branch, short(tip), ctx["boot"] if ctx["boot"] is not None else "?"))
    emit("\n".join(out))
    return 1 if fails else 0


# ---------------------------------------------------------- self-tuning

def file_sha12(path):
    """Return the first 12 hex characters of the sha256 of a file's bytes."""
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()[:12]


def version_warning():
    """Return the version WARN line if ~/.claude/skills/handoff/hk.py differs from this file, else ''."""
    other = os.path.join(os.path.expanduser("~"), ".claude", "skills", "handoff", "hk.py")
    mine = os.path.abspath(__file__)
    if not os.path.isfile(other):
        return ""
    if os.path.exists(mine) and os.path.samefile(other, mine):
        return ""
    if file_sha12(other) == file_sha12(mine):
        return ""
    return "WARN version: project copy differs from %s" % other.replace("\\", "/")


def learned_texts(root, cfg, gitref):
    """Return [(name, text)] for the learned files that exist: project file first, then the skill's."""
    found = []
    text = read_at(root, cfg["learned"], gitref)
    if text is not None:
        found.append((cfg["learned"], text))
    skill_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "LEARNED.md")
    if os.path.isfile(skill_path):
        with open(skill_path, "r", encoding="utf-8", errors="replace", newline="") as fh:
            found.append(("LEARNED.md", fh.read().replace("\r\n", "\n")))
    return found


def learned_rows(text):
    """Return (rows, errors) of a learned file's table; a file with no table has no rows."""
    lines = text.split("\n")
    if not any(l.strip().startswith("|") for l in lines):
        return [], []
    return parse_table(lines, LEARNED_HEADER)


def check_c15(ctx):
    """C15: a learned file is over 20 rows or 2000 chars, malformed, or a row has no Evidence."""
    msgs = []
    for name, text in learned_texts(ctx["root"], ctx["cfg"], ctx["tip"]):
        rows, errors = learned_rows(text)
        for err in errors:
            msgs.append("%s: %s" % (name, err))
        if len(rows) > LEARNED_MAX_ROWS:
            msgs.append("%s: %d rows > %d" % (name, len(rows), LEARNED_MAX_ROWS))
        if len(text) > LEARNED_MAX_CHARS:
            msgs.append("%s: %d chars > %d" % (name, len(text), LEARNED_MAX_CHARS))
        for cells in rows:
            if not cells[3]:
                msgs.append("%s: row %s has an empty Evidence cell" % (name, cells[0]))
    return msgs


def append_tsv(root, rel_path, columns, fields):
    """Append a row to a tracked or local TSV file, writing the header first when the file is new or empty."""
    full = os.path.join(root, *rel_path.split("/"))
    if not os.path.isdir(os.path.dirname(full)):
        os.makedirs(os.path.dirname(full))
    new = not os.path.isfile(full) or os.path.getsize(full) == 0
    with open(full, "a", encoding="utf-8", newline="\n") as fh:
        if new and columns:
            fh.write("\t".join(columns) + "\n")
        fh.write("\t".join(clean_field(f) for f in fields) + "\n")


def read_tsv_rows(root, rel_path, min_fields):
    """Return the rows (lists of fields) of a local TSV file, skipping blank and header lines."""
    text = read_at(root, rel_path, None) or ""
    rows = []
    for line in text.split("\n"):
        fields = line.split("\t")
        if len(fields) >= min_fields and fields[0] != "time":
            rows.append(fields)
    return rows


def metrics_rows(root, cfg):
    """Return the metrics rows as dicts keyed by column name (rows with the wrong width are skipped)."""
    rows = []
    for fields in read_tsv_rows(root, cfg["metrics"], len(METRICS_COLUMNS)):
        rows.append(dict(zip(METRICS_COLUMNS, fields)))
    return rows


def split_cell(value, sep):
    """Split a metrics cell into items; '-' and '' mean none."""
    if value in ("", "-"):
        return []
    return [v for v in value.split(sep) if v]


def join_cell(items, sep):
    """Join metrics items into one cell; '-' when there are none."""
    return sep.join(items) if items else "-"


def as_int(value):
    """Return an int for a metrics cell, or None if it is not a number."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def covered_paths(root, cfg):
    """Return the file paths of the working-tree handoff's Read first and Read when refs."""
    covered = set()
    text = read_at(root, cfg["handoff"], None)
    if text:
        sections, _ = parse_handoff(text)
        for name in ("Read first", "Read when"):
            for cells in section_rows(sections, name):
                covered.add(ref_path(root, cfg, cells[-1]))
    return covered


def cmd_record(args):
    """hk record: append one metrics row for this session, then truncate the session logs."""
    root, cfg = setup()
    coldstart = args.coldstart or "-"
    if coldstart != "-" and not re.match(r"^\d+/\d+$", coldstart):
        raise HkError("--coldstart must look like N/M")
    _, _, boot_total = build_boot(root, cfg, None, False)
    handoff_text = read_at(root, cfg["handoff"], None)
    if handoff_text is None:
        raise HkError("handoff %s not found" % cfg["handoff"])
    runs = read_tsv_rows(root, CHECK_RUNS, 2)
    first = []
    for fields in runs:
        ids = split_cell(fields[2] if len(fields) > 2 else "", ",")
        if not ids:
            break
        for check_id in ids:
            if check_id not in first:
                first.append(check_id)
    reads = read_tsv_rows(root, READS_LOG, 5)
    covered = covered_paths(root, cfg)
    handoff_path = norm_path(cfg["handoff"])
    offmap = []
    fetched = []
    sessions = set()
    for fields in reads:
        sessions.add(fields[1])
        path = fields[3]
        if path not in covered and path != handoff_path and path not in offmap:
            offmap.append(path)
        if fields[2] == "hk" and len(fields) > 5 and fields[5] and fields[5] not in fetched:
            fetched.append(fields[5])
    sections, _ = parse_handoff(handoff_text)
    when_refs = [cells[-1] for cells in section_rows(sections, "Read when")]
    unused = len([r for r in when_refs if r not in fetched])
    level = 0
    for session in sessions:
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", session)
        level_file = os.path.join(root, ".hk", "budget.%s" % safe)
        if os.path.isfile(level_file):
            with open(level_file, "r", encoding="utf-8") as fh:
                level = max(level, as_int(fh.read().strip()) or 0)
    ids = []
    for _, text in learned_texts(root, cfg, None):
        ids.extend(cells[0] for cells in learned_rows(text)[0])
    commit = (rev_parse(root, "HEAD") or "-") if cfg["_git"] else "-"
    machine = os.environ.get("HK_MACHINE") or socket.gethostname()
    row = [now_utc(), machine, commit, str(boot_total), str(len(handoff_text)),
           join_cell(first, ","), join_cell(offmap[:10], ";"), join_cell(fetched[:10], ";"),
           str(unused), str(level), coldstart, join_cell(ids, ",")]
    append_tsv(root, cfg["metrics"], METRICS_COLUMNS, row)
    for rel in (READS_LOG, CHECK_RUNS):
        full = os.path.join(root, *rel.split("/"))
        if os.path.isfile(full):
            with open(full, "w", encoding="utf-8", newline="\n"):
                pass
    emit("recorded boot=%d handoff=%d first_fails=%s" % (boot_total, len(handoff_text),
                                                        join_cell(first, ",")))
    return 0


def check_id_of(learned_id):
    """Return the check id (C1..C15) a learned row's Id starts with, or ''."""
    match = re.match(r"^(C\d+)", learned_id)
    return match.group(1) if match else ""


def tune_proposals(root, cfg, rows):
    """Apply rules T1 to T8 to the metrics rows (oldest first). Return the PROPOSE lines."""
    out = []
    recent = rows[-10:]
    # T1
    counts = {}
    for row in recent:
        for name in set(split_cell(row["offmap"], ";")):
            counts[name] = counts.get(name, 0) + 1
    for name in sorted(counts, key=lambda n: (-counts[n], n)):
        if counts[name] >= 2:
            out.append("add Read-when row for %s|offmap in %d of last %d rows"
                       % (name, counts[name], len(recent)))
    # T2
    last6 = rows[-6:]
    if len(last6) >= 6:
        text = read_at(root, cfg["handoff"], None)
        sections = parse_handoff(text)[0] if text else {}
        for cells in section_rows(sections, "Read when"):
            ref = cells[-1]
            if all(ref not in split_cell(r["fetched"], ";") for r in last6):
                out.append("drop Read-when row %s|not fetched in any of the last 6 rows" % ref)
    # T3
    learned_check_ids = set()
    learned_all = []
    for _, text in learned_texts(root, cfg, None):
        for cells in learned_rows(text)[0]:
            learned_all.append(cells[0])
            if check_id_of(cells[0]):
                learned_check_ids.add(check_id_of(cells[0]))
    last5 = rows[-5:]
    fail_counts = {}
    for row in last5:
        for check_id in set(split_cell(row["first_fails"], ",")):
            fail_counts[check_id] = fail_counts.get(check_id, 0) + 1
    for check_id in sorted(fail_counts, key=lambda c: (-fail_counts[c], c)):
        if fail_counts[check_id] >= 2 and check_id not in learned_check_ids:
            out.append("add learned note: %s keeps failing first time: %s|%s in first_fails in %d of last %d rows"
                       % (check_id, CHECK_MEANINGS.get(check_id, "see hk check"), check_id,
                          fail_counts[check_id], len(last5)))
    # T4
    sizes = [as_int(r["boot_chars"]) for r in rows[-3:]]
    last_size = as_int(rows[-1]["boot_chars"]) if rows else None
    rising = len(sizes) == 3 and None not in sizes and sizes[0] < sizes[1] < sizes[2]
    over = last_size is not None and last_size > 0.8 * cfg["budget_boot"]
    if rising or over:
        parts = build_boot(root, cfg, None, False)[1]
        reads = [p for p in parts if p[0] not in ("boot sections", "index")
                 and not p[0].startswith("inline ")] or parts
        biggest = max(reads, key=lambda p: p[1])
        why = ("boot_chars rising %s" % "->".join(str(x) for x in sizes)) if rising else (
            "boot_chars %d is above 80%% of budget %d" % (last_size, cfg["budget_boot"]))
        out.append("trim Read first: largest part is %s (%d chars)|%s" % (biggest[0], biggest[1], why))
    # T5
    marks = []
    for row in rows[-3:]:
        match = re.match(r"^(\d+)/(\d+)$", row["coldstart"])
        if match:
            marks.append((int(match.group(1)), int(match.group(2))))
    short_marks = [m for m in marks if m[0] < m[1]]
    if len(short_marks) >= 2:
        out.append("rewrite the boot sections the cold-start reader got wrong|coldstart below full marks in %d of last %d rows (%s)"
                   % (len(short_marks), len(rows[-3:]), ", ".join("%d/%d" % m for m in marks)))
    # T6 and T7
    last3 = rows[-3:]
    for learned_id in learned_all:
        check_id = check_id_of(learned_id)
        if not check_id:
            continue
        active = len([r for r in recent if learned_id in split_cell(r["learned_ids"], ",")])
        still = len([r for r in last3 if check_id in split_cell(r["first_fails"], ",")])
        if active >= 5 and still >= 2:
            out.append("remove learned note %s: no effect|active for %d rows yet %s still in first_fails in %d of last 3 rows"
                       % (learned_id, active, check_id, still))
        elif len(recent) >= 10 and all(check_id not in split_cell(r["first_fails"], ",") for r in recent):
            out.append("retire learned note %s: no longer needed|%s absent from first_fails for %d rows"
                       % (learned_id, check_id, len(recent)))
    # T8
    high = len([r for r in last3 if (as_int(r["budget_level"]) or 0) >= 1])
    if high >= 2:
        out.append("delegate more reading: main session over its read budget|budget_level >= 1 in %d of last 3 rows" % high)
    return out


def cmd_tune(args):
    """hk tune: print at most 12 PROPOSE lines from the last 10 metrics rows; apply nothing."""
    root, cfg = setup()
    rows = metrics_rows(root, cfg)
    if not rows:
        return 0
    lines = []
    for item in tune_proposals(root, cfg, rows[-10:]):
        action, evidence = item.split("|", 1)
        lines.append("PROPOSE %s \u2014 evidence: %s" % (action, evidence))
    if lines:
        emit("\n".join(lines[:12]))
    return 0


def cmd_learned(args):
    """hk learned: print the active learned rows (project file first) and a count line."""
    root, cfg = setup()
    out = []
    chars = 0
    for _, text in learned_texts(root, cfg, None):
        for cells in learned_rows(text)[0]:
            line = " | ".join(cells)
            out.append(line)
            chars += len(line)
    out.append("LEARNED %d rows, %d chars" % (len(out), chars))
    emit("\n".join(out))
    return 0


def cmd_version(args):
    """hk version: print the version constant and the first 12 hex of the sha256 of hk.py."""
    emit("%s %s" % (VERSION, file_sha12(os.path.abspath(__file__))))
    return 0


# ------------------------------------------------------ export, quiz, key

def cmd_export(args):
    """hk export: write the tree of <remote>/<branch> into a new or empty plain folder."""
    root, cfg = setup()
    need_git(cfg["_git"])
    target = os.path.abspath(args.dir)
    if os.path.exists(target):
        if not os.path.isdir(target) or os.listdir(target):
            raise HkError("%s exists and is not an empty folder" % args.dir)
    tip = rev_parse(root, "refs/remotes/%s/%s" % (cfg["remote"], cfg["branch"]))
    if tip is None:
        raise HkError("no remote branch %s/%s" % (cfg["remote"], cfg["branch"]))
    rc, data, err = git_run(root, ["archive", "--format=tar", tip])
    if rc != 0:
        raise HkError("git archive failed: %s" % err.strip().split("\n")[0])
    skipped = []
    count = 0
    try:
        if not os.path.isdir(target):
            os.makedirs(target)
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as tar:
            members = []
            for member in tar.getmembers():
                if member.issym() or member.islnk():
                    skipped.append(member.name)
                else:
                    members.append(member)
                    if member.isfile():
                        count += 1
            if hasattr(tarfile, "data_filter"):
                tar.extractall(target, members=members, filter="data")
            else:
                tar.extractall(target, members=members)
    except (tarfile.TarError, OSError, ValueError) as err:
        raise HkError("export failed: %s" % str(err).split("\n")[0])
    for name in skipped:
        emit("skipped symlink %s" % name)
    emit("exported %s@%s to %s (%d files)" % (cfg["branch"], short(tip), args.dir, count))
    return 0


def cmd_quiz(args):
    """hk quiz: print the fixed cold-start questions."""
    emit(QUIZ)
    return 0


def cmd_key(args):
    """hk key: print the answers to the quiz, taken mechanically from the handoff."""
    root, cfg = setup()
    if args.ref:
        need_git(cfg["_git"])
    _, sections, errors = load_handoff(root, cfg, args.ref)
    dups = duplicate_errors(sections)
    if dups:
        raise HkError("handoff: %s" % dups[0])
    anchor = anchor_values(sections)
    out = ["Goal: %s" % anchor.get("Goal", ""), "Milestone: %s" % anchor.get("Milestone", "")]
    nxt = section_rows(sections, "Next")
    if nxt:
        out.append("Step 1: %s | brief %s | model %s" % (nxt[0][1], nxt[0][2], nxt[0][3]))
    else:
        out.append("Step 1: none")
    reads = [cells[0] for cells in section_rows(sections, "Read first")]
    out.append("Read first: %s" % ("; ".join(reads) if reads else "none"))
    never = [cells[0] for cells in section_rows(sections, "Never read")]
    out.append("Never read: %s" % ("; ".join(never) if never else "none"))
    info = state_info(sections)
    out.append("State: %s" % (info["main_line"] or "missing"))
    out.append("Parked: %d" % len(section_rows(sections, "Parked")))
    out.append("Findings: %d" % len(section_rows(sections, "Findings")))
    emit("\n".join(out))
    return 0


def read_hook_input():
    """Read one JSON object from stdin; return a dict, or None if it is missing or malformed."""
    try:
        data = json.loads(sys.stdin.buffer.read().decode("utf-8", "replace"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def log_hook_read(root, session, source, path, chars):
    """Append one row to .hk/reads.tsv for a hook-seen read."""
    log_read(root, path, chars, source, session)


def budget_announcement(root, cfg, session):
    """Return the budget JSON text if a new 25% level was crossed by this session's main reads, else ''."""
    total = 0
    text = read_at(root, READS_LOG, None) or ""
    for line in text.split("\n"):
        fields = line.split("\t")
        if len(fields) >= 5 and fields[1] == session and fields[2] == "main":
            try:
                total += int(fields[4])
            except ValueError:
                pass
    budget = cfg["read_budget"]
    if total < budget:
        return ""
    level = 1 + int((total - budget) / (0.25 * budget))
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", session)
    level_file = os.path.join(root, ".hk", "budget.%s" % safe)
    last = 0
    if os.path.isfile(level_file):
        with open(level_file, "r", encoding="utf-8") as fh:
            try:
                last = int(fh.read().strip() or "0")
            except ValueError:
                last = 0
    if level <= last:
        return ""
    with open(level_file, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("%d\n" % level)
    message = ("Read budget: %d of %d characters read by the main session. "
               "Hand off at the next clean point, or delegate further reading."
               % (total, budget))
    return json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse", "permissionDecision": "allow",
        "additionalContext": message}})


def locate_in_root(root, full):
    """Return the forward-slash path of full below root (lexically, no symlink resolved), or None if outside."""
    current = os.path.abspath(full)
    parts = []
    while True:
        if os.path.exists(current) and os.path.samefile(current, root):
            return "/".join(reversed(parts))
        parent = os.path.dirname(current)
        if parent == current:
            return None
        parts.append(os.path.basename(current))
        current = parent


def run_hook_read(data):
    """Work of hk hook-read on a parsed hook input. Return the exit code."""
    cwd = data.get("cwd") or os.getcwd()
    tool = data.get("tool_name")
    tool_input = data.get("tool_input")
    if not isinstance(tool_input, dict):
        return 0
    key = "file_path" if tool == "Read" else "path"
    raw = tool_input.get(key)
    if tool not in ("Read", "Grep", "Glob") or not raw or not isinstance(raw, str):
        return 0
    root, cfg = setup(cwd)
    full = raw if os.path.isabs(raw) else os.path.join(cwd, raw)
    rel = locate_in_root(root, full)
    if rel is None:
        return 0
    try:
        check_safe_path(root, rel)
    except HkError:
        sys.stderr.write("Symlink path refused: %s.\n" % rel)
        return 2
    rel = rel or "."
    entries, cited = never_read_entries(root, cfg)
    if rel != "." and is_never_read(rel, entries, cited):
        sys.stderr.write("Never-read path: %s. It is on the handoff's Never read list. "
                         "Ask the owner before opening it.\n" % rel)
        return 2
    if tool != "Read":
        return 0
    session = str(data.get("session_id") or "-")
    is_agent = bool(data.get("agent_id"))
    size = os.path.getsize(full) if os.path.isfile(full) else 0
    limit = tool_input.get("limit")
    if isinstance(limit, int) and not isinstance(limit, bool) and limit > 0:
        size = min(limit * 80, size)
    log_hook_read(root, session, "agent" if is_agent else "main", rel, size)
    if not is_agent:
        announcement = budget_announcement(root, cfg, session)
        if announcement:
            emit(announcement)
    return 0


def cmd_hook_read(args):
    """hk hook-read: PreToolUse hook. Block Never-read paths (exit 2), log reads, announce the budget."""
    try:
        data = read_hook_input()
        if data is None:
            return 0
        return run_hook_read(data)
    except Exception:
        return 0


def run_hook_anchor(data):
    """Work of hk hook-anchor: print Anchor, the State Main line, row 1 of Next, and the reminder."""
    root, cfg = setup(data.get("cwd") or os.getcwd())
    text = read_at(root, cfg["handoff"], None)
    if text is None:
        return 0
    sections, _ = parse_handoff(text)
    out = []
    if "Anchor" in sections:
        out.append("\n".join([sections["Anchor"]["heading"]] + trim_blank_tail(sections["Anchor"]["body"])))
    info = state_info(sections)
    out.extend(info["main_lines"])
    out.extend("WARN %s" % msg for msg in duplicate_errors(sections))
    rows = section_rows(sections, "Next")
    if rows:
        out.append("Next 1: " + " | ".join(rows[0]))
    out.append("Re-read the anchor. Work the Next table does not call for goes to the backlog.")
    emit("\n".join(out))
    return 0


def cmd_hook_anchor(args):
    """hk hook-anchor: SessionStart (compact) hook. Never fails the session."""
    try:
        data = read_hook_input()
        if data is None:
            return 0
        return run_hook_anchor(data)
    except Exception:
        return 0


# ---------------------------------------------------------------------- main

def build_parser():
    """Build the argparse parser with one subcommand per hk command."""
    parser = argparse.ArgumentParser(prog="hk", description="handoff kit")
    sub = parser.add_subparsers(dest="command", metavar="command")
    sub.required = True

    p = sub.add_parser("boot", help="print boot sections, Read-first files, index")
    p.add_argument("--measure", action="store_true")
    p.add_argument("--ref")
    p.set_defaults(func=cmd_boot)

    p = sub.add_parser("get", help="print refs and log the reads")
    p.add_argument("refs", nargs="+")
    p.add_argument("--ref")
    p.set_defaults(func=cmd_get)

    p = sub.add_parser("for", help="reading list for paths")
    p.add_argument("paths", nargs="+")
    p.add_argument("--print", action="store_true")
    p.set_defaults(func=cmd_for)

    p = sub.add_parser("pack", help="everything an agent needs for a brief")
    p.add_argument("brief")
    p.add_argument("--measure", action="store_true")
    p.set_defaults(func=cmd_pack)

    p = sub.add_parser("run", help="run a command and record the result (use -- before it)")
    p.add_argument("label")
    p.add_argument("--record", action="store_true")
    p.add_argument("--tail", type=int, default=15)
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("verify", help="scope check and acceptance for a branch")
    p.add_argument("brief")
    p.add_argument("--ref")
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("facts", help="evidence for writing the handoff")
    p.set_defaults(func=cmd_facts)

    p = sub.add_parser("check", help="check the handoff against the remote branch")
    p.add_argument("--no-fetch", action="store_true")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("export", help="write the remote tree into a folder")
    p.add_argument("dir")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("quiz", help="print the cold-start questions")
    p.set_defaults(func=cmd_quiz)

    p = sub.add_parser("key", help="print the quiz answers from the handoff")
    p.add_argument("--ref")
    p.set_defaults(func=cmd_key)

    p = sub.add_parser("record", help="append one metrics row after HANDOFF OK")
    p.add_argument("--coldstart")
    p.set_defaults(func=cmd_record)

    p = sub.add_parser("tune", help="propose changes from the metrics file")
    p.set_defaults(func=cmd_tune)

    p = sub.add_parser("learned", help="print the active learned rows")
    p.set_defaults(func=cmd_learned)

    p = sub.add_parser("version", help="print the version and hk.py checksum")
    p.set_defaults(func=cmd_version)

    p = sub.add_parser("hook-read", help="PreToolUse hook for Read|Grep|Glob")
    p.set_defaults(func=cmd_hook_read)
    p = sub.add_parser("hook-anchor", help="SessionStart (compact) hook")
    p.set_defaults(func=cmd_hook_anchor)
    return parser


def main(argv=None):
    """Parse the command line, run the command, and return the exit code."""
    argv = list(sys.argv[1:] if argv is None else argv)
    set_utf8()
    command_tail = None
    if argv and argv[0] == "run" and "--" in argv:
        cut = argv.index("--")
        command_tail = argv[cut + 1:]
        argv = argv[:cut]
    args = build_parser().parse_args(argv)
    args.command_tail = command_tail
    try:
        return args.func(args)
    except HkError as err:
        sys.stderr.write("hk: %s\n" % err)
        return 2


if __name__ == "__main__":
    sys.exit(main())
