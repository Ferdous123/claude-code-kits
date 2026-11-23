#!/usr/bin/env python3
"""Install the one outside dependency (PyMuPDF, for reading PDF text with positions) into a private folder.

Usage:
  setup_env.py install      create a private virtual environment in the skill's cache folder and install PyMuPDF there
  setup_env.py status       say whether PyMuPDF can be used and where
  setup_env.py path         print the Python interpreter that has PyMuPDF (or nothing)
  setup_env.py remove --yes delete the private environment (only the skill's own cache folder)

Why PyMuPDF: it ships ready-made wheels for Windows, macOS and Linux (no C compiler, no admin rights),
gives text with page positions, and handles page rotation. Licence: AGPL-3.0 (fine for private research use;
read the licence before redistributing anything built on it).

Nothing is written outside the skill's cache folder (see `status` for its location). pip is run with its own
cache switched off so that nothing lands in the user's pip cache either.
"""
import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import rwlib  # noqa: E402

REQUIREMENT = "pymupdf>=1.24,<2"


def venv_dir():
    return rwlib.cache_dir() / "venv"


def site_dir():
    return rwlib.cache_dir() / "site"


def venv_python():
    """Interpreter inside the private environment, or None."""
    v = venv_dir()
    p = v / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    return p if p.exists() else None


def can_import(python):
    """True if `python` can import PyMuPDF."""
    code = "try:\n import pymupdf\nexcept ImportError:\n import fitz\n"
    env = dict(os.environ)
    env["PYTHONPATH"] = str(site_dir()) + os.pathsep + env.get("PYTHONPATH", "")
    try:
        r = subprocess.run([str(python), "-c", code], capture_output=True, text=True, timeout=60, env=env)
        return r.returncode == 0
    except Exception:
        return False


def find_python():
    """Best interpreter that already has PyMuPDF: current one, private venv, or private site folder."""
    if can_import(sys.executable):
        return Path(sys.executable)
    vp = venv_python()
    if vp and can_import(vp):
        return vp
    return None


def run(cmd):
    print("+ " + " ".join(str(c) for c in cmd))
    return subprocess.run([str(c) for c in cmd]).returncode


def install():
    cache = rwlib.cache_dir()
    cache.mkdir(parents=True, exist_ok=True)
    if sys.version_info < (3, 8):
        print("Python 3.8 or newer is needed. This is %s." % sys.version.split()[0])
        return 1
    if find_python():
        print("PyMuPDF is already usable. Nothing to do.")
        return 0
    if not venv_python():
        code = run([sys.executable, "-m", "venv", venv_dir()])
        if code != 0:
            print("Could not create a virtual environment (on some Linux systems the 'venv' package is missing and needs an administrator).")
            print("Falling back to a plain folder install inside the cache.")
            site_dir().mkdir(parents=True, exist_ok=True)
            code = run([sys.executable, "-m", "pip", "install", "--no-cache-dir", "--disable-pip-version-check", "--target", site_dir(), REQUIREMENT])
            if code != 0:
                print("INSTALL FAILED. Report this to the user with the lines above. They can still use text (.txt/.md) papers without PyMuPDF.")
                return 1
            print("Installed into %s (no virtual environment)." % site_dir())
            return 0
    py = venv_python()
    code = run([py, "-m", "pip", "install", "--no-cache-dir", "--disable-pip-version-check", REQUIREMENT])
    if code != 0:
        print("INSTALL FAILED (no internet, a proxy, or no wheel for this Python version?). Report the lines above to the user.")
        return 1
    if not can_import(py):
        print("Installed, but the import check failed. Report this to the user.")
        return 1
    print("PyMuPDF installed into the private environment: %s" % venv_dir())
    return 0


def main():
    rwlib.setup_console()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("install")
    sub.add_parser("status")
    sub.add_parser("path")
    r = sub.add_parser("remove")
    r.add_argument("--yes", action="store_true", help="confirm deletion of the private environment")
    a = ap.parse_args()
    if a.cmd == "install":
        return install()
    if a.cmd == "status":
        py = find_python()
        print("cache folder: %s" % rwlib.cache_dir())
        print("private venv: %s" % (venv_python() or "absent"))
        print("PyMuPDF usable: %s" % ("yes, via %s" % py if py else "no (run: setup_env.py install)"))
        return 0
    if a.cmd == "path":
        py = find_python()
        if py:
            print(py)
            return 0
        return 1
    if not a.yes:
        print("Refusing without --yes. This removes %s and %s." % (venv_dir(), site_dir()))
        return 2
    for d in (venv_dir(), site_dir()):
        if d.exists() and rwlib.inside(rwlib.cache_dir(), d):
            shutil.rmtree(d)
            print("removed %s" % d)
    return 0


if __name__ == "__main__":
    sys.exit(main())
