#!/usr/bin/env python3
"""Detect what this computer can do, write MACHINE.md, and derive the PLAN from the facts.

Usage:
  detect_machine.py [--project DIR]        detect, save facts, derive the plan, write MACHINE.md (old versions kept)
  detect_machine.py --check [--project DIR] one line: is the hardware the same as when MACHINE.md was written?
  detect_machine.py --stdout               detect and print MACHINE.md; write nothing (no project needed)
  detect_machine.py --facts-json FILE      use facts from a file instead of detecting (testing, or a PC you describe)
  detect_machine.py --vram-gb N | --ram-gb N | --no-gpu     tell it a fact it cannot detect (recorded as "told by the user")
  detect_machine.py --clear-overrides      forget told facts
  detect_machine.py --no-network           skip the internet reachability test

Read-only: it only runs harmless system queries (nvidia-smi, PowerShell Get-CimInstance, sysctl, /proc files,
lspci, system_profiler) and opens a few TCP connections to test reachability. It needs no administrator rights,
installs nothing, and never fails hard: a missing tool just makes that fact "unknown", never a guess.
It writes only inside the project folder (MACHINE.md and machine/).
"""
import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import rwlib  # noqa: E402

UNKNOWN = "unknown"


def sh(args, timeout=8):
    """Run a command (no shell). Returns (returncode, stdout) or (None, '') if it is missing or fails to start."""
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout, errors="replace")
        return r.returncode, (r.stdout or "")
    except (OSError, subprocess.SubprocessError):
        return None, ""


def powershell(script, timeout=15):
    """Run a PowerShell snippet; returns parsed JSON or None."""
    exe = shutil.which("powershell") or shutil.which("pwsh")
    if not exe:
        return None
    rc, out = sh([exe, "-NoProfile", "-NonInteractive", "-Command", script], timeout)
    if rc != 0 or not out.strip():
        return None
    try:
        return json.loads(out)
    except ValueError:
        return None


def fact(value, how, status="ok"):
    """One fact record."""
    return {"value": value, "how": how, "status": status}


def unknown(how, why="not available"):
    return {"value": UNKNOWN, "how": how, "status": "unknown (%s)" % why}


# ------------------------------------------------------------------ system
def detect_os(f):
    sysname = platform.system()
    if sysname == "Windows":
        ver = platform.version()
        build = 0
        m = re.match(r"10\.0\.(\d+)", ver)
        if m:
            build = int(m.group(1))
        name = "Windows 11" if build >= 22000 else ("Windows 10" if m else "Windows")
        f["os_name"] = fact(name, "platform.version() build number %s" % (build or "?"))
        f["os_version"] = fact(ver, "platform.version()")
    elif sysname == "Darwin":
        f["os_name"] = fact("macOS", "platform.system()")
        f["os_version"] = fact(platform.mac_ver()[0] or UNKNOWN, "platform.mac_ver()")
    elif sysname == "Linux":
        name, ver = "Linux", platform.release()
        try:
            for line in Path("/etc/os-release").read_text(encoding="utf-8", errors="replace").splitlines():
                if line.startswith("PRETTY_NAME="):
                    name = line.split("=", 1)[1].strip().strip('"')
        except OSError:
            pass
        f["os_name"] = fact(name, "/etc/os-release")
        f["os_version"] = fact(ver, "kernel release from platform.release()")
    else:
        f["os_name"] = fact(sysname or UNKNOWN, "platform.system()", "ok" if sysname else "unknown")
        f["os_version"] = fact(platform.release() or UNKNOWN, "platform.release()")
    f["arch"] = fact(platform.machine() or UNKNOWN, "platform.machine()")


def detect_cpu(f):
    threads = os.cpu_count()
    name, cores = None, None
    sysname = platform.system()
    if sysname == "Windows":
        data = powershell("Get-CimInstance Win32_Processor | Select-Object Name,NumberOfCores,NumberOfLogicalProcessors | ConvertTo-Json -Compress")
        if data:
            if isinstance(data, list):
                data = data[0]
            name, cores = (data.get("Name") or "").strip(), data.get("NumberOfCores")
            how = "PowerShell Get-CimInstance Win32_Processor"
        else:
            rc, out = sh(["wmic", "cpu", "get", "name,numberofcores", "/format:list"])
            m1, m2 = re.search(r"Name=(.+)", out), re.search(r"NumberOfCores=(\d+)", out)
            if m1:
                name = m1.group(1).strip()
            if m2:
                cores = int(m2.group(1))
            how = "wmic cpu"
            if not name:
                name = platform.processor() or None
                how = "platform.processor() (less precise)"
    elif sysname == "Darwin":
        rc, out = sh(["sysctl", "-n", "machdep.cpu.brand_string"])
        name = out.strip() or None
        rc, out = sh(["sysctl", "-n", "hw.physicalcpu"])
        cores = int(out.strip()) if out.strip().isdigit() else None
        how = "sysctl"
    else:
        how = "/proc/cpuinfo"
        try:
            txt = Path("/proc/cpuinfo").read_text(encoding="utf-8", errors="replace")
            m = re.search(r"model name\s*:\s*(.+)", txt) or re.search(r"Hardware\s*:\s*(.+)", txt)
            name = m.group(1).strip() if m else None
            pairs = set(re.findall(r"physical id\s*:\s*(\d+)\s*\n(?:.*\n)*?core id\s*:\s*(\d+)", txt))
            cores = len(pairs) or None
        except OSError:
            pass
        if not name:
            rc, out = sh(["lscpu"])
            m = re.search(r"Model name:\s*(.+)", out)
            name = m.group(1).strip() if m else None
            how = "lscpu"
    f["cpu_model"] = fact(name, how) if name else unknown(how)
    f["cpu_cores_physical"] = fact(int(cores), how) if cores else (
        fact(max(1, threads // 2), "estimated as half of the logical threads (physical count not available)", "ok (estimate)") if threads else unknown("os.cpu_count()"))
    f["cpu_threads"] = fact(threads, "os.cpu_count()") if threads else unknown("os.cpu_count()")


def detect_ram(f):
    sysname = platform.system()
    total = None
    how = ""
    if sysname == "Windows":
        try:
            import ctypes

            class MEMSTATUS(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong), ("ullTotalPhys", ctypes.c_ulonglong),
                            ("ullAvailPhys", ctypes.c_ulonglong), ("ullTotalPageFile", ctypes.c_ulonglong),
                            ("ullAvailPageFile", ctypes.c_ulonglong), ("ullTotalVirtual", ctypes.c_ulonglong),
                            ("ullAvailVirtual", ctypes.c_ulonglong), ("sullAvailExtendedVirtual", ctypes.c_ulonglong)]
            st = MEMSTATUS()
            st.dwLength = ctypes.sizeof(MEMSTATUS)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
                total = st.ullTotalPhys
                how = "Windows GlobalMemoryStatusEx"
        except Exception:
            pass
    elif sysname == "Darwin":
        rc, out = sh(["sysctl", "-n", "hw.memsize"])
        if out.strip().isdigit():
            total, how = int(out.strip()), "sysctl hw.memsize"
    else:
        try:
            m = re.search(r"MemTotal:\s+(\d+) kB", Path("/proc/meminfo").read_text(encoding="utf-8", errors="replace"))
            if m:
                total, how = int(m.group(1)) * 1024, "/proc/meminfo MemTotal"
        except OSError:
            pass
    if total:
        f["ram_gb"] = fact(round(total / 1024 ** 3, 1), how)
    else:
        f["ram_gb"] = unknown(how or "no method worked on this system")


def _nvidia_smi_path():
    cands = ["nvidia-smi"]
    if platform.system() == "Windows":
        cands += [r"C:\Windows\System32\nvidia-smi.exe", r"C:\Program Files\NVIDIA Corporation\NVSMI\nvidia-smi.exe"]
    for c in cands:
        p = shutil.which(c) or (c if os.path.exists(c) else None)
        if p:
            return p
    return None


def detect_gpu(f):
    smi = _nvidia_smi_path()
    gpus = []
    if smi:
        rc, out = sh([smi, "--query-gpu=name,memory.total,driver_version,compute_cap", "--format=csv,noheader,nounits"])
        if rc != 0:
            rc, out = sh([smi, "--query-gpu=name,memory.total,driver_version", "--format=csv,noheader,nounits"])
        for line in out.strip().splitlines():
            p = [x.strip() for x in line.split(",")]
            if len(p) >= 3:
                mem = float(p[1]) / 1024.0 if re.match(r"^\d+(\.\d+)?$", p[1]) else None
                gpus.append({"name": p[0], "vram_gb": round(mem, 1) if mem else None, "driver": p[2], "cc": p[3] if len(p) > 3 else None})
    if gpus:
        g = sorted(gpus, key=lambda x: x["vram_gb"] or 0, reverse=True)[0]
        f["gpu_name"] = fact(g["name"], "nvidia-smi" + (" (largest of %d GPUs)" % len(gpus) if len(gpus) > 1 else ""))
        if g["vram_gb"]:
            f["gpu_vram_gb"] = fact(g["vram_gb"], "nvidia-smi memory.total")
        else:
            f["gpu_vram_gb"] = unknown("nvidia-smi memory.total", "the driver reports no dedicated video memory (shared-memory device?)")
        f["gpu_driver"] = fact(g["driver"], "nvidia-smi driver_version")
        f["gpu_compute_cap"] = fact(g["cc"], "nvidia-smi compute_cap") if g["cc"] and g["cc"] != "[N/A]" else unknown("nvidia-smi compute_cap", "older driver does not report it")
        rc, out = sh([smi])
        m = re.search(r"CUDA Version:\s*([\d.]+)", out)
        f["gpu_cuda"] = fact(m.group(1), "nvidia-smi header (highest CUDA the driver supports)") if m else unknown("nvidia-smi header")
        f["gpu_vendor"] = fact("nvidia", "nvidia-smi found")
        return
    # No NVIDIA tool: look for any GPU name
    sysname = platform.system()
    names = []
    how = ""
    if sysname == "Windows":
        data = powershell("Get-CimInstance Win32_VideoController | Select-Object Name | ConvertTo-Json -Compress")
        if data:
            data = data if isinstance(data, list) else [data]
            names = [d.get("Name") for d in data if d.get("Name")]
            how = "PowerShell Get-CimInstance Win32_VideoController"
    elif sysname == "Darwin":
        rc, out = sh(["system_profiler", "SPDisplaysDataType", "-json"], 20)
        try:
            names = [d.get("sppci_model") for d in json.loads(out).get("SPDisplaysDataType", []) if d.get("sppci_model")]
            how = "system_profiler SPDisplaysDataType"
        except ValueError:
            pass
    else:
        rc, out = sh(["lspci"])
        names = [re.sub(r"^\S+\s+", "", l) for l in out.splitlines() if re.search(r"VGA|3D|Display", l)]
        how = "lspci"
    if names:
        f["gpu_name"] = fact("; ".join(names), how)
        low = " ".join(names).lower()
        vendor = "apple" if "apple" in low else "amd" if ("amd" in low or "radeon" in low) else "intel" if "intel" in low else "nvidia" if "nvidia" in low else "other"
        f["gpu_vendor"] = fact(vendor, how)
        f["gpu_vram_gb"] = unknown(how, "dedicated video memory is not reported without the vendor tool")
        if vendor == "nvidia":
            f["gpu_driver"] = unknown("nvidia-smi", "an NVIDIA GPU is listed but nvidia-smi was not found: the NVIDIA driver may not be installed")
    else:
        f["gpu_name"] = unknown(how or "nvidia-smi, PowerShell, system_profiler, lspci", "no GPU found by any method")
        f["gpu_vendor"] = fact("none", "no tool reported a GPU", "unknown (no GPU found)")
        f["gpu_vram_gb"] = unknown("no GPU found")


def detect_disk(f, where):
    try:
        u = shutil.disk_usage(str(where))
        f["free_disk_gb"] = fact(round(u.free / 1024 ** 3, 1), "shutil.disk_usage(%s)" % ("project folder" if where != Path.cwd() else "current folder"))
    except OSError as exc:
        f["free_disk_gb"] = unknown("shutil.disk_usage", str(exc))


def detect_python(f):
    f["python_version"] = fact(platform.python_version(), "sys.version")
    f["python_exe"] = fact(sys.executable, "sys.executable")
    try:
        import venv  # noqa: F401
        import ensurepip  # noqa: F401
        f["python_venv_ok"] = fact(True, "import venv, ensurepip")
    except ImportError:
        f["python_venv_ok"] = fact(False, "import venv, ensurepip failed (a private environment cannot be made; a folder install is used instead)")
    try:
        import setup_env
        py = setup_env.find_python()
        f["pymupdf_ok"] = fact(bool(py), "setup_env.find_python()" + (": " + str(py) if py else ": run setup_env.py install"))
    except Exception as exc:
        f["pymupdf_ok"] = unknown("setup_env", str(exc))


def detect_runner(f):
    sysname = platform.system()
    cands = [shutil.which("ollama")]
    if sysname == "Windows":
        la = os.environ.get("LOCALAPPDATA")
        if la:
            cands.append(str(Path(la) / "Programs" / "Ollama" / "ollama.exe"))
    elif sysname == "Darwin":
        cands.append("/Applications/Ollama.app/Contents/Resources/ollama")
    else:
        cands += ["/usr/local/bin/ollama", str(Path.home() / "ollama" / "bin" / "ollama")]
    exe = next((c for c in cands if c and os.path.exists(c)), None)
    import ollama_api
    ver = ollama_api.version(3)
    f["runner_name"] = fact("ollama" if (exe or ver) else "none", "looked for the ollama program and for the local server")
    f["runner_path"] = fact(exe, "PATH and usual install folders") if exe else fact("none", "not found on PATH or in the usual folders", "ok (absent)")
    if not ver and exe:
        rc, out = sh([exe, "--version"])
        m = re.search(r"(\d+\.\d+\.\d+)", out)
        ver = m.group(1) if m else None
        f["runner_version"] = fact(ver, "ollama --version") if ver else unknown("ollama --version")
    else:
        f["runner_version"] = fact(ver, "GET /api/version on the local server") if ver else fact("none", "no runner reachable", "ok (absent)")
    f["runner_running"] = fact(bool(ollama_api.version(3)), "GET %s/api/version" % ollama_api.base_url())
    models = []
    if ollama_api.version(3):
        try:
            models = ollama_api.tags()
            f["runner_models"] = fact([{"name": m["name"], "size_gb": round((m["size_bytes"] or 0) / 1024 ** 3, 2)} for m in models], "GET /api/tags")
        except ollama_api.OllamaError as exc:
            f["runner_models"] = unknown("GET /api/tags", str(exc))
    elif exe:
        rc, out = sh([exe, "list"], 10)
        if rc == 0:
            names = [l.split()[0] for l in out.splitlines()[1:] if l.strip()]
            f["runner_models"] = fact([{"name": n, "size_gb": None} for n in names], "ollama list")
        else:
            f["runner_models"] = unknown("ollama list", "the server is not running; start Ollama to list models")
    else:
        f["runner_models"] = fact([], "no runner installed", "ok (absent)")
    others = [n for n in ("lms", "llama-server", "koboldcpp") if shutil.which(n)]
    f["other_runners"] = fact(others, "PATH lookup for lms, llama-server, koboldcpp")


def detect_network(f, enabled):
    for key, host in (("net_pypi", "pypi.org"), ("net_ollama", "ollama.com"), ("net_anthropic", "api.anthropic.com")):
        if not enabled:
            f[key] = unknown("skipped (--no-network)", "not tested")
            continue
        try:
            with socket.create_connection((host, 443), timeout=4):
                f[key] = fact(True, "TCP connect to %s:443 (reachability only, nothing sent)" % host)
        except OSError as exc:
            f[key] = fact(False, "TCP connect to %s:443 failed (%s)" % (host, type(exc).__name__), "ok (not reachable)")


# ---------------------------------------------------------------- overrides
def apply_overrides(f, overrides):
    """Told facts replace detected ones, and are labelled as told."""
    for k, v in (overrides or {}).items():
        f[k] = fact(v, "told by the user (not detected)", "told")


def fingerprint(f):
    """Short hash of the facts that, if changed, mean the plan must be re-derived."""
    keys = ["os_name", "cpu_model", "ram_gb", "gpu_name", "gpu_vram_gb", "gpu_driver", "runner_version"]
    parts = []
    for k in keys:
        v = f.get(k, {}).get("value")
        if k == "ram_gb" and isinstance(v, (int, float)):
            v = int(round(v))
        parts.append("%s=%s" % (k, v))
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]


def detect_all(where, network=True, light=False):
    f = {}
    steps = [detect_os, detect_cpu, detect_ram, detect_gpu]
    if not light:
        steps += [lambda x: detect_disk(x, where), detect_python, detect_runner, lambda x: detect_network(x, network)]
    else:
        steps += [detect_runner]
    for step in steps:
        try:
            step(f)
        except Exception as exc:  # never fail hard
            f["error_" + getattr(step, "__name__", "step")] = unknown(getattr(step, "__name__", "step"), "%s: %s" % (type(exc).__name__, exc))
    return f


def main():
    rwlib.setup_console()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--stdout", action="store_true")
    ap.add_argument("--facts-json")
    ap.add_argument("--vram-gb", type=float)
    ap.add_argument("--ram-gb", type=float)
    ap.add_argument("--no-gpu", action="store_true")
    ap.add_argument("--clear-overrides", action="store_true")
    ap.add_argument("--no-network", action="store_true")
    a = ap.parse_args()
    import make_plan

    project = None
    if not a.stdout or a.project:
        try:
            project = rwlib.find_project(explicit=a.project)
        except SystemExit:
            if not a.stdout and not a.facts_json:
                raise
    where = project or Path.cwd()
    ov_path = (project / "machine" / "overrides.json") if project else None
    overrides = (rwlib.read_json(ov_path, {}) if ov_path else {}) or {}
    if a.clear_overrides:
        overrides = {}
    if a.vram_gb is not None:
        overrides["gpu_vram_gb"] = a.vram_gb
    if a.ram_gb is not None:
        overrides["ram_gb"] = a.ram_gb
    if a.no_gpu:
        overrides["gpu_vram_gb"] = 0
        overrides["gpu_vendor"] = "none"

    if a.check:
        if not project:
            rwlib.die("--check needs a project")
        old = rwlib.read_json(project / "machine" / "facts.json")
        if not old:
            print("machine: no MACHINE.md yet (run detect_machine.py)")
            return 10
        f = detect_all(where, light=True)
        apply_overrides(f, overrides)
        if fingerprint(f) == old["fingerprint"]:
            roles = (rwlib.read_json(project / "roles.json", {}) or {}).get("roles", {})
            installed = {m["name"] for m in (f.get("runner_models", {}).get("value") or []) if isinstance(m, dict)} if isinstance(f.get("runner_models", {}).get("value"), list) else None
            missing = []
            if installed is not None:
                for r, ent in roles.items():
                    for tag in (ent.get("primary"), ent.get("secondary")):
                        if ent.get("assigned") == "local" and tag and tag not in installed and (tag + ":latest") not in installed:
                            missing.append("%s:%s" % (r, tag))
            print("machine: unchanged since %s%s" % (old["detected_on"][:10], ("; model(s) missing from the runner: " + ", ".join(missing)) if missing else ""))
            return 0 if not missing else 11
        changed = [k for k in ("os_name", "cpu_model", "ram_gb", "gpu_name", "gpu_vram_gb", "gpu_driver", "runner_version")
                   if f.get(k, {}).get("value") != old["facts"].get(k, {}).get("value")]
        print("machine: CHANGED (%s). Re-run detect_machine.py to re-derive the plan." % ", ".join(changed))
        return 10

    if a.facts_json:
        doc = rwlib.read_json(a.facts_json)
        f = doc["facts"] if "facts" in doc else doc
    else:
        f = detect_all(where, network=not a.no_network)
    apply_overrides(f, overrides)
    doc = {"detected_on": rwlib.now_iso(), "facts": f, "fingerprint": fingerprint(f)}
    cand = rwlib.read_json(rwlib.SKILL_DIR / "candidates.json")
    roles = (rwlib.read_json(project / "roles.json", {}) if project else {}) or {}
    plan = make_plan.derive_plan(f, cand, roles)
    md = make_plan.render_markdown(doc, plan, roles, user_notes=make_plan.existing_user_notes(project))
    if a.stdout or not project:
        print(md)
        return 0
    (project / "machine" / "history").mkdir(parents=True, exist_ok=True)
    stamp = rwlib.now_iso()[:19].replace(":", "").replace("-", "").replace("T", "-")
    for name, ext in (("MACHINE.md", ".md"), ("machine/facts.json", ".json"), ("machine/plan.json", ".json")):
        p = project / name
        if p.exists():
            shutil.copyfile(p, project / "machine" / "history" / ("%s_%s%s" % (Path(name).stem, stamp, ext)))
    rwlib.write_json(project / "machine" / "facts.json", doc)
    rwlib.write_json(project / "machine" / "plan.json", plan)
    rwlib.write_json(project / "machine" / "overrides.json", overrides)
    rwlib.write_text_atomic(project / "MACHINE.md", md)
    stale = make_plan.mark_stale_roles(project, plan)
    print("wrote MACHINE.md (facts dated %s, fingerprint %s); earlier versions are in machine/history/" % (doc["detected_on"][:10], doc["fingerprint"]))
    print("tier: %s. Summary: %s" % (plan["tier"], plan["summary"]))
    if stale:
        print("roles marked stale (retest needed): " + ", ".join(stale))
    for w in plan["warnings"]:
        print("WARNING: " + w)
    return 0


if __name__ == "__main__":
    sys.exit(main())
