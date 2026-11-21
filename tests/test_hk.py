"""Tests for hk.py (spec section 7). Standard library only.

Every test builds a throwaway repository with a bare "remote" in a temp
folder. Nothing outside those folders is touched and there is no network.
"""

import atexit
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HK = os.path.join(KIT, "skill", "handoff", "hk.py")
sys.path.insert(0, os.path.dirname(HK))

import hk  # noqa: E402

FAKE_HOME = tempfile.mkdtemp(prefix="hk-home-")
atexit.register(shutil.rmtree, FAKE_HOME, True)

TEST_ENV = dict(os.environ)
TEST_ENV.update({
    "HOME": FAKE_HOME,
    "USERPROFILE": FAKE_HOME,
    "HK_MACHINE": "testbox",
    "HK_SESSION": "s1",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_TERMINAL_PROMPT": "0",
})

OK_CMD = [sys.executable, "-c", "print('ok')"]


def quoted_ok_command():
    """Return a shell command string that prints ok using this Python."""
    return '"%s" -c "print(\'ok\')"' % sys.executable


HANDOFF = """# Handoff

## Anchor
Goal: Ship the demo app.
Milestone: M1 walking skeleton
Ends when: the demo runs end to end
Workflow: briefs to small agents, lead merges
Out of scope: payments
Decides: Alex

## State
Main: main @ {s1} check:pass

## Next
| # | Step | Brief | Model | Done when |
|---|---|---|---|---|
| 1 | Build the thing | `docs/briefs/task-1.md` | smallest | tests pass |

## Questions
| # | Question | Blocks |
|---|---|---|

## How to work
- Small steps.

## Read first
| Ref |
|---|
| `docs/guide.md#Setup` |
| docs/arch.md |

## Read when
| Trigger | Ref |
|---|---|
| editing src | `docs/lessons/core.md` |

## Never read
| Path |
|---|
| `vendor/**` |

## Exists
| Piece | Status | Evidence | Paths |
|---|---|---|---|
| core | verified | `check:{s1}` | `src/**` |

## Parked
| Branch | Tip | Verified | Not verified | Next |
|---|---|---|---|---|
| feature-x | {px} | unit | e2e | rebase |

## Findings
| Sev | Finding | File |
|---|---|---|

## Decisions
D-1 chose sqlite.

## Machines
Mac only.
"""

BRIEF = """# Brief: task-1
Model: smallest
Branch: task-1
Touch: src/**, tests/**
Done when: `{cmd}`
Read: docs/guide.md#Usage

## Task
Do the thing.
"""

CONTEXT_MAP = """# Context map

| Paths | Read | Lessons |
|---|---|---|
| `src/**` | docs/arch.md#Overview; docs/guide.md#Usage | core |
"""

GUIDE = "# Guide\n\n## Setup\nInstall it.\n\n## Usage\nRun it.\n"
ARCH = "# Arch\n\n## Overview\nBig picture.\n\n## Data\nTables.\n"
LESSON = "# Core lessons\n\nKeep it small.\n"
DECISIONS = "# Decisions\n\nD-1 chose sqlite.\n"
LEDGER_HEADER = "\t".join(hk.LEDGER_COLUMNS) + "\n"


class Project:
    """A throwaway project: a bare remote plus a working clone in a temp folder."""

    def __init__(self, tmp):
        """Remember the temp folder and the remote and work paths."""
        self.tmp = tmp
        self.remote = os.path.join(tmp, "remote.git")
        self.work = os.path.join(tmp, "work")
        self.s1 = None
        self.s2 = None

    def git(self, *args):
        """Run git in the working clone and return its output text."""
        return self.git_in(self.work, *args)

    def git_in(self, cwd, *args):
        """Run git in a folder, fail the test on error, return stripped stdout."""
        proc = subprocess.run(["git"] + list(args), cwd=cwd, env=TEST_ENV,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if proc.returncode != 0:
            raise AssertionError("git %s failed: %s" % (" ".join(args),
                                 proc.stderr.decode("utf-8", "replace")))
        return proc.stdout.decode("utf-8", "replace").strip()

    def hk(self, *args, **kwargs):
        """Run hk.py in the working clone. Return (exit code, stdout, stderr)."""
        cwd = kwargs.get("cwd", self.work)
        proc = subprocess.run([sys.executable, HK] + list(args), cwd=cwd,
                              env=TEST_ENV, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE)
        return (proc.returncode, proc.stdout.decode("utf-8", "replace"),
                proc.stderr.decode("utf-8", "replace"))

    def path(self, rel):
        """Return the full path of a file in the working clone."""
        return os.path.join(self.work, *rel.split("/"))

    def read(self, rel):
        """Return the text of a file in the working clone."""
        with open(self.path(rel), "r", encoding="utf-8", newline="") as fh:
            return fh.read()

    def write(self, rel, text):
        """Write a file in the working clone, making folders as needed."""
        full = self.path(rel)
        if not os.path.isdir(os.path.dirname(full)):
            os.makedirs(os.path.dirname(full))
        with open(full, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)

    def replace(self, rel, old, new):
        """Replace text in a working file; fail the test if the old text is absent."""
        text = self.read(rel)
        if old not in text:
            raise AssertionError("%r not found in %s" % (old, rel))
        self.write(rel, text.replace(old, new, 1))

    def commit(self, message="change"):
        """Stage everything and commit in the working clone."""
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)

    def push(self):
        """Push the main branch to the remote."""
        self.git("push", "-q", "origin", "main")

    def commit_push(self, message="change"):
        """Commit everything and push main."""
        self.commit(message)
        self.push()

    def check(self):
        """Run `hk check` and return (exit code, stdout)."""
        code, out, _ = self.hk("check")
        return code, out

    def head(self):
        """Return the full sha of HEAD."""
        return self.git("rev-parse", "HEAD")


def make_project(tmp, dirty_ledger=False):
    """Create a valid project in tmp and return it; `hk check` on it prints HANDOFF OK."""
    p = Project(tmp)
    os.makedirs(p.remote)
    p.git_in(p.remote, "init", "-q", "--bare")
    p.git_in(p.remote, "symbolic-ref", "HEAD", "refs/heads/main")
    p.git_in(tmp, "clone", "-q", p.remote, p.work)
    p.git("symbolic-ref", "HEAD", "refs/heads/main")
    p.git("config", "user.name", "Test User")
    p.git("config", "user.email", "test@example.com")
    p.write(".gitignore", ".hk/\n")
    p.write("docs/HANDOFF.md", HANDOFF.format(s1="0000000", px="0000000"))
    p.write("docs/CONTEXT_MAP.md", CONTEXT_MAP)
    p.write("docs/CHECKS.tsv", LEDGER_HEADER)
    p.write("docs/briefs/task-1.md", BRIEF.format(cmd=quoted_ok_command()))
    p.write("docs/DECISIONS.md", DECISIONS)
    p.write("docs/lessons/core.md", LESSON)
    p.write("docs/guide.md", GUIDE)
    p.write("docs/arch.md", ARCH)
    p.write("src/app.py", "print('app')\n")
    p.commit_push("initial")
    p.s1 = p.head()
    # A parked branch with its own commit, pushed to the remote.
    p.git("checkout", "-q", "-b", "feature-x")
    p.write("feat.txt", "feature\n")
    p.commit("feature")
    p.git("push", "-q", "origin", "feature-x")
    px = p.head()
    p.git("checkout", "-q", "main")
    # The evidence row: a real `hk run full --record`.
    if dirty_ledger:
        p.write("scratch.txt", "dirty\n")
    code, out, err = p.hk("run", "full", "--record", "--", *OK_CMD)
    if code != 0:
        raise AssertionError("hk run failed: %s %s" % (out, err))
    if dirty_ledger:
        os.remove(p.path("scratch.txt"))
    p.write("docs/HANDOFF.md", HANDOFF.format(s1=p.s1, px=px[:7]))
    p.commit_push("handoff and ledger")
    p.s2 = p.head()
    return p


class ProjectCase(unittest.TestCase):
    """Base class: a valid project in a fresh temp folder for each test."""

    dirty_ledger = False

    def setUp(self):
        """Build the valid project."""
        self.tmp = tempfile.mkdtemp(prefix="hk-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.p = make_project(self.tmp, self.dirty_ledger)

    def assertFails(self, check_id):
        """Run hk check; assert exit 1 and that exactly this one id is named on FAIL lines."""
        code, out = self.p.check()
        self.assertEqual(code, 1, out)
        ids = set(re.findall(r"^FAIL (C\d+) ", out, re.M))
        self.assertEqual(ids, set([check_id]), out)
        self.assertIn("HANDOFF FAIL", out)
        return out

    def exclude(self, pattern):
        """Hide a path from git status locally (so an untracked helper file does not trip C1)."""
        path = os.path.join(self.p.work, ".git", "info", "exclude")
        with open(path, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(pattern + "\n")

    def point_state_at_head(self):
        """Commit a State line that names HEAD with check:not-run, so later code changes are not a C5 fault."""
        head = self.p.head()
        text = self.p.read("docs/HANDOFF.md")
        text = re.sub(r"Main: main @ [0-9a-f]+ check:(?:pass|not-run)", "Main: main @ %s check:not-run" % head, text)
        self.p.write("docs/HANDOFF.md", text)
        self.p.commit_push("point state at head")

    def drop_check_evidence(self):
        """Make the Exists evidence a plain sha, so a ledger fault is not also a C9 fault."""
        self.p.replace("docs/HANDOFF.md", "`check:%s`" % self.p.s1, "`%s`" % self.p.s1)

    def assertOk(self):
        """Run hk check; assert exit 0 and HANDOFF OK."""
        code, out = self.p.check()
        self.assertEqual(code, 0, out)
        self.assertIn("HANDOFF OK", out)
        return out


class TestValidProject(ProjectCase):
    """The helper's project is valid."""

    def test_valid_project_is_ok(self):
        """hk check on the valid project prints HANDOFF OK."""
        out = self.assertOk()
        self.assertNotIn("FAIL", out)
        self.assertNotIn("WARN", out)
        self.assertRegex(out.strip().split("\n")[-1], r"^HANDOFF OK main@[0-9a-f]{7} boot=\d+$")

    def test_no_fetch_flag(self):
        """hk check --no-fetch also passes."""
        code, out, _ = self.p.hk("check", "--no-fetch")
        self.assertEqual(code, 0, out)


class TestFaultInjection(ProjectCase):
    """One test per row of the fault-injection table."""

    def test_c1_untracked_file(self):
        """An untracked file in the working tree fails C1."""
        self.p.write("scratch.txt", "x\n")
        self.assertFails("C1")

    def test_c2_commit_not_pushed(self):
        """A commit that is not pushed fails C2."""
        self.p.write("notes.txt", "x\n")
        self.p.commit("local only")
        self.assertFails("C2")

    def test_c3_required_section_deleted(self):
        """A required section deleted fails C3."""
        text = self.p.read("docs/HANDOFF.md")
        cut = text.index("## Machines")
        self.p.write("docs/HANDOFF.md", text[:cut])
        self.p.commit_push("drop Machines")
        out = self.assertFails("C3")
        self.assertIn("Machines", out)

    def test_c3_table_header_malformed(self):
        """A table with a wrong header fails C3."""
        self.p.replace("docs/HANDOFF.md", "| Sev | Finding | File |", "| Sev | Thing | File |")
        self.p.commit_push("bad header")
        self.assertFails("C3")

    def test_c3_over_budget(self):
        """A handoff padded past the budget fails C3."""
        self.p.replace("docs/HANDOFF.md", "Mac only.", "Mac only.\n" + ("padding line\n" * 1200))
        self.p.commit_push("pad")
        out = self.assertFails("C3")
        self.assertIn("budget", out)

    def test_c4_goal_reworded_no_anchor_change(self):
        """Goal reworded with no Anchor change line fails C4."""
        self.p.replace("docs/HANDOFF.md", "Goal: Ship the demo app.", "Goal: Ship something else.")
        self.p.commit_push("reword goal")
        self.assertFails("C4")

    def test_c4_goal_reworded_with_anchor_change_passes(self):
        """Goal reworded with Anchor change D-9 and D-9 in the decisions file passes."""
        self.p.replace("docs/HANDOFF.md", "Goal: Ship the demo app.", "Goal: Ship something else.")
        self.p.replace("docs/HANDOFF.md", "check:pass\n", "check:pass\nAnchor change: D-9\n")
        self.p.write("docs/DECISIONS.md", DECISIONS + "\nD-9 changed the goal.\n")
        self.p.commit_push("reword goal with decision")
        self.assertOk()

    def test_c4_anchor_change_id_not_in_decisions(self):
        """An Anchor change id that is not in the decisions file still fails C4."""
        self.p.replace("docs/HANDOFF.md", "Goal: Ship the demo app.", "Goal: Ship something else.")
        self.p.replace("docs/HANDOFF.md", "check:pass\n", "check:pass\nAnchor change: D-9\n")
        self.p.commit_push("reword goal, no decision")
        self.assertFails("C4")

    def test_c4_anchor_key_empty(self):
        """An empty Anchor key fails C4."""
        self.p.replace("docs/HANDOFF.md", "Decides: Alex", "Decides:")
        self.p.commit_push("empty key")
        self.assertFails("C4")

    def test_c5_sha_not_ancestor(self):
        """A State sha that is not an ancestor of the remote tip fails C5."""
        tree = self.p.git("rev-parse", "HEAD^{tree}")
        stray = self.p.git("commit-tree", tree, "-m", "stray")
        self.p.replace("docs/HANDOFF.md", "Main: main @ %s" % self.p.s1, "Main: main @ %s" % stray[:10])
        self.p.commit_push("bad state sha")
        self.assertFails("C5")

    def test_c5_code_committed_after_state_sha(self):
        """A code file committed after the State sha fails C5."""
        self.p.write("lib/extra.py", "x = 1\n")
        self.p.commit_push("code after state")
        self.assertFails("C5")

    def test_c5_check_pass_without_ledger_row(self):
        """check:pass with the ledger row removed fails C5."""
        self.drop_check_evidence()
        self.p.write("docs/CHECKS.tsv", LEDGER_HEADER)
        self.p.commit_push("remove row")
        self.assertFails("C5")

    def test_c6_next_names_missing_brief(self):
        """Row 1 of Next naming a brief that does not exist fails C6."""
        self.p.replace("docs/HANDOFF.md", "docs/briefs/task-1.md", "docs/briefs/nope.md")
        self.p.commit_push("missing brief")
        self.assertFails("C6")

    def test_c7_read_first_heading_renamed(self):
        """A Read-first ref to a heading that was renamed fails C7."""
        self.p.replace("docs/guide.md", "## Setup", "## Install")
        self.p.commit_push("rename heading")
        self.assertFails("C7")

    def test_c7_read_first_file_only_untracked(self):
        """A Read-first ref to a file that exists only untracked in the working tree fails C7."""
        self.p.write("docs/local-only.md", "# Local\n")
        self.exclude("docs/local-only.md")
        self.p.replace("docs/HANDOFF.md", "| docs/arch.md |", "| docs/arch.md |\n| docs/local-only.md |")
        self.p.git("add", "docs/HANDOFF.md")
        self.p.git("commit", "-q", "-m", "ref to untracked")
        self.p.push()
        self.assertFails("C7")

    def test_c8_read_first_larger_than_boot_budget(self):
        """A Read-first file larger than the boot budget fails C8."""
        self.p.write("docs/big.md", "# Big\n" + ("x" * 41000) + "\n")
        self.p.replace("docs/HANDOFF.md", "| docs/arch.md |", "| docs/arch.md |\n| docs/big.md |")
        self.p.commit_push("big read")
        out = self.assertFails("C8")
        self.assertNotIn("FAIL C7", out)

    def test_c9_status_done(self):
        """An Exists row with status done fails C9."""
        self.p.replace("docs/HANDOFF.md", "| core | verified |", "| core | done |")
        self.p.commit_push("done")
        self.assertFails("C9")

    def test_c9_check_evidence_without_pass_row(self):
        """An Exists check:<sha> with no pass row fails C9."""
        self.p.replace("docs/HANDOFF.md", "`check:%s`" % self.p.s1, "`check:%s`" % self.p.s2)
        self.p.commit_push("evidence without row")
        self.assertFails("C9")

    def test_c9_paths_changed_after_evidence(self):
        """Exists paths changed after the evidence sha fail C9."""
        self.p.write("src/app.py", "print('changed')\n")
        self.p.commit_push("change covered code")
        self.point_state_at_head()
        out = self.assertFails("C9")
        self.assertIn("changed since verified", out)

    def test_c10_parked_branch_deleted_on_remote(self):
        """A Parked branch deleted on the remote fails C10."""
        self.p.git("push", "-q", "origin", "--delete", "feature-x")
        self.assertFails("C10")

    def test_c10_parked_tip_stale(self):
        """A stale Parked tip cell fails C10."""
        self.p.git("checkout", "-q", "feature-x")
        self.p.write("feat2.txt", "more\n")
        self.p.commit("more feature")
        self.p.git("push", "-q", "origin", "feature-x")
        self.p.git("checkout", "-q", "main")
        self.assertFails("C10")

    def test_c11_context_map_missing_heading(self):
        """A CONTEXT_MAP ref to a missing heading fails C11."""
        self.p.replace("docs/CONTEXT_MAP.md", "docs/arch.md#Overview", "docs/arch.md#Nowhere")
        self.p.commit_push("bad map ref")
        self.assertFails("C11")

    def test_c12_hk_not_ignored(self):
        """Removing .hk/ from .gitignore fails C12."""
        shutil.rmtree(self.p.path(".hk"))
        self.p.write(".gitignore", "")
        self.p.commit_push("unignore")
        self.point_state_at_head()
        self.assertFails("C12")

    def test_c13_ledger_row_edited_by_hand(self):
        """A ledger row edited by hand (exit 1 changed to 0) fails C13."""
        code, _, _ = self.p.hk("run", "lint", "--record", "--", sys.executable, "-c", "import sys; sys.exit(1)")
        self.assertEqual(code, 1)
        lines = self.p.read("docs/CHECKS.tsv").split("\n")
        for i, line in enumerate(lines):
            fields = line.split("\t")
            if len(fields) == 10 and fields[4] == "lint":
                self.assertEqual(fields[5], "1")
                fields[5] = "0"
                lines[i] = "\t".join(fields)
        self.p.write("docs/CHECKS.tsv", "\n".join(lines))
        self.p.commit_push("edit row")
        self.assertFails("C13")

    def test_c14_secret_in_handoff(self):
        """A password in the handoff fails C14."""
        self.p.replace("docs/HANDOFF.md", "Mac only.", "Mac only.\npassword: hunter2hunter2")
        self.p.commit_push("secret")
        out = self.assertFails("C14")
        self.assertNotIn("hunter2", out)


class TestDirtyLedger(ProjectCase):
    """A project whose only pass row was made on a dirty tree."""

    dirty_ledger = True

    def test_c5_check_pass_row_made_on_dirty_tree(self):
        """check:pass where the only row was made on a dirty tree fails C5."""
        rows = hk.parse_ledger(self.p.read("docs/CHECKS.tsv"))
        self.assertEqual(rows[0][3], "1")
        self.assertTrue(hk.row_valid(rows[0]))
        self.drop_check_evidence()
        self.p.commit_push("plain evidence")
        self.assertFails("C5")


class TestWarnings(ProjectCase):
    """The WARN line."""

    def test_warn_on_check_not_run(self):
        """State saying check:not-run gives a WARN line but still passes."""
        self.p.replace("docs/HANDOFF.md", "check:pass", "check:not-run")
        self.p.commit_push("not run")
        code, out = self.p.check()
        self.assertEqual(code, 0, out)
        self.assertIn("WARN State says check:not-run", out)


class TestRefs(unittest.TestCase):
    """Ref resolution: exact, prefix, ambiguous, missing, tail."""

    def setUp(self):
        """Make a temp folder holding one markdown file."""
        self.tmp = tempfile.mkdtemp(prefix="hk-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        text = ("# Doc\n\n## 5. Ledger\nledger body\n\n### Sub\nsub body\n\n"
                "## Alpha one\na1\n\n## Alpha two\na2\n\n## Usage\nuse\n\n## Usage notes\nnotes\n"
                "\n```\n## not a heading\n```\n")
        with open(os.path.join(self.tmp, "doc.md"), "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)

    def resolve(self, ref):
        """Resolve a ref against the temp folder's working tree."""
        return hk.resolve_ref(self.tmp, ref, None)

    def test_whole_file(self):
        """A bare path returns the whole file."""
        path, content = self.resolve("doc.md")
        self.assertEqual(path, "doc.md")
        self.assertIn("## Alpha two", content)

    def test_exact_heading_includes_subsections(self):
        """An exact heading match runs to the next heading of the same or higher level."""
        _, content = self.resolve("doc.md#5. Ledger")
        self.assertIn("ledger body", content)
        self.assertIn("sub body", content)
        self.assertNotIn("a1", content)

    def test_exact_heading_case_insensitive(self):
        """Matching ignores case."""
        _, content = self.resolve("doc.md#SUB")
        self.assertEqual(content, "### Sub\nsub body")

    def test_prefix_heading(self):
        """A unique prefix resolves."""
        _, content = self.resolve("doc.md#5. Led")
        self.assertIn("ledger body", content)

    def test_exact_beats_prefix(self):
        """An exact match wins even when another heading starts with the text."""
        _, content = self.resolve("doc.md#Usage")
        self.assertIn("use", content)
        self.assertNotIn("notes", content)

    def test_ambiguous_heading(self):
        """Two prefix matches are an error."""
        with self.assertRaises(hk.HkError) as ctx:
            self.resolve("doc.md#Alpha")
        self.assertIn("ambiguous", str(ctx.exception))

    def test_missing_heading(self):
        """A heading that is not there is an error."""
        with self.assertRaises(hk.HkError):
            self.resolve("doc.md#Nope")

    def test_heading_in_code_fence_ignored(self):
        """A heading inside a code fence does not count."""
        with self.assertRaises(hk.HkError):
            self.resolve("doc.md#not a heading")

    def test_missing_file(self):
        """A file that is not there is an error."""
        with self.assertRaises(hk.HkError):
            self.resolve("absent.md")

    def test_tail(self):
        """path@tail:N returns the last N lines."""
        _, content = self.resolve("doc.md@tail:2")
        self.assertEqual(content, "## not a heading\n```")

    def test_glob_rules(self):
        """Globs: /** means everything under a folder; others use fnmatchcase."""
        self.assertTrue(hk.glob_match("src/**", "src/a/b.py"))
        self.assertFalse(hk.glob_match("src/**", "srcx/a.py"))
        self.assertTrue(hk.glob_match("*.py", "src/a.py"))
        self.assertFalse(hk.glob_match("*.PY", "a.py"))
        self.assertTrue(hk.globs_overlap("src/**", "src/app/**"))
        self.assertTrue(hk.globs_overlap("src/app/**", "src/**"))
        self.assertFalse(hk.globs_overlap("docs/**", "src/**"))


class TestCommands(ProjectCase):
    """The other commands against a valid project."""

    def test_boot_content_and_index(self):
        """boot prints boot sections, Read-first contents and small on-demand sections in full."""
        code, out, _ = self.p.hk("boot")
        self.assertEqual(code, 0, out)
        self.assertIn("## Anchor\nGoal: Ship the demo app.", out)
        self.assertIn("## Never read", out)
        self.assertIn("=== docs/guide.md#Setup (", out)
        self.assertIn("Install it.", out)
        self.assertIn("=== docs/arch.md (", out)
        for name in ("Exists", "Parked", "Findings", "Decisions", "Machines"):
            self.assertIn("## %s\n" % name, out)
        self.assertLess(out.index("## Exists"), out.index("## Parked"))
        self.assertLess(out.index("## Parked"), out.index("## Machines"))
        self.assertIn("Mac only.", out)
        self.assertNotIn("NOT LOADED", out)
        self.assertRegex(out.strip().split("\n")[-1], r"^BOOT \d+ chars, budget 40000$")

    def test_boot_index_for_sections_over_the_limit(self):
        """A section at or over inline_under is indexed only; smaller ones are printed and not indexed."""
        self.p.write("hk.json", '{"inline_under": 100}')
        code, out, _ = self.p.hk("boot")
        self.assertEqual(code, 0, out)
        self.assertIn("NOT LOADED. Fetch a section when a step or question needs it:", out)
        self.assertRegex(out, r"(?m)^Exists: 1 rows, 137 chars \u2014 python3 \S*hk\.py get docs/HANDOFF\.md#Exists$")
        self.assertRegex(out, r"(?m)^Parked: 1 rows, \d+ chars \u2014 python3 \S*hk\.py get docs/HANDOFF\.md#Parked$")
        self.assertNotIn("## Exists", out)
        self.assertNotIn("## Parked", out)
        self.assertIn("## Findings\n", out)
        self.assertIn("## Machines\n", out)
        self.assertNotRegex(out, r"(?m)^Findings: ")
        self.assertNotRegex(out, r"(?m)^Machines: ")

    def test_boot_inline_under_zero_restores_old_output(self):
        """inline_under 0 prints no on-demand section and indexes all five."""
        self.p.write("hk.json", '{"inline_under": 0}')
        code, out, _ = self.p.hk("boot")
        self.assertEqual(code, 0, out)
        self.assertNotIn("## Exists", out)
        self.assertNotIn("## Machines", out)
        self.assertIn("NOT LOADED.", out)
        for name in ("Exists", "Parked", "Findings", "Decisions", "Machines"):
            self.assertRegex(out, r"(?m)^%s: \d+ rows, \d+ chars \u2014 python3 \S*hk\.py get docs/HANDOFF\.md#%s$" % (name, name))

    def test_boot_measure_lists_inlined_sections(self):
        """boot --measure lists each inlined section as its own part, and no index part."""
        code, out, _ = self.p.hk("boot", "--measure")
        self.assertEqual(code, 0, out)
        for name in ("Exists", "Parked", "Findings", "Decisions", "Machines"):
            self.assertRegex(out, r"(?m)^inline %s: \d+$" % name)
        self.assertNotIn("index:", out)

    def test_inline_under_must_be_an_integer(self):
        """inline_under is type-checked like the other numeric keys."""
        self.p.write("hk.json", '{"inline_under": "lots"}')
        code, _, err = self.p.hk("boot")
        self.assertEqual(code, 2)
        self.assertIn("inline_under", err)

    def test_c8_counts_inlined_text(self):
        """C8 counts inlined sections: a budget that fits the old output but not the new one fails."""
        self.p.replace("docs/HANDOFF.md", "D-1 chose sqlite.", "D-1 chose sqlite.\n" + ("x" * 900))
        self.p.commit_push("longer decisions")
        self.point_state_at_head()
        self.p.write("hk.json", '{"inline_under": 0}')
        old_total = int(re.search(r"BOOT (\d+) chars", self.p.hk("boot")[1]).group(1))
        self.p.write("hk.json", "{}")
        new_total = int(re.search(r"BOOT (\d+) chars", self.p.hk("boot")[1]).group(1))
        self.assertGreater(new_total, old_total)
        budget = new_total - 1
        self.p.write("hk.json", '{"budget_boot": %d, "inline_under": 0}' % max(budget, old_total))
        self.p.commit_push("old output fits")
        self.point_state_at_head()
        self.assertOk()
        self.p.write("hk.json", '{"budget_boot": %d}' % budget)
        self.p.commit_push("inlined output does not")
        self.point_state_at_head()
        out = self.assertFails("C8")
        self.assertIn("budget %d" % budget, out)

    def test_boot_measure_prints_sizes_only(self):
        """boot --measure prints a size table and the BOOT line, not the content."""
        code, out, _ = self.p.hk("boot", "--measure")
        self.assertEqual(code, 0, out)
        self.assertNotIn("Install it.", out)
        self.assertIn("docs/arch.md:", out)
        self.assertIn("BOOT ", out)

    def test_boot_measure_over_budget_exits_1(self):
        """boot --measure exits 1 when over budget."""
        self.p.write("hk.json", '{"budget_boot": 100}\n')
        code, out, _ = self.p.hk("boot", "--measure")
        self.assertEqual(code, 1, out)
        self.assertIn("budget 100", out)

    def test_boot_reads_from_git_ref(self):
        """boot --ref reads from the git ref rather than the working tree."""
        self.p.write("docs/arch.md", "# Arch\n\nWORKING TREE ONLY\n")
        code, out, _ = self.p.hk("boot", "--ref", "origin/main")
        self.assertEqual(code, 0, out)
        self.assertNotIn("WORKING TREE ONLY", out)
        code, out, _ = self.p.hk("boot")
        self.assertIn("WORKING TREE ONLY", out)

    def test_branch_prefers_dev_on_remote(self):
        """With a dev branch on the remote, the integration branch is dev."""
        self.p.git("push", "-q", "origin", "main:dev")
        self.p.git("fetch", "-q", "origin")
        self.assertEqual(hk.default_branch(self.p.work, "origin"), "dev")

    def test_get_appends_to_read_log(self):
        """get prints refs under === lines and appends rows to .hk/reads.tsv."""
        code, out, _ = self.p.hk("get", "docs/guide.md#Usage", "docs/arch.md@tail:1")
        self.assertEqual(code, 0, out)
        self.assertIn("=== docs/guide.md#Usage (", out)
        self.assertIn("Tables.", out)
        rows = [l.split("\t") for l in self.p.read(".hk/reads.tsv").strip().split("\n")[1:]]
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0][1:4], ["s1", "hk", "docs/guide.md"])
        self.assertEqual(rows[0][4], str(len("## Usage\nRun it.")))
        self.assertEqual(rows[1][3], "docs/arch.md")

    def test_get_unresolved_ref_exits_2(self):
        """get with a ref that does not resolve exits 2 with one stderr line."""
        code, out, err = self.p.hk("get", "docs/guide.md#Nope")
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertEqual(len(err.strip().split("\n")), 1)

    def test_for_routing(self):
        """for lists deduplicated refs with sizes and a total."""
        code, out, _ = self.p.hk("for", "src/app.py", "src/other.py")
        self.assertEqual(code, 0, out)
        lines = out.strip().split("\n")
        self.assertEqual(len(lines), 4, out)
        self.assertTrue(lines[0].startswith("docs/arch.md#Overview ("))
        self.assertTrue(lines[1].startswith("docs/guide.md#Usage ("))
        self.assertTrue(lines[2].startswith("docs/lessons/core.md ("))
        self.assertTrue(lines[3].startswith("TOTAL "))

    def test_for_print_and_no_match(self):
        """for --print shows contents; a path with no match prints nothing."""
        code, out, _ = self.p.hk("for", "src/app.py", "--print")
        self.assertIn("Big picture.", out)
        code, out, _ = self.p.hk("for", "README.md")
        self.assertEqual((code, out), (0, ""))

    def test_for_without_context_map(self):
        """for with no CONTEXT_MAP prints nothing and exits 0."""
        os.remove(self.p.path("docs/CONTEXT_MAP.md"))
        code, out, _ = self.p.hk("for", "src/app.py")
        self.assertEqual((code, out), (0, ""))

    def test_pack(self):
        """pack prints the brief, anchor lines, routed and brief reading, the report shape and PACK."""
        code, out, _ = self.p.hk("pack", "docs/briefs/task-1.md")
        self.assertEqual(code, 0, out)
        self.assertTrue(out.startswith("# Brief: task-1"))
        self.assertIn("Goal: Ship the demo app.", out)
        self.assertIn("Workflow: briefs to small agents, lead merges", out)
        self.assertIn("Out of scope: payments", out)
        self.assertIn("=== docs/arch.md#Overview (", out)
        self.assertIn("=== docs/lessons/core.md (", out)
        self.assertEqual(out.count("=== docs/guide.md#Usage ("), 1)
        self.assertIn("REPORT (20 lines at most)\nstatus: <word>", out)
        self.assertRegex(out.strip().split("\n")[-1], r"^PACK \d+ chars$")
        code2, out2, _ = self.p.hk("pack", "task-1", "--measure")
        self.assertEqual(code2, 0, out2)
        self.assertNotIn("Do the thing.", out2)
        self.assertIn("PACK ", out2)

    def test_run_writes_row_and_exit_code(self):
        """run writes a correct local ledger row, a log, the tail and the exit code."""
        code, out, _ = self.p.hk("run", "boom", "--tail", "2", "--", sys.executable, "-c",
                                 "print('a'); print('b'); print('c'); raise SystemExit(3)")
        self.assertEqual(code, 1)
        lines = out.strip().split("\n")
        self.assertEqual(lines[:2], ["b", "c"])
        self.assertRegex(lines[2], r"^RESULT fail exit=3 commit=%s dirty=0 secs=\d+\.\d log=\.hk/logs/\S+-boom\.log$"
                         % self.p.s2[:7])
        rows = hk.parse_ledger(self.p.read(".hk/ledger.tsv"))
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row[1:6], ["testbox", self.p.s2, "0", "boom", "3"])
        self.assertTrue(hk.row_valid(row))
        self.assertRegex(row[0], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        log_rel = lines[2].split("log=")[1]
        log_text = self.p.read(log_rel)
        self.assertIn("a", log_text)
        self.assertEqual(row[7], hk.sha12(log_text))
        self.assertEqual(self.p.read("docs/CHECKS.tsv").count("\n"), 2)

    def test_run_pass_and_dirty_flag(self):
        """run exits 0 on success and marks dirty=1 when the tree has changes."""
        self.p.write("scratch.txt", "x\n")
        code, out, _ = self.p.hk("run", "ok", "--", *OK_CMD)
        self.assertEqual(code, 0)
        self.assertRegex(out.strip().split("\n")[-1], r"^RESULT pass exit=0 .* dirty=1 ")
        row = hk.parse_ledger(self.p.read(".hk/ledger.tsv"))[0]
        self.assertEqual(row[3], "1")

    def test_run_needs_command(self):
        """run with nothing after -- exits 2."""
        code, _, err = self.p.hk("run", "x")
        self.assertEqual(code, 2)
        self.assertIn("command", err)

    def make_task_branch(self, files):
        """Create branch task-1 from main with one commit writing the given files, and check it out."""
        self.p.git("checkout", "-q", "-b", "task-1")
        for rel in files:
            self.p.write(rel, "content\n")
        self.p.commit("task work")

    def test_verify_scope_pass_and_accept(self):
        """verify on the checked-out branch passes scope and runs Done when."""
        self.make_task_branch(["src/new.py", "tests/test_new.py"])
        code, out, _ = self.p.hk("verify", "docs/briefs/task-1.md")
        self.assertEqual(code, 0, out)
        lines = out.strip().split("\n")
        self.assertEqual(lines[0], "SCOPE ok (2 files)")
        self.assertRegex(lines[1], r"^RESULT pass exit=0 ")
        row = hk.parse_ledger(self.p.read(".hk/ledger.tsv"))[0]
        self.assertEqual(row[4], "verify-task-1")

    def test_verify_scope_fail(self):
        """verify names each file outside the Touch globs and exits 1."""
        self.make_task_branch(["src/new.py", "README.md", "docs/extra.md"])
        code, out, _ = self.p.hk("verify", "docs/briefs/task-1.md")
        self.assertEqual(code, 1, out)
        self.assertIn("SCOPE FAIL README.md", out)
        self.assertIn("SCOPE FAIL docs/extra.md", out)
        self.assertNotIn("SCOPE FAIL src/new.py", out)

    def test_verify_skips_accept_when_not_checked_out(self):
        """verify off the branch skips acceptance and says which branch to check out."""
        self.make_task_branch(["src/new.py"])
        self.p.git("checkout", "-q", "main")
        code, out, _ = self.p.hk("verify", "docs/briefs/task-1.md")
        self.assertEqual(code, 0, out)
        self.assertEqual(out.strip().split("\n"), ["SCOPE ok (1 files)", "ACCEPT skipped: check out task-1"])

    def test_verify_runs_acceptance_in_the_branch_own_worktree(self):
        """verify from another checkout runs Done when in the worktree that holds the branch."""
        self.make_task_branch(["src/new.py"])
        self.p.git("checkout", "-q", "main")
        self.p.git("worktree", "add", "-q", os.path.join(self.tmp, "task-wt"), "task-1")
        code, out, _ = self.p.hk("verify", "docs/briefs/task-1.md")
        self.assertEqual(code, 0, out)
        self.assertNotIn("ACCEPT skipped", out)
        self.assertRegex(out, r"RESULT pass exit=0 ")

    def test_verify_acceptance_failure_exits_1(self):
        """verify exits 1 when the Done when command fails."""
        self.make_task_branch(["src/new.py"])
        self.p.replace("docs/briefs/task-1.md", quoted_ok_command(),
                       '"%s" -c "import sys; sys.exit(5)"' % sys.executable)
        self.p.commit("failing done-when")
        code, out, _ = self.p.hk("verify", "task-1")
        self.assertEqual(code, 1, out)
        self.assertIn("RESULT fail exit=5", out)

    def test_export_writes_remote_tree_only(self):
        """export writes the remote tree and not untracked or unpushed files."""
        self.p.write("untracked.txt", "x\n")
        self.p.write("unpushed.txt", "y\n")
        self.p.git("add", "unpushed.txt")
        self.p.git("commit", "-q", "-m", "unpushed")
        target = os.path.join(self.tmp, "cold")
        code, out, _ = self.p.hk("export", target)
        self.assertEqual(code, 0, out)
        self.assertTrue(os.path.isfile(os.path.join(target, "docs", "HANDOFF.md")))
        self.assertTrue(os.path.isfile(os.path.join(target, "src", "app.py")))
        self.assertFalse(os.path.exists(os.path.join(target, "untracked.txt")))
        self.assertFalse(os.path.exists(os.path.join(target, "unpushed.txt")))
        self.assertFalse(os.path.exists(os.path.join(target, ".hk")))
        code, _, err = self.p.hk("export", target)
        self.assertEqual(code, 2, "second export into a non-empty folder must fail")

    def test_key(self):
        """key prints the mechanical answers."""
        code, out, _ = self.p.hk("key")
        self.assertEqual(code, 0, out)
        lines = out.strip().split("\n")
        self.assertEqual(lines[0], "Goal: Ship the demo app.")
        self.assertEqual(lines[1], "Milestone: M1 walking skeleton")
        self.assertEqual(lines[2], "Step 1: Build the thing | brief docs/briefs/task-1.md | model smallest")
        self.assertEqual(lines[3], "Read first: docs/guide.md#Setup; docs/arch.md")
        self.assertEqual(lines[4], "Never read: vendor/**")
        self.assertEqual(lines[5], "State: Main: main @ %s check:pass" % self.p.s1)
        self.assertEqual(lines[6], "Parked: 1")
        self.assertEqual(lines[7], "Findings: 0")

    def test_quiz(self):
        """quiz prints the six fixed questions."""
        code, out, _ = self.p.hk("quiz")
        self.assertEqual(code, 0)
        self.assertEqual(len(out.strip().split("\n")), 6)
        self.assertIn("6. How many parked branches", out)

    def test_facts(self):
        """facts prints the integration tips, unmerged branches, ledgers, reads and worktrees."""
        self.p.hk("get", "docs/DECISIONS.md")
        self.p.hk("get", "docs/DECISIONS.md")
        self.p.hk("get", "docs/lessons/core.md")
        code, out, _ = self.p.hk("facts")
        self.assertEqual(code, 0, out)
        self.assertIn("integration main: local %s remote %s" % (self.p.s2[:7], self.p.s2[:7]), out)
        self.assertIn("since handoff: 0 commits", out)
        self.assertIn("  feature-x ", out)
        self.assertIn("ledger docs/CHECKS.tsv (last 5 of 1)", out)
        self.assertIn(" full exit=0 commit=%s dirty=0 testbox" % self.p.s1[:7], out)
        self.assertIn("  docs/DECISIONS.md x2", out)
        self.assertNotIn("docs/lessons/core.md x", out)
        self.assertIn("0 changed", out)

    def test_runs_from_subfolder(self):
        """hk finds the repository root from a subfolder."""
        code, out, _ = self.p.hk("quiz", cwd=self.p.path("src"))
        self.assertEqual(code, 0)
        code, out, _ = self.p.hk("key", cwd=self.p.path("src"))
        self.assertEqual(code, 0, out)

    def test_outside_repository_exits_2(self):
        """hk outside a git repository exits 2."""
        outside = tempfile.mkdtemp(prefix="hk-test-")
        self.addCleanup(shutil.rmtree, outside, True)
        code, _, err = self.p.hk("key", cwd=outside)
        self.assertEqual(code, 2)
        self.assertIn("not inside a git repository", err)


ROW1 = "| 1 | Build the thing | `docs/briefs/task-1.md` | smallest | tests pass |"


class HookCase(ProjectCase):
    """Base for hook tests: run a hook with JSON on stdin in the valid project."""

    def hook(self, name, payload=None, raw=None):
        """Run `hk <name>` with a JSON payload (or raw bytes) on stdin. Return (code, stdout, stderr)."""
        if raw is None:
            import json
            raw = json.dumps(payload).encode("utf-8")
        proc = subprocess.run([sys.executable, HK, name], cwd=self.p.work, env=TEST_ENV,
                              input=raw, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return proc.returncode, proc.stdout.decode("utf-8", "replace"), proc.stderr.decode("utf-8", "replace")

    def read_event(self, rel, tool="Read", agent=False, session="sess1", **extra):
        """Build a hook payload for a Read/Grep/Glob of a repository path."""
        key = "file_path" if tool == "Read" else "path"
        tool_input = {key: self.p.path(rel)}
        tool_input.update(extra)
        event = {"session_id": session, "cwd": self.p.work, "hook_event_name": "PreToolUse",
                 "tool_name": tool, "tool_input": tool_input}
        if agent:
            event["agent_id"] = "agent-1"
        return event

    def reads_rows(self):
        """Return the rows of .hk/reads.tsv (without the header) as lists of fields."""
        if not os.path.isfile(self.p.path(".hk/reads.tsv")):
            return []
        lines = self.p.read(".hk/reads.tsv").strip().split("\n")[1:]
        return [l.split("\t") for l in lines]

    def cite(self, section_row_after, new_row):
        """Add a table row to the working-tree handoff after a given row text."""
        self.p.replace("docs/HANDOFF.md", section_row_after, section_row_after + "\n" + new_row)


class TestHooks(HookCase):
    """Spec section 8 and amendment A3 for hook-read; hook-anchor."""

    def test_never_read_path_exits_2_with_message(self):
        """A never-read path exits 2 with the exact message."""
        self.p.write("vendor/x.txt", "secret stuff\n")
        code, out, err = self.hook("hook-read", self.read_event("vendor/x.txt"))
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertEqual(err.strip(), "Never-read path: vendor/x.txt. It is on the handoff's Never read "
                                      "list. Ask the owner before opening it.")
        self.assertEqual(self.reads_rows(), [])

    def test_never_read_applies_to_subagents(self):
        """A subagent's Read of a never-read path also exits 2."""
        self.p.write("vendor/x.txt", "x\n")
        code, _, _ = self.hook("hook-read", self.read_event("vendor/x.txt", agent=True))
        self.assertEqual(code, 2)

    def test_grep_on_never_read_folder_exits_2(self):
        """Grep on a never-read folder exits 2."""
        self.p.write("vendor/x.txt", "x\n")
        code, _, err = self.hook("hook-read", self.read_event("vendor", tool="Grep", pattern="x"))
        self.assertEqual(code, 2)
        self.assertIn("Never-read path: vendor.", err)
        code, _, _ = self.hook("hook-read", self.read_event("vendor", tool="Glob"))
        self.assertEqual(code, 2)

    def test_allowed_read_appends_row_with_source(self):
        """An allowed Read exits 0 silently and appends one row with source main, then agent."""
        code, out, err = self.hook("hook-read", self.read_event("docs/guide.md"))
        self.assertEqual((code, out, err), (0, "", ""))
        rows = self.reads_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][1:], ["sess1", "main", "docs/guide.md", str(len(GUIDE))])
        self.hook("hook-read", self.read_event("docs/guide.md", agent=True))
        rows = self.reads_rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1][2], "agent")

    def test_grep_and_outside_paths_log_nothing(self):
        """Grep of an allowed path and a Read outside the repository exit 0 and log nothing."""
        code, _, _ = self.hook("hook-read", self.read_event("docs", tool="Grep"))
        self.assertEqual(code, 0)
        outside = os.path.join(self.tmp, "elsewhere.txt")
        with open(outside, "w", encoding="utf-8") as fh:
            fh.write("x")
        event = self.read_event("docs/guide.md")
        event["tool_input"]["file_path"] = outside
        code, _, _ = self.hook("hook-read", event)
        self.assertEqual(code, 0)
        self.assertEqual(self.reads_rows(), [])

    def test_read_limit_sets_chars(self):
        """With limit, chars is limit x 80 capped at the file size."""
        self.p.write("docs/long.txt", "y" * 1000)
        self.hook("hook-read", self.read_event("docs/long.txt", limit=3))
        self.hook("hook-read", self.read_event("docs/long.txt", limit=100))
        rows = self.reads_rows()
        self.assertEqual([r[4] for r in rows], ["240", "1000"])

    def test_missing_path_exits_0(self):
        """A payload without a path exits 0."""
        event = self.read_event("docs/guide.md")
        event["tool_input"] = {}
        self.assertEqual(self.hook("hook-read", event)[0], 0)

    def test_agent_reads_do_not_count_toward_budget(self):
        """An agent call does not count toward the budget."""
        self.p.write("hk.json", '{"read_budget": 1000}\n')
        self.p.write("docs/long.txt", "y" * 3000)
        code, out, _ = self.hook("hook-read", self.read_event("docs/long.txt", agent=True))
        self.assertEqual((code, out), (0, ""))
        code, out, _ = self.hook("hook-read", self.read_event("docs/guide.md"))
        self.assertEqual((code, out), (0, ""))

    def test_budget_announced_once_per_level(self):
        """Crossing the budget prints the JSON once, not again on the next read, and again at +25%."""
        import json
        self.p.write("hk.json", '{"read_budget": 1000}\n')
        self.p.write("docs/a.txt", "y" * 1000)
        self.p.write("docs/b.txt", "y" * 300)
        code, out, _ = self.hook("hook-read", self.read_event("docs/a.txt"))
        self.assertEqual(code, 0)
        data = json.loads(out)["hookSpecificOutput"]
        self.assertEqual(data["hookEventName"], "PreToolUse")
        self.assertEqual(data["permissionDecision"], "allow")
        self.assertEqual(data["additionalContext"],
                         "Read budget: 1000 of 1000 characters read by the main session. "
                         "Hand off at the next clean point, or delegate further reading.")
        code, out, _ = self.hook("hook-read", self.read_event("docs/guide.md"))
        self.assertEqual((code, out), (0, ""))
        code, out, _ = self.hook("hook-read", self.read_event("docs/b.txt"))
        self.assertIn("of 1000 characters", out)
        code, out, _ = self.hook("hook-read", self.read_event("docs/guide.md"))
        self.assertEqual(out, "")
        self.assertTrue(os.path.isfile(self.p.path(".hk/budget.sess1")))
        code, out, _ = self.hook("hook-read", self.read_event("docs/a.txt", session="other"))
        self.assertIn("Read budget", out)

    def test_malformed_stdin_exits_0(self):
        """Malformed, empty and non-object stdin exit 0 with no output, for both hooks."""
        for name in ("hook-read", "hook-anchor"):
            for raw in (b"not json {", b"", b"[1, 2]"):
                code, out, err = self.hook(name, raw=raw)
                self.assertEqual((code, out, err), (0, "", ""))

    def test_hook_outside_repository_exits_0(self):
        """A cwd that is not a repository exits 0 silently."""
        code, out, err = self.hook("hook-read", {"cwd": self.tmp, "tool_name": "Read",
                                                 "tool_input": {"file_path": "x"}})
        self.assertEqual((code, out, err), (0, "", ""))

    def test_hook_anchor_prints_anchor_and_row_1(self):
        """hook-anchor prints Anchor, the Main line, row 1 of Next and the reminder."""
        code, out, _ = self.hook("hook-anchor", {"cwd": self.p.work, "hook_event_name": "SessionStart"})
        self.assertEqual(code, 0)
        lines = out.strip().split("\n")
        self.assertEqual(lines[0], "## Anchor")
        self.assertIn("Goal: Ship the demo app.", lines)
        self.assertIn("Decides: Alex", lines)
        self.assertIn("Main: main @ %s check:pass" % self.p.s1, lines)
        self.assertIn("Next 1: 1 | Build the thing | docs/briefs/task-1.md | smallest | tests pass", lines)
        self.assertEqual(lines[-1], "Re-read the anchor. Work the Next table does not call for goes to the backlog.")

    def test_hook_anchor_without_handoff_prints_nothing(self):
        """hook-anchor with no handoff prints nothing and exits 0."""
        os.remove(self.p.path("docs/HANDOFF.md"))
        code, out, _ = self.hook("hook-anchor", {"cwd": self.p.work})
        self.assertEqual((code, out), (0, ""))

    def test_a3_hook_allows_exact_cited_file(self):
        """A3: hook-read allows the exact file a Read when ref cites; the rest of the folder stays blocked."""
        self.p.write("vendor/ok.md", "# Ok\n")
        self.p.write("vendor/other.md", "# Other\n")
        self.cite("| editing src | `docs/lessons/core.md` |", "| vendored api | `vendor/ok.md#Ok` |")
        self.assertEqual(self.hook("hook-read", self.read_event("vendor/ok.md"))[0], 0)
        self.assertEqual(self.hook("hook-read", self.read_event("vendor/other.md"))[0], 2)


class TestAmendments(HookCase):
    """Section 9: A1 to A5."""

    def make_review_branch(self):
        """Push branch m0 holding docs/rev.md, then return to main (the file is not in the working tree)."""
        self.p.git("checkout", "-q", "-b", "m0")
        self.p.write("docs/rev.md", "# Rev\n\n## Findings\nF1 found.\n")
        self.p.commit("review")
        self.p.git("push", "-q", "origin", "m0")
        self.p.git("checkout", "-q", "main")
        self.assertFalse(os.path.exists(self.p.path("docs/rev.md")))

    def test_a1_get_resolves_branch_qualified_ref(self):
        """A1: get reads a branch-qualified ref from the remote branch, whatever --ref says."""
        self.make_review_branch()
        code, out, err = self.p.hk("get", "m0:docs/rev.md#Findings")
        self.assertEqual(code, 0, err)
        self.assertIn("=== m0:docs/rev.md#Findings (", out)
        self.assertIn("F1 found.", out)
        code, out, err = self.p.hk("get", "--ref", "origin/main", "m0:docs/rev.md#Findings")
        self.assertEqual(code, 0, err)
        self.assertIn("F1 found.", out)
        rows = self.reads_rows_plain()
        self.assertEqual(rows[0][3], "docs/rev.md")

    def reads_rows_plain(self):
        """Return the reads.tsv rows (without header)."""
        lines = self.p.read(".hk/reads.tsv").strip().split("\n")[1:]
        return [l.split("\t") for l in lines]

    def test_a1_unknown_prefix_is_plain_path(self):
        """A1: text before ':' that is not a remote branch is not a branch qualifier."""
        code, _, err = self.p.hk("get", "nobranch:docs/guide.md")
        self.assertEqual(code, 2)
        self.assertIn("file not found", err)

    def test_a1_boot_reads_qualified_ref(self):
        """A1: boot includes a branch-qualified Read-first ref."""
        self.make_review_branch()
        self.cite("| docs/arch.md |", "| m0:docs/rev.md#Findings |")
        code, out, err = self.p.hk("boot")
        self.assertEqual(code, 0, err)
        self.assertIn("=== m0:docs/rev.md#Findings (", out)
        self.assertIn("F1 found.", out)

    def test_a1_check_passes_with_qualified_ref(self):
        """A1: check resolves a branch-qualified Read-first ref at the remote."""
        self.make_review_branch()
        self.cite("| docs/arch.md |", "| m0:docs/rev.md#Findings |")
        self.p.commit_push("cite review branch")
        self.assertOk()

    def test_a1_check_c7_missing_file_on_branch(self):
        """A1: a qualified ref to a file missing on that branch fails C7."""
        self.make_review_branch()
        self.cite("| docs/arch.md |", "| m0:docs/nope.md |")
        self.p.commit_push("bad qualified ref")
        self.assertFails("C7")

    def test_a1_check_c7_missing_heading_on_branch(self):
        """A1: a qualified ref to a missing heading fails C7."""
        self.make_review_branch()
        self.cite("| docs/arch.md |", "| m0:docs/rev.md#Nowhere |")
        self.p.commit_push("bad qualified heading")
        self.assertFails("C7")

    def test_a1_check_c7_qualified_file_only_on_main_not_on_branch(self):
        """A1: the qualified ref reads the branch, not main: a file only on main fails C7."""
        self.make_review_branch()
        self.cite("| docs/arch.md |", "| m0:docs/guide.md#Setup |")
        self.p.commit_push("file is on main, not m0? it is on both")
        # docs/guide.md was branched from main, so it exists on m0 as well: passes.
        self.assertOk()
        self.p.write("docs/only-main.md", "# Only\n")
        self.cite("| m0:docs/guide.md#Setup |", "| m0:docs/only-main.md |")
        self.p.commit_push("only on main")
        self.assertFails("C7")

    def test_a2_get_refuses_unsafe_paths(self):
        """A2: get refuses absolute paths, '..' segments and a leading '-' with exit 2."""
        outside = os.path.join(self.tmp, "outside.md")
        with open(outside, "w", encoding="utf-8") as fh:
            fh.write("# Outside\n")
        for ref in (outside, "../outside.md", "docs/../../outside.md", "-x.md", "docs/../docs/guide.md"):
            code, out, err = self.p.hk("get", "--", ref)
            self.assertEqual(code, 2, ref)
            self.assertEqual(out, "", ref)
            self.assertEqual(len(err.strip().split("\n")), 1, ref)

    def check_unsafe_read_first(self, ref):
        """Put an unsafe ref in Read first, push, and assert C7 fails."""
        self.cite("| docs/arch.md |", "| %s |" % ref)
        self.p.commit_push("unsafe ref")
        out = self.assertFails("C7")
        return out

    def test_a2_check_fails_absolute_ref(self):
        """A2: an absolute Read-first ref fails C7."""
        outside = os.path.join(self.tmp, "outside.md")
        with open(outside, "w", encoding="utf-8") as fh:
            fh.write("# Outside\n")
        self.check_unsafe_read_first(outside.replace("\\", "/"))

    def test_a2_check_fails_dotdot_ref(self):
        """A2: a Read-first ref with a '..' segment fails C7."""
        self.check_unsafe_read_first("docs/../docs/guide.md")

    def test_a2_check_fails_leading_dash_ref(self):
        """A2: a Read-first ref starting with '-' fails C7."""
        self.check_unsafe_read_first("-x.md")

    def test_a2_check_fails_unsafe_context_map_ref(self):
        """A2: an unsafe CONTEXT_MAP ref fails C11."""
        self.p.replace("docs/CONTEXT_MAP.md", "docs/arch.md#Overview", "../x.md")
        self.p.commit_push("unsafe map")
        self.assertFails("C11")

    def test_a3_get_refuses_never_read_path(self):
        """A3: get refuses a Never-read path with one stderr line and exit 2."""
        self.p.write("vendor/x.txt", "x\n")
        code, out, err = self.p.hk("get", "vendor/x.txt")
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertEqual(len(err.strip().split("\n")), 1)
        self.assertIn("vendor/x.txt", err)
        self.assertFalse(os.path.exists(self.p.path(".hk/reads.tsv")))

    def test_a3_get_allows_exact_cited_file(self):
        """A3: get allows the exact file cited in Read when, but not its neighbours."""
        self.p.write("vendor/ok.md", "# Ok\nfine\n")
        self.p.write("vendor/other.md", "# Other\n")
        self.cite("| editing src | `docs/lessons/core.md` |", "| vendored api | `vendor/ok.md#Ok` |")
        code, out, err = self.p.hk("get", "vendor/ok.md")
        self.assertEqual(code, 0, err)
        self.assertIn("fine", out)
        code, _, _ = self.p.hk("get", "vendor/other.md")
        self.assertEqual(code, 2)

    def run_done_when(self, done_line, extra_commit=True):
        """Rewrite the brief's Done when line, branch task-1 with one commit, and run hk verify."""
        self.p.git("checkout", "-q", "-b", "task-1")
        self.p.write("src/new.py", "x = 1\n")
        text = self.p.read("docs/briefs/task-1.md")
        lines = [("Done when: " + done_line) if l.startswith("Done when:") else l for l in text.split("\n")]
        lines = [("Touch: src/**, docs/**") if l.startswith("Touch:") else l for l in lines]
        self.p.write("docs/briefs/task-1.md", "\n".join(lines))
        self.p.git("add", "-A")
        self.p.git("commit", "-q", "-m", "work")
        return self.p.hk("verify", "docs/briefs/task-1.md")

    def test_a4_two_commands_second_fails(self):
        """A4: two commands, the second fails: two RESULT lines and exit 1."""
        py = '"%s"' % sys.executable
        done = "`%s -c \"print('one')\"` then `%s -c \"import sys; sys.exit(4)\"`" % (py, py)
        code, out, _ = self.run_done_when(done)
        self.assertEqual(code, 1, out)
        results = [l for l in out.split("\n") if l.startswith("RESULT")]
        self.assertEqual(len(results), 2, out)
        self.assertTrue(results[0].startswith("RESULT pass exit=0"))
        self.assertTrue(results[1].startswith("RESULT fail exit=4"))

    def test_a4_stops_at_first_failure(self):
        """A4: the first command fails, so the second is not run."""
        py = '"%s"' % sys.executable
        done = "`%s -c \"import sys; sys.exit(2)\"` `%s -c \"print('never')\"`" % (py, py)
        code, out, _ = self.run_done_when(done)
        self.assertEqual(code, 1, out)
        self.assertEqual(len([l for l in out.split("\n") if l.startswith("RESULT")]), 1)

    def test_a4_two_commands_both_pass(self):
        """A4: two passing commands give two RESULT pass lines and exit 0."""
        py = '"%s"' % sys.executable
        done = "`%s -c \"print(1)\"` `%s -c \"print(2)\"`" % (py, py)
        code, out, _ = self.run_done_when(done)
        self.assertEqual(code, 0, out)
        self.assertEqual(out.count("RESULT pass"), 2)

    def test_a4_brief_without_backticked_command_does_not_parse(self):
        """A4: a Done when line with no backticked command does not parse."""
        text = BRIEF.format(cmd="x").replace("Done when: `x`", "Done when: run the tests")
        with self.assertRaises(hk.HkError):
            hk.parse_brief(text)
        self.p.write("docs/briefs/task-1.md", text)
        code, _, err = self.p.hk("pack", "docs/briefs/task-1.md")
        self.assertEqual(code, 2)
        self.assertIn("Done when", err)

    def test_a5_row_1_legacy_brief_fails_c6(self):
        """A5: the brief of row 1 must parse; a legacy one fails C6."""
        self.p.write("docs/briefs/task-1.md", "# Legacy brief\nfree text\n")
        self.p.commit_push("legacy row 1")
        self.assertFails("C6")

    def test_a5_later_row_legacy_brief_warns_and_passes(self):
        """A5: a later-row brief that exists but does not parse gives WARN C6 and still passes."""
        self.p.write("docs/briefs/old.md", "# Old plan\nno fields here\n")
        self.cite(ROW1, "| 2 | Old step | `docs/briefs/old.md` | middle | later |")
        self.p.commit_push("legacy later row")
        out = self.assertOk()
        self.assertIn("WARN C6 docs/briefs/old.md: not in brief format", out)

    def test_a5_later_row_missing_brief_fails_c6(self):
        """A5: a later-row brief that does not exist fails C6."""
        self.cite(ROW1, "| 2 | Ghost step | `docs/briefs/ghost.md` | middle | later |")
        self.p.commit_push("missing later brief")
        self.assertFails("C6")

    def test_a5_later_rows_with_empty_brief_pass(self):
        """Real-world case: later Next rows with an empty Brief cell pass."""
        self.cite(ROW1, "| 2 | Think about it | | | someday |\n| 3 | Plan more | - | - | maybe |")
        self.p.commit_push("rows without briefs")
        out = self.assertOk()
        self.assertNotIn("WARN", out)



def can_symlink(tmp):
    """True if this machine can make symlinks (some Windows set-ups cannot)."""
    probe = os.path.join(tmp, "probe-link")
    try:
        os.symlink("target", probe)
    except (OSError, NotImplementedError, AttributeError):
        return False
    os.remove(probe)
    return True


class TestRound3(HookCase):
    """Round 3: defects proved by the adversarial review, one test per binding decision."""

    def push_extra_branch(self, name):
        """Push a copy of main to the remote under another branch name."""
        self.p.git("push", "-q", "origin", "main:%s" % name)

    def need_symlinks(self):
        """Skip the test where symlinks cannot be made."""
        if not can_symlink(self.tmp):
            self.skipTest("symlinks not available")

    def commit_symlink(self, rel, target):
        """Commit and push a symlink at rel pointing to target."""
        full = self.p.path(rel)
        os.symlink(target, full)
        self.p.commit_push("symlink")

    # -- 1. renames ---------------------------------------------------------

    def test_r1_c9_sees_moved_file_old_path(self):
        """1: a file moved out of an Exists path still counts as changed there (C9)."""
        os.makedirs(self.p.path("lib"))
        self.p.git("mv", "src/app.py", "lib/app.py")
        self.p.commit_push("move app out of src")
        self.point_state_at_head()
        out = self.assertFails("C9")
        self.assertIn("src/app.py", out)

    def test_r1_verify_scope_sees_moved_file_old_path(self):
        """1: verify lists the old path of a moved file as out of scope."""
        self.p.git("checkout", "-q", "-b", "task-1")
        self.p.git("mv", "docs/guide.md", "src/guide.md")
        self.p.commit("move into scope")
        code, out, _ = self.p.hk("verify", "docs/briefs/task-1.md")
        self.assertEqual(code, 1, out)
        self.assertIn("SCOPE FAIL docs/guide.md", out)

    # -- 2. option injection ------------------------------------------------

    def test_r2_hk_json_unsafe_names_exit_2(self):
        """2: hk.json remote or branch that could be a git option exits 2 with one line."""
        for body in ('{"branch": "--upload-pack=x"}', '{"remote": "-x"}', '{"branch": "a..b"}'):
            self.p.write("hk.json", body)
            code, out, err = self.p.hk("boot")
            self.assertEqual(code, 2, body)
            self.assertEqual(len(err.strip().split("\n")), 1, err)
            self.assertIn("safe name", err)

    def test_r2_check_fails_unsafe_working_tree_names(self):
        """2: hk check turns an unsafe hk.json name into a C3 FAIL before any git call."""
        self.p.write("hk.json", '{"branch": "--output=evil.txt"}')
        code, out, err = self.p.hk("check")
        self.assertEqual(code, 1, out + err)
        self.assertIn("FAIL C3", out)
        self.assertNotIn("Traceback", err)
        self.assertFalse(os.path.exists(self.p.path("evil.txt")))

    def test_r2_unsafe_ref_option_and_brief_branch(self):
        """2: --ref starting with '-' and a brief Branch starting with '-' are refused."""
        code, _, err = self.p.hk("get", "--ref=--foo", "docs/guide.md")
        self.assertEqual(code, 2)
        self.assertIn("bad git ref", err)
        self.p.replace("docs/briefs/task-1.md", "Branch: task-1", "Branch: --evil")
        code, _, err = self.p.hk("pack", "docs/briefs/task-1.md")
        self.assertEqual(code, 2)
        self.assertIn("branch name", err)
        code, _, err = self.p.hk("verify", "docs/briefs/task-1.md", "--ref", "--evil")
        self.assertEqual(code, 2)

    def test_r2_parked_branch_cell_unsafe(self):
        """2: a Parked Branch cell that is not a safe name fails C10."""
        self.p.replace("docs/HANDOFF.md", "| feature-x |", "| --foo |")
        self.p.commit_push("unsafe parked")
        self.assertFails("C10")

    # -- 3. config source ---------------------------------------------------

    def test_r3_check_ignores_working_tree_hk_json(self):
        """3: a working-tree hk.json (other keys) does not change what check reads."""
        self.exclude("hk.json")
        self.p.write("hk.json", '{"budget_boot": 10}')
        self.assertOk()

    def test_r3_check_uses_committed_hk_json(self):
        """3: hk.json at the remote ref does apply (budget_boot 10 fails C8)."""
        self.p.write("hk.json", '{"budget_boot": 10}')
        self.p.commit_push("tight boot budget")
        self.point_state_at_head()
        self.assertFails("C8")

    def test_r3_working_tree_branch_must_match_remote_file(self):
        """3: remote and branch taken from the working tree must agree with the remote hk.json."""
        self.push_extra_branch("other")
        self.exclude("hk.json")
        self.p.write("hk.json", '{"branch": "other"}')
        code, out = self.p.check()
        self.assertEqual(code, 1, out)
        self.assertIn("FAIL C3 hk.json branch differs", out)

    # -- 4. hk.json types ---------------------------------------------------

    def test_r4_wrong_types_exit_2_without_traceback(self):
        """4: a wrong type in hk.json is one stderr line and exit 2."""
        for body in ('{"budget_boot": "big"}', '{"doc_paths": 5}', '{"read_budget": true}',
                     '{"handoff": 7}', '[1]', '{"main_check": ""}'):
            self.p.write("hk.json", body)
            code, out, err = self.p.hk("boot")
            self.assertEqual(code, 2, body)
            self.assertNotIn("Traceback", err)
            self.assertEqual(len(err.strip().split("\n")), 1, err)

    def test_r4_check_fails_c3_on_committed_bad_type(self):
        """4: a bad hk.json type at the remote ref is a C3 FAIL in check, not a traceback."""
        self.p.write("hk.json", '{"budget_boot": "big", "doc_paths": "docs/"}')
        self.p.commit_push("bad types")
        code, out, err = self.p.hk("check")
        self.assertNotIn("Traceback", err)
        self.assertEqual(code, 1, out)
        self.assertIn("FAIL C3 hk.json budget_boot", out)
        self.assertIn("FAIL C3 hk.json doc_paths", out)

    # -- 5. duplicates ------------------------------------------------------

    def test_r5_duplicate_anchor_key_fails_c3(self):
        """5: a repeated Anchor key fails C3."""
        self.p.replace("docs/HANDOFF.md", "Goal: Ship the demo app.\n", "Goal: Ship the demo app.\nGoal: Ship the demo app.\n")
        self.p.commit_push("dup goal")
        self.assertFails("C3")

    def test_r5_second_main_line_fails_c3(self):
        """5: a second Main: line fails C3."""
        self.p.replace("docs/HANDOFF.md", "check:pass\n", "check:pass\nMain: main @ %s check:pass\n" % self.p.s1)
        self.p.commit_push("dup main")
        self.assertFails("C3")

    def test_r5_second_anchor_change_line_fails_c3(self):
        """5: a second Anchor change: line fails C3."""
        self.p.replace("docs/HANDOFF.md", "check:pass\n", "check:pass\nAnchor change: D-1\nAnchor change: D-1\n")
        self.p.commit_push("dup change")
        self.assertFails("C3")

    def test_r5_key_and_hook_anchor_do_not_pick_one(self):
        """5: key refuses a handoff with a repeated Goal; hook-anchor shows both Main lines and a WARN."""
        self.p.replace("docs/HANDOFF.md", "Goal: Ship the demo app.\n", "Goal: A.\nGoal: B.\n")
        code, out, err = self.p.hk("key")
        self.assertEqual(code, 2)
        self.assertIn("Goal", err)
        self.p.replace("docs/HANDOFF.md", "check:pass\n", "check:pass\nMain: main @ %s check:fail\n" % self.p.s1)
        code, out, _ = self.hook("hook-anchor", {"cwd": self.p.work})
        self.assertEqual(code, 0)
        self.assertIn("Goal: A.", out)
        self.assertIn("Goal: B.", out)
        self.assertEqual(out.count("Main: main @"), 2)
        self.assertIn("WARN Anchor: 'Goal' appears 2 times", out)

    # -- 6. table-only sections, pipe lines ---------------------------------

    def test_r6_prose_line_in_table_section_fails_c3(self):
        """6: a non-table line inside a table section fails C3."""
        self.p.replace("docs/HANDOFF.md", ROW1, ROW1 + "\nsee the notes for step two")
        self.p.commit_push("prose in table")
        self.assertFails("C3")

    def test_r6_context_map_pipe_line_without_leading_pipe(self):
        """6: a CONTEXT_MAP line with ' | ' that does not start with '|' fails C11."""
        self.p.replace("docs/CONTEXT_MAP.md", "| `src/**` |", "x | `src/**` |")
        self.p.commit_push("broken row")
        self.assertFails("C11")

    def test_r6_brief_prose_with_pipe_is_fine_and_fields_and_fences_too(self):
        """6: a brief prose line holding ' | ' is not a table row, so it is no error (nor are fields and code fences)."""
        path = "docs/briefs/task-1.md"
        good = self.p.read(path)
        self.p.write(path, good + "\na | b\n")
        self.assertEqual(self.p.hk("pack", path)[0], 0)
        self.p.write(path, good + "\n```\na | b\n```\n")
        self.assertEqual(self.p.hk("pack", path)[0], 0)
        self.p.write(path, good.replace("Read: docs/guide.md#Usage", "Read: docs/guide.md#Usage\nDone when: `echo a | cat`"))
        self.assertEqual(self.p.hk("pack", path)[0], 0)

    # -- 7. commit ids ------------------------------------------------------

    def test_r7_hex_named_tag_is_not_evidence(self):
        """7: a tag named like hex does not pass as an Exists evidence sha."""
        self.p.git("tag", "abcdef1", self.p.s1)
        self.p.replace("docs/HANDOFF.md", "`check:%s`" % self.p.s1, "`abcdef1`")
        self.p.commit_push("tag evidence")
        self.assertFails("C9")

    def test_r7_hex_named_tag_is_not_state_sha(self):
        """7: a tag named like hex does not pass as the State sha."""
        self.p.git("tag", "abcdef1", self.p.s1)
        self.p.replace("docs/HANDOFF.md", "Main: main @ %s" % self.p.s1, "Main: main @ abcdef1")
        self.p.commit_push("tag state")
        self.assertFails("C5")

    def test_r7_evidence_not_on_remote_branch(self):
        """7: an evidence commit reachable from no remote-tracking branch fails C9."""
        self.p.git("checkout", "-q", "-b", "side")
        self.p.write("side.txt", "x\n")
        self.p.commit("side")
        side = self.p.head()
        self.p.git("checkout", "-q", "main")
        self.p.replace("docs/HANDOFF.md", "`check:%s`" % self.p.s1, "`%s`" % side)
        self.p.commit_push("local-only evidence")
        self.assertFails("C9")

    # -- 8. parked ----------------------------------------------------------

    def test_r8_parked_branch_with_revision_syntax(self):
        """8: a Parked Branch cell with revision syntax (feature-x~0) fails C10."""
        self.p.replace("docs/HANDOFF.md", "| feature-x |", "| feature-x~0 |")
        self.p.commit_push("rev syntax")
        self.assertFails("C10")

    # -- 9. symlinks --------------------------------------------------------

    def test_r9_get_refuses_symlinked_file_and_directory(self):
        """9: get refuses a symlinked file and a path through a symlinked directory, with one line."""
        self.need_symlinks()
        outside = os.path.join(self.tmp, "outside")
        os.makedirs(outside)
        with open(os.path.join(outside, "secret.md"), "w", encoding="utf-8") as fh:
            fh.write("# Outside secret\n")
        os.symlink(os.path.join(outside, "secret.md"), self.p.path("docs/link.md"))
        os.symlink(outside, self.p.path("docs/ldir"))
        os.symlink("arch.md", self.p.path("docs/inlink.md"))
        for ref in ("docs/link.md", "docs/ldir/secret.md", "docs/inlink.md"):
            code, out, err = self.p.hk("get", ref)
            self.assertEqual(code, 2, ref)
            self.assertEqual(out, "")
            self.assertEqual(len(err.strip().split("\n")), 1)
            self.assertNotIn("Outside secret", err)

    def test_r9_boot_key_pack_refuse_symlinks(self):
        """9: boot (Read first), key (handoff) and pack (brief Read) refuse symlinked files."""
        self.need_symlinks()
        os.remove(self.p.path("docs/arch.md"))
        os.symlink(os.path.join(self.tmp, "nowhere.md"), self.p.path("docs/arch.md"))
        code, _, err = self.p.hk("boot")
        self.assertEqual(code, 2)
        self.assertIn("symlink", err)
        code, _, err = self.p.hk("pack", "docs/briefs/task-1.md")
        self.assertEqual(code, 2)
        copy = os.path.join(self.tmp, "handoff-copy.md")
        shutil.copy(self.p.path("docs/HANDOFF.md"), copy)
        os.remove(self.p.path("docs/HANDOFF.md"))
        os.symlink(copy, self.p.path("docs/HANDOFF.md"))
        code, _, err = self.p.hk("key")
        self.assertEqual(code, 2)
        self.assertIn("symlink", err)

    def test_r9_hook_read_refuses_symlink_path(self):
        """9: hook-read exits 2 for a path through a symlink inside the repository."""
        self.need_symlinks()
        os.symlink(os.path.join(self.tmp, "elsewhere"), self.p.path("docs/ldir"))
        code, out, err = self.hook("hook-read", self.read_event("docs/ldir/x.md"))
        self.assertEqual(code, 2)
        self.assertIn("Symlink path refused: docs/ldir/x.md", err)
        self.assertEqual(self.reads_rows(), [])

    def test_r9_check_fails_c7_on_symlink_at_remote_ref(self):
        """9: a cited file that is a symlink (mode 120000) at the remote ref fails C7."""
        self.need_symlinks()
        self.commit_symlink("docs/link.md", "arch.md")
        self.p.replace("docs/HANDOFF.md", "| docs/arch.md |", "| docs/arch.md |\n| docs/link.md |")
        self.p.commit_push("cite symlink")
        out = self.assertFails("C7")
        self.assertIn("symlink", out)

    # -- 10. dirty detection ------------------------------------------------

    def test_r10_assume_unchanged_counts_as_dirty(self):
        """10: a tracked file hidden with assume-unchanged makes C1 fail and hk run dirty=1."""
        self.p.git("update-index", "--assume-unchanged", "docs/guide.md")
        self.p.write("docs/guide.md", GUIDE + "changed\n")
        self.assertEqual(self.p.git("status", "--porcelain"), "")
        self.assertFails("C1")
        code, out, _ = self.p.hk("run", "x", "--", *OK_CMD)
        self.assertIn("dirty=1", out)

    def test_r10_skip_worktree_counts_as_dirty(self):
        """10: skip-worktree also counts as dirty."""
        self.p.git("update-index", "--skip-worktree", "docs/guide.md")
        self.p.write("docs/guide.md", GUIDE + "changed\n")
        self.assertFails("C1")

    def test_r10_untracked_hidden_by_config_still_counts(self):
        """10: status.showUntrackedFiles=no does not hide an untracked file from C1."""
        self.p.git("config", "status.showUntrackedFiles", "no")
        self.p.write("docs/new.md", "x\n")
        self.assertFails("C1")

    # -- 11. C14 ------------------------------------------------------------

    def test_r11_secret_value_on_next_line(self):
        """11: a value on the line after the key is caught, and never printed."""
        self.p.replace("docs/HANDOFF.md", "Mac only.", "Mac only.\npassword:\nhunter2hunter2")
        self.p.commit_push("secret next line")
        out = self.assertFails("C14")
        self.assertNotIn("hunter2", out)

    # -- 12. export ---------------------------------------------------------

    def test_r12_export_skips_symlinks(self):
        """12: export skips symlink members, naming each, and does not crash on an absolute link."""
        self.need_symlinks()
        self.commit_symlink("docs/abs-link.md", "/etc/hosts")
        target = os.path.join(self.tmp, "cold")
        code, out, err = self.p.hk("export", target)
        self.assertEqual(code, 0, err)
        self.assertNotIn("Traceback", err)
        self.assertIn("skipped symlink docs/abs-link.md", out.split("\n"))
        self.assertFalse(os.path.lexists(os.path.join(target, "docs", "abs-link.md")))
        self.assertTrue(os.path.isfile(os.path.join(target, "docs", "HANDOFF.md")))

    def test_r12_export_error_is_one_line_exit_2(self):
        """12: an extraction error is one line and exit 2, not a traceback."""
        blocker = os.path.join(self.tmp, "afile")
        with open(blocker, "w", encoding="utf-8") as fh:
            fh.write("x")
        code, out, err = self.p.hk("export", os.path.join(blocker, "sub"))
        self.assertEqual(code, 2)
        self.assertNotIn("Traceback", err)
        self.assertEqual(len(err.strip().split("\n")), 1)

    # -- 13. C12 ------------------------------------------------------------

    def test_r13_check_ignore_must_succeed(self):
        """13: .hk/ in the text of .gitignore is not enough if a later rule un-ignores it."""
        shutil.rmtree(self.p.path(".hk"))
        self.p.write(".gitignore", ".hk/\n!.hk/\n")
        self.p.commit_push("negate ignore")
        self.point_state_at_head()
        out = self.assertFails("C12")
        self.assertIn("check-ignore", out)

    # -- 14. C4 walk --------------------------------------------------------

    def test_r14_unrecorded_goal_change_is_not_forgotten_by_next_commit(self):
        """14: a goal change with no decision still fails after an unrelated later commit."""
        self.p.replace("docs/HANDOFF.md", "Goal: Ship the demo app.", "Goal: Ship something else.")
        self.p.commit_push("reword goal")
        self.p.replace("docs/HANDOFF.md", "Mac only.", "Mac and PC.")
        self.p.commit_push("unrelated edit")
        self.assertFails("C4")

    def test_r14_reverted_goal_change_passes(self):
        """14: a goal reworded by mistake and restored in the next commit passes C4."""
        self.p.replace("docs/HANDOFF.md", "Goal: Ship the demo app.", "Goal: Ship something else.")
        self.p.commit_push("reword goal by mistake")
        self.p.replace("docs/HANDOFF.md", "Goal: Ship something else.", "Goal: Ship the demo app.")
        self.p.commit_push("restore goal")
        self.assertOk()

    def test_r14_change_id_needs_a_digit(self):
        """14: an Anchor change id with no digit is not valid even if it is in the decisions file."""
        self.p.replace("docs/HANDOFF.md", "Goal: Ship the demo app.", "Goal: Ship something else.")
        self.p.replace("docs/HANDOFF.md", "check:pass\n", "check:pass\nAnchor change: nope\n")
        self.p.write("docs/DECISIONS.md", DECISIONS + "\nnope was never numbered.\n")
        self.p.commit_push("no-digit id")
        self.assertFails("C4")

    def test_r14_walk_stops_after_version_with_valid_change(self):
        """14: the walk stops at the older version that carries an Anchor change line."""
        self.p.replace("docs/HANDOFF.md", "Goal: Ship the demo app.", "Goal: Ship something else.")
        self.p.replace("docs/HANDOFF.md", "check:pass\n", "check:pass\nAnchor change: D-9\n")
        self.p.write("docs/DECISIONS.md", DECISIONS + "\nD-9 changed the goal.\n")
        self.p.commit_push("goal with decision")
        self.p.replace("docs/HANDOFF.md", "Anchor change: D-9\n", "")
        self.p.replace("docs/HANDOFF.md", "Mac only.", "Mac and PC.")
        self.p.commit_push("drop line, unrelated edit")
        self.assertOk()

    # -- 15. Main branch ----------------------------------------------------

    def test_r15_main_line_branch_must_be_integration_branch(self):
        """15: a Main line naming another branch fails C5."""
        self.p.replace("docs/HANDOFF.md", "Main: main @", "Main: feature-x @")
        self.p.commit_push("wrong branch")
        self.assertFails("C5")

    # -- 16. section order --------------------------------------------------

    def test_r16_sections_out_of_order_fail_c3(self):
        """16: sections in a different order than the spec fail C3."""
        text = self.p.read("docs/HANDOFF.md")
        a = text.index("## Findings")
        b = text.index("## Decisions")
        c = text.index("## Parked")
        swapped = text[:c] + text[a:b] + text[c:a] + text[b:]
        self.p.write("docs/HANDOFF.md", swapped)
        self.p.commit_push("swap Parked and Findings")
        out = self.assertFails("C3")
        self.assertIn("out of order", out)

    # -- 17. dev branch default honoured by check ---------------------------

    def test_r17_check_honours_dev_branch_default(self):
        """17: with a dev branch on the remote, check itself checks dev."""
        self.p.git("checkout", "-q", "-b", "dev")
        self.p.replace("docs/HANDOFF.md", "Main: main @", "Main: dev @")
        self.p.commit("handoff on dev")
        self.p.git("push", "-q", "-u", "origin", "dev")
        code, out = self.p.check()
        self.assertEqual(code, 0, out)
        self.assertRegex(out.strip().split("\n")[-1], r"^HANDOFF OK dev@[0-9a-f]{7} boot=\d+$")

    # -- 18. outside git ----------------------------------------------------

    def export_cold(self):
        """Export the project into a plain folder and return its path."""
        target = os.path.join(self.tmp, "cold")
        code, out, err = self.p.hk("export", target)
        self.assertEqual(code, 0, err)
        self.assertFalse(os.path.exists(os.path.join(target, ".git")))
        return target

    def test_r18_boot_and_key_work_in_an_export_folder(self):
        """18: boot and key run from the files on disk inside an exported folder."""
        cold = self.export_cold()
        code, out, err = self.p.hk("boot", cwd=cold)
        self.assertEqual(code, 0, err)
        self.assertIn("Goal: Ship the demo app.", out)
        self.assertIn("Install it.", out)
        code, out, err = self.p.hk("key", cwd=os.path.join(cold, "docs"))
        self.assertEqual(code, 0, err)
        self.assertIn("Goal: Ship the demo app.", out)
        code, out, _ = self.p.hk("quiz", cwd=cold)
        self.assertEqual(code, 0)
        code, out, err = self.p.hk("for", "src/app.py", cwd=cold)
        self.assertEqual(code, 0, err)
        self.assertIn("docs/arch.md#Overview", out)

    def test_r18_git_commands_and_qualified_refs_fail_cleanly_outside_git(self):
        """18: git-only commands exit 2 with one line; a branch-qualified ref names itself in the error."""
        cold = self.export_cold()
        for args in (("check",), ("facts",), ("run", "x", "--", "echo"), ("export", "out")):
            code, out, err = self.p.hk(*args, cwd=cold)
            self.assertEqual(code, 2, args)
            self.assertEqual(len(err.strip().split("\n")), 1, err)
            self.assertIn("git", err)
        code, _, err = self.p.hk("get", "m0:docs/guide.md", cwd=cold)
        self.assertEqual(code, 2)
        self.assertIn("m0:docs/guide.md", err)


LEARNED_ROW = "| %s | 2026-11-19 | note text | seen twice | 2026-11-19 |"
LEARNED_TOP = "# Learned\n\n| Id | Added | Note | Evidence | Last seen |\n|---|---|---|---|---|\n"
METRIC_DEFAULTS = {"time": "t", "machine": "m", "commit": "c", "boot_chars": "1000",
                   "handoff_chars": "500", "first_fails": "-", "offmap": "-", "fetched": "-",
                   "unused_when": "0", "budget_level": "0", "coldstart": "-", "learned_ids": "-"}


class TestRound4(HookCase):
    """Round 4: self-tuning (section 10) and the stale Anchor change fix."""

    def metrics(self, rows):
        """Write docs/HK_METRICS.tsv from a list of dicts (missing columns take defaults)."""
        lines = ["\t".join(hk.METRICS_COLUMNS)]
        for row in rows:
            merged = dict(METRIC_DEFAULTS)
            merged.update(row)
            lines.append("\t".join(merged[c] for c in hk.METRICS_COLUMNS))
        self.p.write("docs/HK_METRICS.tsv", "\n".join(lines) + "\n")

    def learned_file(self, ids):
        """Write docs/HK_LEARNED.md with one row per id."""
        self.p.write("docs/HK_LEARNED.md", LEARNED_TOP + "\n".join(LEARNED_ROW % i for i in ids) + "\n")

    def tune(self):
        """Run hk tune; return its stdout (exit code must be 0)."""
        code, out, err = self.p.hk("tune")
        self.assertEqual(code, 0, err)
        return out

    # -- tune rules ---------------------------------------------------------

    def test_t1_offmap_twice(self):
        """T1 fires for a file off the map in 2 rows, silent for 1."""
        self.metrics([{"offmap": "docs/x.md"}, {"offmap": "docs/x.md;docs/y.md"}])
        out = self.tune()
        self.assertIn("PROPOSE add Read-when row for docs/x.md \u2014 evidence: offmap in 2 of last 2 rows", out)
        self.assertNotIn("docs/y.md", out)
        self.metrics([{"offmap": "docs/x.md"}, {}])
        self.assertEqual(self.tune(), "")

    def test_t2_unfetched_read_when(self):
        """T2 fires when a Read-when ref was never fetched in 6 rows; silent with 5 rows or one fetch."""
        self.metrics([{}] * 6)
        self.assertIn("PROPOSE drop Read-when row docs/lessons/core.md \u2014 evidence:", self.tune())
        self.metrics([{}] * 5)
        self.assertEqual(self.tune(), "")
        self.metrics([{}] * 5 + [{"fetched": "docs/lessons/core.md"}])
        self.assertEqual(self.tune(), "")

    def test_t3_repeated_first_fail(self):
        """T3 fires when an id is in first_fails in 2 of the last 5 rows; silent for 1."""
        self.metrics([{"first_fails": "C4"}, {}, {"first_fails": "C4,C7"}])
        out = self.tune()
        self.assertIn("PROPOSE add learned note: C4 keeps failing first time: "
                      "anchor empty or changed without a recorded decision \u2014 evidence: "
                      "C4 in first_fails in 2 of last 3 rows", out)
        self.assertNotIn("C7 keeps", out)
        self.metrics([{"first_fails": "C4"}, {}, {}])
        self.assertEqual(self.tune(), "")

    def test_t4_boot_rising_or_near_budget(self):
        """T4 fires on three rising boot sizes or above 80% of budget; silent on flat sizes."""
        self.metrics([{"boot_chars": "1000"}, {"boot_chars": "1100"}, {"boot_chars": "1200"}])
        out = self.tune()
        self.assertIn("PROPOSE trim Read first: largest part is docs/arch.md (", out)
        self.assertIn("boot_chars rising 1000->1100->1200", out)
        self.metrics([{"boot_chars": "1000"}] * 3)
        self.assertEqual(self.tune(), "")
        self.p.write("hk.json", '{"budget_boot": 1000}')
        self.metrics([{"boot_chars": "900"}] * 3)
        self.assertIn("above 80% of budget 1000", self.tune())

    def test_t5_coldstart_below_full_marks(self):
        """T5 fires when coldstart is below full marks in 2 of the last 3 rows; silent otherwise."""
        self.metrics([{"coldstart": "4/6"}, {"coldstart": "6/6"}, {"coldstart": "5/6"}])
        self.assertIn("PROPOSE rewrite the boot sections the cold-start reader got wrong \u2014 evidence:", self.tune())
        self.metrics([{"coldstart": "4/6"}, {"coldstart": "6/6"}, {"coldstart": "6/6"}])
        self.assertEqual(self.tune(), "")

    def test_t6_learned_note_without_effect(self):
        """T6 fires for a learned note active 5 rows whose id still fails first; silent when it stopped failing."""
        self.learned_file(["C4"])
        self.metrics([{"learned_ids": "C4"}] * 3 + [{"learned_ids": "C4", "first_fails": "C4"}] * 2)
        out = self.tune()
        self.assertIn("PROPOSE remove learned note C4: no effect \u2014 evidence: active for 5 rows", out)
        self.metrics([{"learned_ids": "C4"}] * 5)
        self.assertNotIn("remove learned note", self.tune())

    def test_t7_learned_note_not_seen(self):
        """T7 fires for a learned note whose id is absent from first_fails for 10 rows; silent for 9."""
        self.learned_file(["C4"])
        quiet = {"learned_ids": "C4", "fetched": "docs/lessons/core.md"}
        self.metrics([quiet] * 10)
        self.assertIn("PROPOSE retire learned note C4: no longer needed \u2014 evidence:", self.tune())
        self.metrics([quiet] * 9)
        self.assertEqual(self.tune(), "")

    def test_t8_budget_level(self):
        """T8 fires when budget_level >= 1 in 2 of the last 3 rows; silent for 1."""
        self.metrics([{"budget_level": "1"}, {}, {"budget_level": "2"}])
        self.assertIn("PROPOSE delegate more reading: main session over its read budget \u2014 evidence:", self.tune())
        self.metrics([{"budget_level": "1"}, {}, {}])
        self.assertEqual(self.tune(), "")

    def test_tune_without_metrics_is_silent_and_applies_nothing(self):
        """tune with no metrics file prints nothing and writes nothing."""
        self.assertEqual(self.tune(), "")
        self.assertFalse(os.path.exists(self.p.path("docs/HK_METRICS.tsv")))

    def test_tune_prints_at_most_12_lines(self):
        """tune never prints more than 12 lines."""
        names = ";".join("docs/f%02d.md" % i for i in range(10))
        self.metrics([{"offmap": names}] * 2 + [{"first_fails": "C1,C2,C3,C4,C5,C6,C7"}] * 2)
        self.assertLessEqual(len(self.tune().strip().split("\n")), 12)

    # -- record -------------------------------------------------------------

    def seed_logs(self):
        """Seed .hk/check_runs.tsv, .hk/reads.tsv and a budget file."""
        self.p.write(".hk/check_runs.tsv", "t\tc1\tC4,C7\nt\tc2\tC7\nt\tc3\t\nt\tc4\tC9\n")
        self.p.write(".hk/reads.tsv", "time\tsession\tsource\tpath\tchars\tref\n"
                     "t\ts1\tmain\tdocs/other.md\t10\t\n"
                     "t\ts1\tmain\tdocs/other.md\t10\t\n"
                     "t\ts1\tagent\tdocs/guide.md\t5\t\n"
                     "t\ts1\thk\tdocs/guide.md\t5\tdocs/guide.md#Setup\n")
        self.p.write(".hk/budget.s1", "2\n")

    def test_record_writes_row_and_truncates_logs(self):
        """record writes the right metrics row from seeded logs, then empties both logs."""
        self.seed_logs()
        code, boot_out, _ = self.p.hk("boot")
        boot_n = re.search(r"BOOT (\d+) chars", boot_out).group(1)
        self.p.hk("check")
        self.p.write(".hk/check_runs.tsv", "t\tc1\tC4,C7\nt\tc2\tC7\nt\tc3\t\nt\tc4\tC9\n")
        code, out, err = self.p.hk("record", "--coldstart", "5/6")
        self.assertEqual(code, 0, err)
        rows = [l.split("\t") for l in self.p.read("docs/HK_METRICS.tsv").strip().split("\n")]
        self.assertEqual(rows[0], hk.METRICS_COLUMNS)
        row = dict(zip(hk.METRICS_COLUMNS, rows[1]))
        self.assertEqual(row["machine"], "testbox")
        self.assertEqual(row["commit"], self.p.s2)
        self.assertEqual(row["boot_chars"], boot_n)
        self.assertEqual(row["handoff_chars"], str(len(self.p.read("docs/HANDOFF.md"))))
        self.assertEqual(row["first_fails"], "C4,C7")
        self.assertEqual(row["offmap"], "docs/other.md")
        self.assertEqual(row["fetched"], "docs/guide.md#Setup")
        self.assertEqual(row["unused_when"], "1")
        self.assertEqual(row["budget_level"], "2")
        self.assertEqual(row["coldstart"], "5/6")
        self.assertEqual(row["learned_ids"], "-")
        self.assertEqual(os.path.getsize(self.p.path(".hk/reads.tsv")), 0)
        self.assertEqual(os.path.getsize(self.p.path(".hk/check_runs.tsv")), 0)

    def test_record_lists_learned_ids_and_default_coldstart(self):
        """record puts the active learned ids in the row and '-' for coldstart when not given."""
        self.learned_file(["C4", "C7-note"])
        code, out, err = self.p.hk("record")
        self.assertEqual(code, 0, err)
        row = dict(zip(hk.METRICS_COLUMNS, self.p.read("docs/HK_METRICS.tsv").strip().split("\n")[1].split("\t")))
        self.assertEqual(row["learned_ids"], "C4,C7-note")
        self.assertEqual(row["coldstart"], "-")
        self.assertEqual(row["first_fails"], "-")

    def test_record_rejects_bad_coldstart(self):
        """record --coldstart must look like N/M."""
        code, _, err = self.p.hk("record", "--coldstart", "lots")
        self.assertEqual(code, 2)
        self.assertFalse(os.path.exists(self.p.path("docs/HK_METRICS.tsv")))

    def test_check_appends_check_runs_rows(self):
        """check appends one row per run: time, commit, FAIL ids joined by a comma."""
        self.assertOk()
        self.p.write("docs/HK_LEARNED.md", LEARNED_TOP + (LEARNED_ROW % "C4").replace("seen twice", "") + "\n")
        self.p.commit_push("learned row without evidence")
        self.assertFails("C15")
        rows = [l.split("\t") for l in self.p.read(".hk/check_runs.tsv").strip().split("\n")]
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0][1], self.p.s2)
        self.assertEqual(rows[0][2] if len(rows[0]) > 2 else "", "")
        self.assertEqual(rows[1][2], "C15")

    # -- C15, learned, version ----------------------------------------------

    def test_c15_too_many_rows(self):
        """C15: a learned file with 21 rows fails."""
        self.learned_file(["C%d" % i for i in range(1, 22)])
        self.p.commit_push("too many")
        out = self.assertFails("C15")
        self.assertIn("21 rows > 20", out)

    def test_c15_too_many_chars(self):
        """C15: a learned file over 2000 characters fails."""
        self.p.write("docs/HK_LEARNED.md", LEARNED_TOP + (LEARNED_ROW % "C4").replace("note text", "x" * 2100) + "\n")
        self.p.commit_push("too long")
        out = self.assertFails("C15")
        self.assertIn("chars > 2000", out)

    def test_c15_empty_evidence(self):
        """C15: a row with an empty Evidence cell fails."""
        self.p.write("docs/HK_LEARNED.md", LEARNED_TOP + "| C4 | 2026-11-19 | note | | 2026-11-19 |\n")
        self.p.commit_push("no evidence")
        out = self.assertFails("C15")
        self.assertIn("empty Evidence", out)

    def test_c15_good_learned_file_passes(self):
        """C15: a learned file within the caps passes, and `hk learned` lists it."""
        self.learned_file(["C4", "C7"])
        self.p.commit_push("learned")
        self.assertOk()
        code, out, _ = self.p.hk("learned")
        self.assertEqual(code, 0)
        lines = out.strip().split("\n")
        self.assertEqual(lines[0], "C4 | 2026-11-19 | note text | seen twice | 2026-11-19")
        self.assertRegex(lines[-1], r"^LEARNED 2 rows, \d+ chars$")

    def test_learned_with_no_files(self):
        """learned with no learned files prints just the count line."""
        code, out, _ = self.p.hk("learned")
        self.assertEqual(out.strip(), "LEARNED 0 rows, 0 chars")

    def test_version_prints_constant_and_sha(self):
        """version prints the constant and the first 12 hex of the sha256 of hk.py."""
        import hashlib
        with open(HK, "rb") as fh:
            digest = hashlib.sha256(fh.read()).hexdigest()[:12]
        code, out, _ = self.p.hk("version")
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "%s %s" % (hk.VERSION, digest))

    def test_version_warning(self):
        """check warns when ~/.claude/skills/handoff/hk.py differs, and not when it is identical."""
        folder = os.path.join(FAKE_HOME, ".claude", "skills", "handoff")
        os.makedirs(folder)
        self.addCleanup(shutil.rmtree, os.path.join(FAKE_HOME, ".claude"), True)
        other = os.path.join(folder, "hk.py")
        with open(other, "w", encoding="utf-8") as fh:
            fh.write("# an older copy\n")
        code, out = self.p.check()
        self.assertEqual(code, 0, out)
        self.assertIn("WARN version: project copy differs from %s" % other.replace("\\", "/"), out)
        shutil.copy(HK, other)
        code, out = self.p.check()
        self.assertEqual(code, 0, out)
        self.assertNotIn("WARN version", out)

    # -- stale Anchor change (b) --------------------------------------------

    def reword_goal(self, goal, change_id, decision):
        """Reword the Goal with an Anchor change line and its decision, commit and push."""
        text = self.p.read("docs/HANDOFF.md")
        text = re.sub(r"Goal: .*", "Goal: %s" % goal, text, count=1)
        text = re.sub(r"Anchor change: \S+\n", "", text)
        text = text.replace("check:pass\n", "check:pass\nAnchor change: %s\n" % change_id, 1)
        self.p.write("docs/HANDOFF.md", text)
        self.p.write("docs/DECISIONS.md", self.p.read("docs/DECISIONS.md") + "\n%s reworded.\n" % decision)
        self.p.commit_push("goal %s" % goal)

    def test_b_stale_anchor_change_id_is_not_valid_twice(self):
        """An Anchor change id the previous version already carried cannot excuse a second change."""
        self.reword_goal("Goal two.", "D-9", "D-9")
        self.assertOk()
        text = self.p.read("docs/HANDOFF.md").replace("Goal: Goal two.", "Goal: Goal three.")
        self.p.write("docs/HANDOFF.md", text)
        self.p.commit_push("goal three, same id")
        out = self.assertFails("C4")
        self.assertIn("already in the previous version", out)

    def test_b_fresh_anchor_change_id_passes(self):
        """A new id for a second change passes."""
        self.reword_goal("Goal two.", "D-9", "D-9")
        self.reword_goal("Goal three.", "D-10", "D-10")
        self.assertOk()


class TestRound5(ProjectCase):
    """Round 5: kit issues of 2026-11-20 (procedure, Touch globs, checks, budgets, Base, headings, jobs, machines, external refs)."""

    def config(self, commit=False, **kw):
        """Write hk.json with the given keys: hidden from git status, or committed and pushed."""
        import json
        if commit:
            kw.setdefault("doc_paths", ["docs/", "START_PROMPT.md", "hk.json"])
        else:
            self.exclude("hk.json")
        self.p.write("hk.json", json.dumps(kw))
        if commit:
            self.p.commit_push("hk.json")

    def py(self, code):
        """Return a shell command string running Python code."""
        return '"%s" -c "%s"' % (sys.executable, code)

    # -- 1. procedure -------------------------------------------------------

    def test_i1_procedure_lines_appended_to_pack(self):
        """1: hk.json procedure (path + lines) is appended to every pack."""
        self.p.write("docs/proc.md", "line1\nRED FIRST\nOUTCOME MAP\nline4\n")
        self.config(procedure={"path": "docs/proc.md", "lines": "2-3"})
        code, out, err = self.p.hk("pack", "task-1")
        self.assertEqual(code, 0, out + err)
        self.assertIn("RED FIRST\nOUTCOME MAP", out)
        self.assertNotIn("line1", out)
        self.assertNotIn("line4", out)

    def test_i1_procedure_absolute_file_whole(self):
        """1: a plain string procedure may be an absolute file, appended whole."""
        ext = os.path.join(self.tmp, "wave.txt")
        with open(ext, "w") as fh:
            fh.write("DISCIPLINE TEXT\n")
        self.config(procedure=ext)
        code, out, err = self.p.hk("pack", "task-1")
        self.assertEqual(code, 0, out + err)
        self.assertIn("DISCIPLINE TEXT", out)

    # -- 2. Touch globs -----------------------------------------------------

    def test_i2_touch_prose_fails_with_item(self):
        """2: a Touch item that is prose is a FAIL naming the item."""
        self.p.replace("docs/briefs/task-1.md", "Touch: src/**, tests/**",
                       "Touch: ONLY the module src/x.py, tests/**")
        code, out, err = self.p.hk("pack", "task-1")
        self.assertEqual(code, 2, out)
        self.assertIn("Touch", err)
        self.assertIn("ONLY the module src/x.py", err)

    # -- 3. checks map ------------------------------------------------------

    def test_i3_run_resolves_label_from_checks(self):
        """3: hk run full with no command uses hk.json checks.full."""
        self.config(checks={"full": self.py("print(42)")})
        code, out, err = self.p.hk("run", "full")
        self.assertEqual(code, 0, out + err)
        self.assertIn("42", out)
        self.assertEqual(hk.parse_ledger(self.p.read(".hk/ledger.tsv"))[0][4], "full")

    def test_i3_run_without_command_exits_nonzero(self):
        """3: no command and no checks entry never exits 0, also with an empty --."""
        for args in (("run", "full"), ("run", "full", "--"), ("run", "other")):
            code, out, err = self.p.hk(*args)
            self.assertNotEqual(code, 0, args)
        self.config(checks={"full": self.py("print(1)")})
        code, out, err = self.p.hk("run", "other")
        self.assertNotEqual(code, 0)

    # -- 4. budgets ---------------------------------------------------------

    def test_i4_budget_ref_warns_and_budget_pack_fails(self):
        """4: refs above budget_ref WARN; a pack above budget_pack FAILs."""
        self.config(budget_ref=10)
        code, out, err = self.p.hk("pack", "task-1")
        self.assertEqual(code, 0, out + err)
        self.assertRegex(out, r"WARN ref docs/arch.md#Overview is \d+ chars > budget_ref 10")
        self.config(budget_pack=50)
        code, out, err = self.p.hk("pack", "task-1")
        self.assertEqual(code, 1, out)
        self.assertRegex(out, r"PACK \d+ chars > budget_pack 50")

    # -- 2026-11-21 kit faults ----------------------------------------------

    def test_pack_warns_over_default_budget_naming_three_biggest_map_refs(self):
        """pack over the default 50000 chars (budget_pack unset) WARNs with the 3 biggest map refs; exit 0."""
        for name in ("a", "b", "c", "d"):
            self.p.write("docs/%s.md" % name, "# T\n" + "x" * (20000 if name != "d" else 100))
        self.p.replace("docs/CONTEXT_MAP.md", "docs/arch.md#Overview;",
                       "docs/a.md; docs/b.md; docs/c.md; docs/d.md; docs/arch.md#Overview;")
        self.p.commit("big refs")
        code, out, err = self.p.hk("pack", "task-1")
        self.assertEqual(code, 0, out + err)
        warn = [l for l in out.split("\n") if l.startswith("WARN pack ")]
        self.assertEqual(len(warn), 1, out)
        for name in ("docs/a.md", "docs/b.md", "docs/c.md"):
            self.assertIn(name, warn[0])
        self.assertNotIn("docs/d.md", warn[0])

    # -- 5. verify base -----------------------------------------------------

    def merged_branch(self, base_field):
        """task-1 forks, main moves on, task-1 merges main, local main is then left behind."""
        self.p.git("checkout", "-q", "-b", "task-1")
        self.p.write("src/new.py", "x\n")
        self.p.commit("task work")
        self.p.git("checkout", "-q", "main")
        self.p.write("README.md", "other card\n")
        self.p.commit("other card on main")
        self.p.git("checkout", "-q", "task-1")
        self.p.git("merge", "-q", "--no-edit", "main")
        self.p.git("update-ref", "refs/heads/main", self.p.s2)
        if base_field:
            self.p.replace("docs/briefs/task-1.md", "Touch:", "Base: %s\nTouch:" % self.p.s2)
            self.p.commit("brief base")

    def test_i5_verify_ignores_merged_in_integration_files(self):
        """5: files merged in from the integration branch are not SCOPE FAIL (fork point)."""
        self.merged_branch(False)
        code, out, _ = self.p.hk("verify", "docs/briefs/task-1.md")
        self.assertIn("SCOPE ok (1 files)", out)
        self.assertNotIn("SCOPE FAIL", out)

    def test_i5_verify_uses_base_field(self):
        """5: a Base: field names the commit the scope is measured from."""
        self.merged_branch(True)
        code, out, _ = self.p.hk("verify", "docs/briefs/task-1.md")
        self.assertNotIn("SCOPE FAIL README.md", out)
        self.assertIn("SCOPE", out)
        self.assertNotIn("SCOPE FAIL", out.replace("SCOPE FAIL docs/briefs/task-1.md", ""))

    # -- 6. brief heading rule ----------------------------------------------

    def test_i6_task_not_first_names_heading(self):
        """6: when ## Task is not the first ## section the error names the first heading."""
        self.p.replace("docs/briefs/task-1.md", "## Task", "## Procedure\nx\n\n## Task")
        code, out, err = self.p.hk("pack", "task-1")
        self.assertEqual(code, 2)
        self.assertIn("## Procedure", err)
        self.assertIn("first", err)

    # -- 7. doc_paths files -------------------------------------------------

    def test_i7_doc_paths_may_list_files(self):
        """7: doc_paths may name a file such as .gitignore; FORMAT.md says so."""
        self.point_state_at_head()
        self.p.write(".gitignore", ".hk/\n*.tmp\n")
        self.p.commit_push("ignore tmp")
        self.assertFails("C5")
        self.config(commit=True, doc_paths=["docs/", "hk.json", ".gitignore"])
        self.assertOk()
        fmt = os.path.join(os.path.dirname(HK), "FORMAT.md")
        with open(fmt, encoding="utf-8") as fh:
            self.assertIn(".gitignore", fh.read())

    # -- 8. Running / jobs --------------------------------------------------

    def test_i8_running_line_and_jobs_in_facts(self):
        """8: State may carry Running: lines (not counted as extra); facts lists .hk/jobs.tsv."""
        text = self.p.read("docs/HANDOFF.md")
        text = text.replace("check:pass\n", "check:pass\n" + "".join(
            "Running: job %d on pc\n" % i for i in range(12)), 1)
        self.p.write("docs/HANDOFF.md", text)
        self.p.commit_push("running")
        self.assertOk()
        sections, _ = hk.parse_handoff(text)
        self.assertEqual(len(hk.state_info(sections)["running"]), 12)
        self.p.write(".hk/jobs.tsv", "started\tmachine\tjob\n2026-11-20\tpc\tocr batch 3\n")
        code, out, err = self.p.hk("facts")
        self.assertEqual(code, 0, err)
        self.assertIn("jobs (.hk/jobs.tsv", out)
        self.assertIn("ocr batch 3", out)

    # -- 9. machines_check --------------------------------------------------

    def test_i9_machines_check_refuses_run(self):
        """9: a failing machines_check stops hk run before the command and the ledger row."""
        self.config(machines_check=self.py("print('pc offline'); import sys; sys.exit(3)"))
        code, out, err = self.p.hk("run", "x", "--", *OK_CMD)
        self.assertEqual(code, 2, out)
        self.assertIn("machines_check", err)
        self.assertIn("pc offline", err)
        self.assertFalse(os.path.exists(self.p.path(".hk/ledger.tsv")))
        self.config(machines_check=self.py("print('up')"))
        code, out, err = self.p.hk("run", "x", "--", *OK_CMD)
        self.assertEqual(code, 0, out + err)

    # -- 10. external refs --------------------------------------------------

    def external(self):
        """Create an outside file and cite it from Read first; return its path."""
        ext = os.path.join(self.tmp, "GUIDE.md")
        with open(ext, "w") as fh:
            fh.write("# Guide\n\n## Rules\nOUTSIDE RULES\n")
        self.p.replace("docs/HANDOFF.md", "| docs/arch.md |", "| docs/arch.md |\n| `%s#Rules` |" % ext)
        self.p.commit_push("cite external")
        return ext

    def test_i10_external_ref_resolves_locally(self):
        """10: a Read first ref to a listed absolute path is read locally; unlisted stays refused."""
        ext = self.external()
        code, out, err = self.p.hk("boot")
        self.assertEqual(code, 2, out)
        self.assertIn("absolute", err)
        self.config(commit=True, external_refs=[ext])
        code, out, err = self.p.hk("boot")
        self.assertEqual(code, 0, err)
        self.assertIn("OUTSIDE RULES", out)
        self.assertOk()

    def test_i10_external_ref_missing_is_a_warning(self):
        """10: on a machine without the file, boot and check WARN instead of failing."""
        ext = self.external()
        self.config(commit=True, external_refs=[ext])
        os.remove(ext)
        code, out, err = self.p.hk("boot")
        self.assertEqual(code, 0, err)
        self.assertIn("WARN external ref", out)
        out = self.assertOk()
        self.assertIn("WARN C7", out)


if __name__ == "__main__":
    unittest.main()
