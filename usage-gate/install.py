#!/usr/bin/env python3
"""Install usage-gate from this repo: script, skill, global hooks, account map. Idempotent; stdlib only.
Runs on macOS, Linux and Windows. The hook command uses absolute paths (this interpreter and the installed
gate.py), so it needs neither python3 on PATH nor ~ expansion."""
import json, os, shutil, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))


def fwd(p):
    return p.replace("\\", "/") if os.name == "nt" else p


def has_keychain():
    return sys.platform == "darwin" and not os.environ.get("USAGE_GATE_NO_KEYCHAIN") and shutil.which("security") is not None


def is_ours(h):
    return "usage-gate/gate.py" in h.get("command", "").replace("\\", "/")


def find_python():
    """An unversioned absolute interpreter: python3 on POSIX; python on Windows, skipping the WindowsApps store stub.
    Falls back to the interpreter running this installer."""
    if os.name == "nt":
        for d in os.environ.get("PATH", "").split(os.pathsep):
            if d and "windowsapps" not in d.lower():
                hit = shutil.which("python", path=d)
                if hit:
                    return os.path.abspath(hit)
    else:
        skip = [os.path.abspath(v) for v in (os.environ.get("VIRTUAL_ENV"), os.environ.get("CONDA_PREFIX")) if v]
        for d in os.environ.get("PATH", "").split(os.pathsep):
            ad = os.path.abspath(d) if d else ""
            if not d or "/shims" in ad.replace("\\", "/") + "/" or any(ad == k or ad.startswith(k + os.sep) for k in skip):
                continue
            hit = shutil.which("python3", path=d)
            if hit:
                return os.path.abspath(hit)
    return sys.executable


def main():
    home = os.path.expanduser("~")
    path = os.path.join(home, ".claude", "settings.json")
    d = {}
    if os.path.exists(path):  # parse first: a malformed file stops everything before anything is written
        try:
            with open(path, encoding="utf-8-sig") as f:
                d = json.load(f)
            if not isinstance(d, dict):
                raise ValueError("top level is not an object")
        except (ValueError, OSError) as e:
            print("usage-gate install: cannot read %s (%s). Fix or remove it, then rerun. Nothing was changed." % (path, e))
            return 1
    gdir = os.path.join(home, ".claude", "usage-gate")
    sdir = os.path.join(home, ".claude", "skills", "usage-gate")
    os.makedirs(gdir, exist_ok=True)
    os.makedirs(sdir, exist_ok=True)
    gate = os.path.join(gdir, "gate.py")
    shutil.copyfile(os.path.join(HERE, "gate.py"), gate)
    shutil.copyfile(os.path.join(HERE, "skill", "SKILL.md"), os.path.join(sdir, "SKILL.md"))

    cmd = {"type": "command", "command": '"%s" -I "%s" hook' % (fwd(find_python()), fwd(gate)), "timeout": 20}
    for ev in ("PreToolUse", "PostToolUse"):
        groups = d.setdefault("hooks", {}).setdefault(ev, [])
        for g in list(groups):  # drop older usage-gate entries; keep every other hook and group
            mine = [h for h in g.get("hooks", []) if is_ours(h)]
            if mine:
                g["hooks"] = [h for h in g["hooks"] if not is_ours(h)]
                if not g["hooks"]:
                    groups.remove(g)
        groups.append({"matcher": "*", "hooks": [cmd]})
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, indent=2)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    print("hooks ok in", path)

    if has_keychain():
        subprocess.run([sys.executable, "-I", gate, "accounts", "--session", "install"])
    else:
        print("usage-gate: no keychain here, skipping the accounts step (the login file ~/.claude/.credentials.json is used).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
