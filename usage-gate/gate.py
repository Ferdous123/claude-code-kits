#!/usr/bin/env python3
"""usage-gate: pause armed Claude Code sessions cleanly near plan limits, resume after reset.

Zero-token by design: usage comes from Anthropic's usage endpoint via the account's
Claude Code login (macOS keychain, or ~/.claude/.credentials.json elsewhere); enforcement is a PreToolUse/PostToolUse hook.

Commands (the hook appends --session <id> automatically when Claude runs them):
  arm [--soft 80] [--hard 95] [--ask 10]   arm this session
  disarm | status
  continue | resume                        user said go on (gates again only at a higher level)
  pause                                    user said pause now (pack up)
  timer                                    background: wait out the question timeout
  wait                                     background: hold the pause until limits are below soft
  accounts                                 map keychain logins to accounts
  listen                                   background: wait for a Telegram command for this session, print what to do
  telegram setup|test|poll                 connect / test Telegram alerts (token on stdin) / read commands
  hook                                     hook entry point (reads JSON on stdin)
"""
import contextlib, copy, getpass, hashlib, json, os, re, shutil, subprocess, sys, time, urllib.error, urllib.request, uuid
try:
    import fcntl
except ImportError:  # Windows: no fcntl; locked() falls back to an O_EXCL lock file
    fcntl = None

HOME = os.path.expanduser("~")
GATE = os.path.join(HOME, ".claude", "usage-gate")
SESS = os.path.join(GATE, "sessions")
NOTES = os.path.join(GATE, "notes")
CACHE = os.path.join(GATE, "cache")
LOG = os.path.join(GATE, "gate.log")
CONFIG = os.path.join(GATE, "config.json")


def _is_installed_copy():
    """The copy the hook runs (~/.claude/usage-gate/gate.py) never honours test overrides, whatever the env says."""
    inst = os.path.expanduser("~/.claude/usage-gate/gate.py")
    return os.path.realpath(__file__) == os.path.realpath(inst)


def testenv(name, default=None):
    """Test-only overrides (fake usage, fake API URLs) count only in a process started with USAGE_GATE_TEST=1.
    The hook denies any Bash command naming USAGE_GATE_*, so the model cannot set them for a gate command."""
    if os.environ.get("USAGE_GATE_TEST") != "1" or _is_installed_copy():
        return default
    return os.environ.get(name, default)


def platform():
    return testenv("USAGE_GATE_PLATFORM") or sys.platform


def launcher(plat=None):
    """Interpreter name for commands shown to the model: `python` on Windows, `python3` elsewhere."""
    return "python" if (plat or platform()).startswith("win") else "python3"


def has_keychain():
    return platform() == "darwin" and not testenv("USAGE_GATE_NO_KEYCHAIN") and shutil.which("security") is not None


SELF = launcher() + " -I ~/.claude/usage-gate/gate.py"
DEFAULT_SVC = "Claude Code-credentials"
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
API = testenv("USAGE_GATE_API") or "https://api.anthropic.com"
LOCK_TIMEOUT = float(os.environ.get("USAGE_GATE_LOCK_TIMEOUT") or 10)  # seconds a lock is waited for; also the longest a fallback lock file counts as live
LOCK_STALE = None   # tests: stale age override for fallback lock files (default: the wait timeout)
POLL_LOCK_STALE = 120  # the poll lock is held across network calls
STALE_OK = 15 * 60   # seconds an old reading still gates after a transient read error
BACKOFF_MIN, BACKOFF_MAX = 60, 15 * 60   # 429 back-off without Retry-After: 60 s doubling to 15 min
FETCH_SLOT = 30   # seconds a process's claim to fetch blocks the others (longer than the 15 s HTTP timeout)
HOOK_TTL = 120   # seconds a usage reading is reused by the hook
WAIT_POLL = 300  # seconds between readings while paused
NEW_WORK = {"Agent", "Task", "Workflow", "ScheduleWakeup", "CronCreate", "RemoteTrigger",
            "mcp__ccd_session__start_session", "mcp__ccd_session__hand_off_to_session"}
CLAUDE_CMD = re.compile(r"(^|[;&|]\s*)claude\b")  # a Bash command that starts another Claude session is new work


def is_new_work(tool, ti):
    return tool in NEW_WORK or (tool == "Bash" and bool(CLAUDE_CMD.search((ti or {}).get("command", "") or "")))


HARD_OK = {"Read", "TaskStop", "TaskOutput", "SendMessage", "PushNotification", "SubagentHandback"}
# A command is auto-allowed only when it is one gate call and nothing else: no chaining, redirects, substitution,
# quotes, newlines or env-var prefix (anything else goes through normal gating).
GATE_CMD = re.compile(r"^[ \t]*(?:python3?)[ \t]+(?:-I[ \t]+)?~/\.claude/usage-gate/gate\.py[ \t]+"
                      r"(?:arm|disarm|status|continue|resume|pause|timer|wait|listen|accounts|telegram)"
                      r"(?:[ \t]+[\w.,:=@/-]+)*[ \t]*$")
WINDOWS = (("five_hour", "5-hour"), ("seven_day", "weekly"))


# ---------- small helpers ----------
def now():
    return time.time()


def log(msg):
    try:
        os.makedirs(GATE, exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")
    except Exception:
        pass


def rjson(path, default=None):
    try:
        with open(path, encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception:
        return default


def replace_retry(tmp, path):
    """os.replace with a few retries (Windows raises PermissionError while the target is open); removes tmp on failure."""
    try:
        for i in range(6):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if i == 5:
                    raise
                time.sleep(0.05)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def wjson(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".%d.tmp" % os.getpid()
    try:
        with open_private(tmp) as f:
            json.dump(data, f, indent=1)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    replace_retry(tmp, path)


def open_private(path):
    """Open `path` for writing (utf-8), mode 600 on POSIX (os.fchmod is absent on Windows, where it is skipped)."""
    f = os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8")
    if hasattr(os, "fchmod"):
        os.fchmod(f.fileno(), 0o600)
    return f


def write_private_json(path, data):
    wjson(path, data)


def _lock_age(lk):
    try:
        return now() - os.path.getmtime(lk)
    except OSError:
        return 0


def _break_stale(lk, stale):
    """Remove a stale lock file. Rename first (atomic: one breaker wins), then check what we took; if it turned out
    fresh (the owner released and another process took the lock meanwhile) put it back. Returns False when that
    restore failed: a live lock was lost, so the caller must not treat the lock as acquired."""
    if _lock_age(lk) <= stale:
        return True
    gone = "%s.%d.%s.stale" % (lk, os.getpid(), uuid.uuid4().hex[:6])
    try:
        os.rename(lk, gone)
    except OSError:
        return True
    ok = True
    if _lock_age(gone) <= stale:
        try:
            os.link(gone, lk)
        except OSError:
            ok = False
            log("lock lost: could not restore a live lock %s" % os.path.basename(lk))
    try:
        os.remove(gone)
    except OSError:
        pass
    return ok


@contextlib.contextmanager
def locked(path, wait=True, timeout=None, stale=None):
    """Exclusive lock on `path` (flock; O_EXCL lock file where fcntl is missing). Yields True when held.
    With wait=False it gives up at once; with wait=True it gives up after `timeout` s and yields False.
    A fallback lock file older than `stale` s (default: the timeout) is taken over; it holds its owner's nonce
    and is released only by that owner."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    timeout = LOCK_TIMEOUT if timeout is None else timeout
    stale = stale if stale is not None else (LOCK_STALE if LOCK_STALE is not None else timeout)
    end, got = now() + timeout, False
    if fcntl:
        f = open(path, "a+")
        while True:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                got = True
                break
            except OSError:
                if not wait or now() >= end:
                    break
                time.sleep(0.02)
        try:
            yield got
        finally:
            if got:
                fcntl.flock(f, fcntl.LOCK_UN)
            f.close()
        return
    lk, nonce = path + ".lk", "%d:%s" % (os.getpid(), uuid.uuid4().hex)
    while True:
        if not _break_stale(lk, stale):
            break
        try:
            fd = os.open(lk, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                os.write(fd, nonce.encode())
            finally:
                os.close(fd)
            got = True
            break
        except FileExistsError:
            if not wait or now() >= end:
                break
            time.sleep(0.02)
        except OSError:
            break
    try:
        yield got
    finally:
        if got:
            try:
                with open(lk, encoding="utf-8") as f:
                    mine = f.read() == nonce
                if mine:
                    os.remove(lk)
            except OSError:
                pass


TG_CONFIG = os.path.join(GATE, "telegram.json")
TG_API = "https://api.telegram.org"
TG_MSGS = os.path.join(GATE, "telegram-msgs.json")     # message id -> session id (this machine; send + poll children)
TG_LAST = os.path.join(GATE, "telegram-last.json")     # last handled update id (poll only, under the poll lock)
TG_POLLED = os.path.join(GATE, "telegram-polled")      # timestamp of the last poll (rate limit, back-off)
TG_CMD_MAX_AGE = 600   # seconds; older commands are ignored
TG_MAX_AGE = 300  # seconds; older readings are not sent to Telegram


def tg_conf():
    c = rjson(TG_CONFIG)
    return c if c and c.get("token") and c.get("chat_id") else None


def tg_call(token, method, payload=None):
    """Telegram Bot API call. The token lives only in the URL; errors are scrubbed before they reach a log."""
    base = testenv("USAGE_GATE_TELEGRAM_API", TG_API).rstrip("/")
    data = json.dumps(payload or {}).encode()
    req = urllib.request.Request("%s/bot%s/%s" % (base, token, method), data=data,
                                 headers={"Content-Type": "application/json", "User-Agent": "usage-gate"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            res = json.loads(r.read())
    except Exception as e:
        raise RuntimeError(("%s: %s" % (type(e).__name__, e)).replace(token, "<token>"))
    if not res.get("ok"):
        raise RuntimeError("telegram %s: %s" % (method, str(res.get("description", "not ok"))[:100]))
    return res.get("result")


def machine_name(c):
    host = os.uname().nodename if hasattr(os, "uname") else os.environ.get("COMPUTERNAME", "?")
    return c.get("machine") or host.split(".")[0]


BTN_PREFIX = 12   # callback_data carries the action and this many characters of the session id (64-byte limit)


def keyboard(phase, sid):
    """Inline buttons for an alert about session `sid` in `phase`: one row, each button addressing that session."""
    if not sid:
        return None
    if phase == "asked":
        acts = (("Continue", "continue"), ("Pause", "pause"))
    elif phase == "hard" or phase.startswith("paused"):
        acts = (("Resume", "resume"), ("Status", "status"))
    else:
        acts = (("Status", "status"),)
    return [{"text": label, "callback_data": "%s:%s" % (act, sid[:BTN_PREFIX])} for label, act in acts]


def tg_send(c, text, email="", sid=""):
    parts = [machine_name(c)] + ([email] if email else [])
    if sid:  # name the session so two on one machine and account can be told apart
        parts.append(("%s #%s" % ((load_sess(sid) or {}).get("project") or "", sid[:4])).strip())
    prefix = "[%s]" % " \u00b7 ".join(parts)
    body = {"chat_id": c["chat_id"], "text": prefix + " " + text}
    kb = keyboard((load_sess(sid) or {}).get("phase", "run") if sid else "", sid)
    if kb:
        body["reply_markup"] = {"inline_keyboard": [kb]}
    res = tg_call(c["token"], "sendMessage", body)
    if sid and isinstance(res, dict) and res.get("message_id"):
        try:  # remember which session this message was about, so a reply to it addresses that session
            with locked(TG_MSGS + ".lock"):
                msgs = rjson(TG_MSGS, {}) or {}
                msgs[str(res["message_id"])] = sid
                for k in sorted(msgs, key=int)[:-200]:
                    del msgs[k]
                wjson(TG_MSGS, msgs)
        except Exception as e:
            log("telegram state write failed: %s" % type(e).__name__)
    return res


def notify(text, s=None, u=None):
    """macOS notification (macOS only) plus Telegram when configured. Never raises, never blocks long."""
    if testenv("USAGE_GATE_QUIET"):
        log("notify(quiet): " + text)
        return
    if sys.platform == "darwin" and not os.environ.get("USAGE_GATE_NO_OSASCRIPT"):
        t = text.replace("\\", "").replace('"', "'")
        try:
            subprocess.run(["osascript", "-e", 'display notification "%s" with title "Usage gate" sound name "Glass"' % t],
                           capture_output=True, timeout=5)
        except Exception as e:
            log("osascript failed: %s" % type(e).__name__)
    log("notify: " + text)
    try:
        c = tg_conf()
        if not c:
            return
        msg = text
        if u and "error" not in u and u.get("ts"):
            age = now() - u["ts"]
            if age > TG_MAX_AGE:
                log("telegram skipped: usage reading is %d s old (max %d)" % (age, TG_MAX_AGE))
                return
            msg += " (reading %d s old)" % age
        # Detached child does the HTTP so a slow network can never delay the hook's decision.
        p = subprocess.Popen([sys.executable, "-I", os.path.abspath(__file__), "telegram", "send",
                              (s or {}).get("email", ""), (s or {}).get("session", "")], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
        p.stdin.write(msg.encode())
        p.stdin.close()
    except Exception as e:
        log("telegram send failed: %s" % type(e).__name__)


MENU = (("status", "Usage and phase of each armed session"), ("stop", "Hard stop now"),
        ("pause", "Pack up now"), ("continue", "Go on past the soft limit"), ("resume", "Resume a paused session"),
        ("off", "Disarm the gate"), ("help", "List the commands"))


def tg_set_menu(token):
    """The list Telegram shows when the owner types "/"."""
    tg_call(token, "setMyCommands", {"commands": [{"command": k, "description": d} for k, d in MENU]})


def cmd_telegram(argv):
    argv = list(argv)
    if "--session" in argv:  # injected by the hook; not needed here
        k = argv.index("--session")
        del argv[k:k + 2]
    sub = argv[0] if argv else ""
    if sub == "send":  # internal: detached child of notify(); text on stdin
        try:
            c = tg_conf()
            if c:
                tg_send(c, sys.stdin.buffer.read().decode("utf-8", "replace"), argv[1] if len(argv) > 1 else "", argv[2] if len(argv) > 2 else "")
        except Exception as e:
            log("telegram send failed: %s" % e)
    elif sub == "poll":
        tg_poll("--claimed" in argv)
    elif sub == "test":
        c = tg_conf()
        if not c:
            raise SystemExit("usage-gate: Telegram is not set up. Run: gate.py telegram setup")
        try:
            tg_send(c, "Test message from usage-gate.")
        except Exception as e:
            log("telegram test failed: %s" % e)
            raise SystemExit("usage-gate: Telegram test failed: %s" % e)
        print("Test message sent.")
    elif sub == "menu":
        c = tg_conf()
        if not c:
            raise SystemExit("usage-gate: Telegram is not set up. Run: gate.py telegram setup")
        try:
            tg_set_menu(c["token"])
        except Exception as e:
            raise SystemExit("usage-gate: setting the command menu failed: %s" % e)
        print("Command menu set.")
    elif sub == "setup":
        token = (getpass.getpass("Bot token: ") if sys.stdin.isatty() else sys.stdin.readline()).strip()
        if not token:
            raise SystemExit("usage-gate: pass the bot token on stdin (not as an argument).")
        try:
            ups = tg_call(token, "getUpdates", {"timeout": 0}) or []
        except Exception as e:
            raise SystemExit("usage-gate: Telegram getUpdates failed: %s" % e)
        chats = {}
        for u in ups:
            ch = (u.get("message") or {}).get("chat") or {}
            if ch.get("type") == "private":
                chats[ch["id"]] = ch.get("first_name", "")
        want = int(argv[argv.index("--chat") + 1]) if "--chat" in argv else None
        if want is not None:
            if want not in chats:
                raise SystemExit("usage-gate: chat %s not among: %s" % (want, dict(chats)))
            chat = want
        elif not chats:
            raise SystemExit("usage-gate: no private chat found. Send /start to the bot, then rerun setup.")
        elif len(chats) > 1:
            raise SystemExit("usage-gate: several chats wrote to the bot; rerun with --chat <id>: "
                             + ", ".join("%s (%s)" % (k, v) for k, v in chats.items()))
        else:
            chat = next(iter(chats))
        c = rjson(TG_CONFIG, {}) or {}
        c.update(token=token, chat_id=chat)
        os.makedirs(GATE, exist_ok=True)
        write_private_json(TG_CONFIG, c)
        try:
            tg_send(c, "usage-gate is connected.")
        except Exception as e:
            raise SystemExit("usage-gate: saved, but the test message failed: %s" % e)
        try:
            tg_set_menu(token)
        except Exception as e:
            log("telegram menu failed: %s" % e)
            print("Note: the command menu was not set (%s); run: gate.py telegram menu" % e)
        print("Telegram set up (chat %s). Test message sent." % chat)
    else:
        print("usage: gate.py telegram setup [--chat <id>] (token on stdin) | test | menu | poll")

# ---------- Telegram commands (D9, D11) ----------
CMDS = ("status", "stop", "pause", "continue", "resume", "off", "help")
HELP = ("Commands: /status, /stop (hard stop now), /pause (pack up now), /continue, /resume, /off (disarm), /help. "
        "Reply to a gate message to pick that session, add a machine name (/stop box) to pick a machine, "
        "or send it bare for every armed session on every machine.")


def poll_interval():
    return float(os.environ.get("USAGE_GATE_POLL_INTERVAL", 15))


def read_polled():
    try:
        with open(TG_POLLED, encoding="utf-8") as f:
            return float(f.read())
    except Exception:
        return 0.0


def poll_due():
    return now() - read_polled() >= poll_interval()


def local_sessions():
    out = []
    try:
        names = sorted(os.listdir(SESS))
    except OSError:
        return out
    for n in names:
        if not n.endswith(".json"):
            continue
        s = rjson(os.path.join(SESS, n))
        if s and s.get("armed") and s.get("session"):
            out.append(load_sess(s["session"]) or s)
    return out


def bump(s, name):
    s["cmd"] = {"seq": (s.get("cmd") or {}).get("seq", 0) + 1, "name": name, "ts": now()}


def do_continue(s):
    u = usage(s["service"], 60)
    # Unreadable usage (e.g. 429): the owner said go on, so hold until the hard limit; never clear the override.
    lv = "soft" if "error" in u else level(s, u)
    s.update(phase="run", override=None if lv == "ok" else lv, told=None, manual=None, post_tell=None)
    must_save(s)
    return "Go on. " + ("Next gate at %g%%." % s["hard"] if lv == "soft" else
                        "Gating is off until limits drop below %g%%." % s["soft"] if lv == "hard" else "")


def apply_command(s, name):
    """Apply one phone command to one armed local session; return the one-line reply."""
    ph = s.get("phase", "run")
    if name == "status":
        u = usage(s["service"], HOOK_TTL)
        man = " (owner %s)" % s["manual"] if s.get("manual") else ""
        return "%s%s; %s" % (ph, man, u.get("error") or "%s -> %s" % (figures(u), level(s, u)))
    if name in ("continue", "resume"):
        txt = do_continue(s)
    elif name == "stop":
        s.update(manual="stop", override=None, told=None)
        if not ph.startswith("paused"):
            s["phase"] = "hard"
        must_save(s)
        txt = "Hard stop: tools are blocked now. Say /resume to go on."
    elif name == "pause":
        if ph.startswith("paused"):
            return "Already paused."
        u = usage(s["service"], HOOK_TTL)
        lv = "ok" if "error" in u else level(s, u)
        s.update(phase="packing", told=None, override=None, manual="pause" if lv == "ok" else None)
        must_save(s)
        txt = "Packing up now. Say /resume to go on."
    elif name == "off":
        s.update(armed=False, manual=None)
        must_save(s)
        clear_blocked(s["session"])
        txt = "Disarmed."
    else:
        return ""
    s = load_sess(s["session"]) or s
    bump(s, name)
    must_save(s)
    return txt


TG_OK = os.path.join(GATE, "telegram-ok")   # when this machine last read Telegram successfully
TG_DONE = os.path.join(GATE, "telegram-done.json")   # update id -> targets already answered (retry after a busy save)


def done_mark(uid, key=None):
    """Targets of update `uid` already applied; with `key`, record it. Runs under the poll lock."""
    st = rjson(TG_DONE, {}) or {}
    cur = st.get(str(uid), [])
    if key is None:
        return cur
    cur.append(key)
    st[str(uid)] = cur
    for k in sorted(st, key=int)[:-50]:
        del st[k]
    wjson(TG_DONE, st)
    return cur


def handle_message(c, m, uid=None):
    if str((m.get("chat") or {}).get("id")) != str(c["chat_id"]):
        return
    parts = (m.get("text") or "").split()
    if not parts or not parts[0].startswith("/"):
        return
    name = parts[0][1:].split("@")[0].lower()
    if name not in CMDS:
        return
    arg = parts[1].lower() if len(parts) > 1 else ""
    reply_to = (m.get("reply_to_message") or {}).get("message_id")
    armed = local_sessions()
    if not reply_to:  # a session armed after the message was sent is not its audience
        armed = [s for s in armed if s.get("armed_at", 0) - 1 <= m.get("date", now())]
    if reply_to:
        sid = (rjson(TG_MSGS, {}) or {}).get(str(reply_to))
        if not sid:
            return  # a reply to another machine's message (or one we no longer remember)
        target = [s for s in armed if s["session"] == sid]
    elif arg:
        if arg != machine_name(c).lower():
            return
        target = armed
    else:
        target = armed
    if name == "help":
        if target or arg or reply_to:  # a bare /help is answered only by machines that have armed sessions
            if uid is None or "_" not in done_mark(uid):
                tg_send(c, HELP)
                uid is None or done_mark(uid, "_")
        return
    if not target and name == "status" and arg:
        if uid is None or "_" not in done_mark(uid):
            tg_send(c, "no armed sessions")
            uid is None or done_mark(uid, "_")
    for s in target:
        if uid is not None and s["session"] in done_mark(uid):
            continue
        try:
            if os.environ.get("USAGE_GATE_TEST_APPLY_FAIL") == s["session"]:  # tests only
                raise RuntimeError("test failure")
            txt = apply_command(s, name)
            log("telegram command /%s -> %s" % (name, s["session"]))
            tg_send(c, txt, s.get("email", ""), s["session"])
        except SaveBusy:
            raise
        except Exception as e:
            log("telegram command failed for %s: %s: %s" % (s["session"], type(e).__name__, str(e).replace(c["token"], "<token>")))
        if uid is not None:
            done_mark(uid, s["session"])


def answer_callback(c, cq, text):
    try:
        tg_call(c["token"], "answerCallbackQuery", {"callback_query_id": cq.get("id"), "text": text[:150]})
    except RuntimeError as e:
        log("telegram answerCallbackQuery failed: %s" % e)


def handle_callback(c, cq, uid=None, fresh=True):
    """A button press from the configured chat: apply its action to exactly the session its data names, and
    always answer the press (so the button stops spinning), saying why when nothing was applied. `fresh`: this
    machine was reading Telegram recently, so the press cannot be older than the age limit (a press has no
    date of its own; the alert it sits on may be old)."""
    m = cq.get("message") or {}
    if str((m.get("chat") or {}).get("id")) != str(c["chat_id"]):
        return
    act, _, pre = (cq.get("data") or "").partition(":")
    if act not in ("continue", "pause", "resume", "status") or not pre:
        return
    names = []
    try:
        names = sorted(n for n in os.listdir(SESS) if n.endswith(".json"))
    except OSError:
        pass
    mine = [x for x in (rjson(os.path.join(SESS, n)) for n in names) if x and x.get("session")]
    mine = [x for x in mine if x["session"] == pre] or [x for x in mine if x["session"].startswith(pre)]
    if not mine:
        return  # another machine's session, left for its own poller
    target = [load_sess(x["session"]) or x for x in mine if x.get("armed")]
    if not target:
        answer_callback(c, cq, "That session is no longer armed.")
        return
    if not fresh:
        answer_callback(c, cq, "Too old (this machine was not reading Telegram). Send /%s." % act)
        return
    for s in target:
        if uid is not None and s["session"] in done_mark(uid):
            continue
        txt = ""
        try:
            txt = apply_command(s, act)
            log("telegram button %s -> %s" % (act, s["session"]))
            tg_send(c, txt, s.get("email", ""), s["session"])
        except SaveBusy:
            raise
        except Exception as e:
            log("telegram button failed for %s: %s: %s" % (s["session"], type(e).__name__, str(e).replace(c["token"], "<token>")))
        answer_callback(c, cq, txt or "Could not apply that.")
        if uid is not None:
            done_mark(uid, s["session"])


def tg_poll(claimed=False):
    """One short getUpdates (timeout 0), at most once per poll_interval per machine. Never raises.
    `claimed`: the hook already took the rate-limit slot for this child."""
    c = tg_conf()
    if not c or not (claimed or poll_due()):
        return
    try:
        with locked(os.path.join(GATE, "telegram-poll.lock"), wait=False, stale=POLL_LOCK_STALE) as got:
            if not got or not (claimed or poll_due()):
                return
            with open(TG_POLLED, "w", encoding="utf-8") as f:
                f.write(str(now()))
            try:
                ups = tg_call(c["token"], "getUpdates", {"timeout": 0, "allowed_updates": ["message", "callback_query"]}) or []
            except RuntimeError as e:
                log("telegram poll failed: %s" % e)
                if "409" in str(e):  # another poller holds the bot: back off
                    with open(TG_POLLED, "w", encoding="utf-8") as f:
                        f.write(str(now() + 45))
                return
            last = (rjson(TG_LAST, {}) or {}).get("last_update_id", 0)
            ack, leading, t = None, True, now()
            try:
                with open(TG_OK, encoding="utf-8") as f:
                    prev_ok = float(f.read())
            except Exception:
                prev_ok = t  # never read before: nothing can be stale
            fresh = t - prev_ok <= TG_CMD_MAX_AGE
            try:
                with open(TG_OK, "w", encoding="utf-8") as f:
                    f.write(str(t))
            except OSError:
                pass
            for u in sorted(ups, key=lambda x: x.get("update_id", 0)):
                uid, m, cq = u.get("update_id", 0), u.get("message") or {}, u.get("callback_query")
                # only message dates decide what is dropped from the shared queue; a stale press is answered, never acked
                old = (not cq) and t - m.get("date", t) > TG_CMD_MAX_AGE
                if leading and old:
                    ack = uid + 1
                else:
                    leading = False
                if uid <= last or old:
                    continue
                try:
                    handle_callback(c, cq, uid, fresh) if cq else handle_message(c, m, uid)
                except SaveBusy as e:  # not applied: leave the id where it was so the next poll retries it
                    log("telegram command busy: %s" % e)
                    break
                except Exception as e:  # any other error: skip this command and move on
                    log("telegram command failed: %s: %s" % (type(e).__name__, str(e).replace(c["token"], "<token>")))
                last = uid
                wjson(TG_LAST, {"last_update_id": uid})
            if ack:  # only commands older than the age limit are dropped from the shared queue
                try:
                    tg_call(c["token"], "getUpdates", {"timeout": 0, "offset": ack, "limit": 1})
                except RuntimeError as e:
                    log("telegram ack failed: %s" % e)
    except Exception as e:
        log("telegram poll failed: %s" % type(e).__name__)


def claim_poll_slot():
    """Take this machine's poll slot (rate limit) before spawning, so a burst of hook calls spawns one child."""
    with locked(TG_POLLED + ".lock", timeout=2) as got:
        if not got or not poll_due():
            return False
        with open(TG_POLLED, "w", encoding="utf-8") as f:
            f.write(str(now()))
        return True


def start_poll_child():
    """From the hook: poll in a detached child so the hook never waits on the network."""
    try:
        if tg_conf() and claim_poll_slot():
            if os.environ.get("USAGE_GATE_SPAWN_COUNT"):  # tests only: one line per spawned poll child
                with open(os.environ["USAGE_GATE_SPAWN_COUNT"], "a", encoding="utf-8") as f:
                    f.write("x\n")
            subprocess.Popen([sys.executable, "-I", os.path.abspath(__file__), "telegram", "poll", "--claimed"],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
    except Exception as e:
        log("telegram poll start failed: %s" % type(e).__name__)


def _stamp():
    try:
        st = os.stat(__file__)
        return (st.st_size, st.st_mtime_ns)
    except OSError:
        return None


_START_STAMP = _stamp()
_PENDING = None   # a changed stamp must hold across two loop turns before it counts (an installer may be mid-write)


def _resume_file(pid):
    return os.path.join(GATE, "resume-%d.json" % pid)


def resumed(cmd):
    """State a loop handed to itself before re-executing into a newer gate.py (empty on a normal start). It lives
    in GATE/resume-<pid>.json, read only when the pid it names is this process (exec keeps the pid) or its parent
    (the Windows wrapper), then deleted."""
    argv = sys.argv
    if "--resume-pid" not in argv:
        return {}
    k = argv.index("--resume-pid")
    try:
        pid = int(argv[k + 1])
    except (IndexError, ValueError):
        pid = -1
    del argv[k:k + 2]
    if pid not in (os.getpid(), os.getppid() if hasattr(os, "getppid") else -2):
        return {}
    f = _resume_file(pid)
    d = rjson(f, {}) or {}
    try:
        os.remove(f)
    except OSError:
        pass
    return d.get("state", {}) if d.get("cmd") == cmd else {}


def _is_nt():
    return testenv("USAGE_GATE_NT") == "1" or os.name == "nt"


def reexec_if_updated(cmd, state):
    """Called once per loop turn: when gate.py changed on disk since this process started (and stayed changed for
    a second turn, and compiles), replace the process by the new code carrying `state`. On POSIX that is exec (same
    pid, same stdout, so the background task stays the same); on Windows exec would end the parent, so the new
    code runs as a child on the same stdio and this process exits with its exit code."""
    global _START_STAMP, _PENDING
    cur = _stamp()
    if _START_STAMP is None or cur is None or cur == _START_STAMP:
        _PENDING = None
        return
    if cur != _PENDING:
        _PENDING = cur
        return
    try:
        with open(__file__, encoding="utf-8") as f:
            compile(f.read(), __file__, "exec")
    except (OSError, SyntaxError, ValueError) as e:
        log("gate.py updated but not loadable (%s): keeping the running code" % type(e).__name__)
        _START_STAMP, _PENDING = cur, None  # log once per version, not every turn
        return
    log("gate.py updated: restarting %s" % cmd)
    args = [a for a in sys.argv[1:]]
    pid = os.getpid()
    try:
        wjson(_resume_file(pid), {"cmd": cmd, "state": state})
        args += ["--resume-pid", str(pid)]
        cmdline = [sys.executable, "-I", os.path.abspath(__file__)] + args
        sys.stdout.flush()
        if _is_nt():
            rc = subprocess.run(cmdline).returncode
            sys.exit(rc)
        os.execv(sys.executable, cmdline)
    except OSError as e:  # keep running the old code rather than die
        log("gate.py restart failed: %s" % type(e).__name__)
        _START_STAMP, _PENDING = cur, None
        try:
            os.remove(_resume_file(pid))
        except OSError:
            pass


def tick(default):
    time.sleep(min(default, float(os.environ.get("USAGE_GATE_TICK", default))))


def cmd_seq(s):
    return (s.get("cmd") or {}).get("seq", 0)


def instruction(s, seq0):
    """What the owner asked for since seq0, as text for the session (empty when nothing came)."""
    cmd = s.get("cmd") or {}
    if cmd.get("seq", 0) <= seq0:
        return ""
    n = cmd.get("name")
    head = "The owner sent /%s from Telegram. " % n
    if n in ("continue", "resume"):
        return head + "Carry on with the work. Read RESUME.md and agent-*.md in %s too. %s" % (
            note_dir(s["session"]), blocked_hint(s["session"]))
    if n == "off":
        return head + "The gate is disarmed: carry on, no more gating."
    u = usage(s["service"], HOOK_TTL)
    if n == "stop":
        return head + msg_hard(s, u, False, "")
    if n == "pause":
        return head + msg_pack(s, u)
    return head


def cmd_listen(sid):
    if not tg_conf():
        print("usage-gate: Telegram is not set up, so no command can reach this session. Run: gate.py telegram setup")
        return
    s = need(sid)
    lf = os.path.join(GATE, "listen-%s.json" % re.sub(r"[^A-Za-z0-9_-]", "", sid))
    st = resumed("listen")
    if st:
        seq0, mine, end = st["seq0"], st["mine"], st["end"]
    else:
        seq0 = cmd_seq(s)
        mine = "%d-%f" % (os.getpid(), now())
        wjson(lf, {"token": mine})
        end = now() + 24 * 3600
    while now() < end:
        tick(5)
        reexec_if_updated("listen", {"seq0": seq0, "mine": mine, "end": end})
        if (rjson(lf, {}) or {}).get("token") != mine:
            print("usage-gate listen: replaced by a newer listen for this session. Nothing to do.")
            return
        tg_poll()
        s = load_sess(sid)
        if not s or not s.get("armed"):
            print((s and instruction(s, seq0)) or "usage-gate listen: Disarmed; nothing to do.")
            return
        msg = instruction(s, seq0)
        if msg:
            print(msg)
            return
    print("usage-gate listen: 24 h passed with no command. Start another listen if you are still waiting.")


def sess_path(sid):
    return os.path.join(SESS, re.sub(r"[^A-Za-z0-9_-]", "", sid) + ".json")


_SNAP = {}  # session id -> the session as last loaded/saved by this process


def load_sess(sid):
    s = rjson(sess_path(sid)) if sid else None
    if s is not None:
        _SNAP[sid] = copy.deepcopy(s)
    return s


class SaveBusy(Exception):
    pass


def save_sess(s, replace=False):
    """Save under a per-session lock, writing back only the keys this process changed since it loaded the
    session, so a concurrent change by another process (a phone command, the hook) is never overwritten.
    Returns False (and logs) when the lock is busy or the write fails; never raises for that, so the hook
    always reaches its decision."""
    sid, path = s["session"], sess_path(s["session"])
    try:
        with locked(path + ".lock") as got:
            if not got:  # a live process holds the session file; never write unlocked
                log("save_sess: lock busy for %s, not saved" % sid)
                return False
            snap, cur = _SNAP.get(sid), rjson(path)
            if not replace and snap is not None and cur is not None:
                for k, v in s.items():
                    if k not in snap or snap[k] != v:
                        cur[k] = v
                s.clear()
                s.update(cur)
            wjson(path, s)
    except OSError as e:
        log("save_sess: write failed for %s: %s" % (sid, type(e).__name__))
        return False
    _SNAP[sid] = copy.deepcopy(s)
    return True


def must_save(s, replace=False):
    """For commands (CLI, phone): a save that did not happen is an error to report, not to hide."""
    if not save_sess(s, replace):
        raise SaveBusy("usage-gate: session file is busy or unwritable, try again")


# ---------- credentials & usage ----------
def cred_file(svc):
    cfg = os.environ.get("CLAUDE_CONFIG_DIR")
    base = os.path.expanduser(cfg) if cfg else os.path.join(HOME, ".claude")
    return os.path.join(base, ".credentials.json")


def kc_read(svc):
    if not has_keychain():  # Linux, Windows: the CLI keeps its login in a file; read-only, never refreshed (D12)
        path = cred_file(svc)
        try:
            with open(path, encoding="utf-8-sig") as f:
                return json.load(f)
        except FileNotFoundError:
            raise RuntimeError("no login: %s not found (run `claude /login`)" % path)
        except (OSError, ValueError) as e:
            raise RuntimeError("no login: cannot read %s (%s)" % (path, type(e).__name__))
    r = subprocess.run(["security", "find-generic-password", "-s", svc, "-w"], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("no keychain login '%s'" % svc)
    return json.loads(r.stdout)


def kc_write(svc, data):
    r = subprocess.run(["security", "find-generic-password", "-s", svc], capture_output=True, text=True)
    m = re.search(r'"acct"<blob>="([^"]*)"', r.stdout)
    acct = m.group(1) if m else os.environ.get("USER", "")
    esc = json.dumps(data, separators=(",", ":")).replace("\\", "\\\\").replace('"', '\\"')
    cmd = 'add-generic-password -U -s "%s" -a "%s" -w "%s"\n' % (svc, acct, esc)
    w = subprocess.run(["security", "-i"], input=cmd, capture_output=True, text=True)
    if w.returncode != 0 or "error" in (w.stdout + w.stderr).lower():
        raise RuntimeError("keychain write failed")


def http(url, token=None, body=None):
    h = {"User-Agent": "usage-gate", "anthropic-beta": "oauth-2025-04-20", "Content-Type": "application/json"}
    if token:
        h["Authorization"] = "Bearer " + token
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=h, method="POST" if data else "GET")
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def refresh(svc, force=False):
    """Refresh a gate-managed login (one nothing else uses). Locked: refresh tokens rotate."""
    os.makedirs(CACHE, exist_ok=True)
    with open(os.path.join(CACHE, "refresh.lock"), "w", encoding="utf-8") as lk:
        if fcntl:
            fcntl.flock(lk, fcntl.LOCK_EX)
        cred = kc_read(svc)
        o = cred["claudeAiOauth"]
        if not force and o.get("expiresAt", 0) / 1000 - now() > 600:  # someone else refreshed meanwhile
            return o["accessToken"]
        r = http(TOKEN_URL, body={"grant_type": "refresh_token", "refresh_token": o["refreshToken"],
                                  "client_id": CLIENT_ID})
        o["accessToken"] = r["access_token"]
        if r.get("refresh_token"):
            o["refreshToken"] = r["refresh_token"]
        o["expiresAt"] = int((now() + int(r.get("expires_in", 3600))) * 1000)
        cred["claudeAiOauth"] = o
        kc_write(svc, cred)
        log("refreshed login %s" % svc)
        return o["accessToken"]


def managed(svc):
    acc = (rjson(CONFIG, {}) or {}).get("accounts", {})
    return any(a.get("service") == svc and a.get("managed") for a in acc.values())


def _refresh_or_login(svc):
    try:
        return refresh(svc)
    except urllib.error.HTTPError as e:
        if e.code in (400, 401, 403):  # invalid_grant: the refresh token is dead, only a new login helps
            raise RuntimeError("login expired (refresh rejected: %d)" % e.code)
        raise


def fetch_usage(svc):
    o = kc_read(svc)["claudeAiOauth"]
    tok = o["accessToken"]
    is_managed = has_keychain() and managed(svc)
    if is_managed and o.get("expiresAt", 0) / 1000 - now() < 600:
        tok = _refresh_or_login(svc)
    try:
        u = http(API + "/api/oauth/usage", token=tok)
    except urllib.error.HTTPError as e:
        if e.code == 401 and is_managed:
            u = http(API + "/api/oauth/usage", token=_refresh_or_login(svc))
        elif e.code == 401:
            raise RuntimeError("login expired (401)")
        else:
            raise
    out = {"ts": now()}
    for key, label in WINDOWS:
        w = u.get(key) or {}
        out[key] = {"pct": float(w.get("utilization") or 0), "resets_at": w.get("resets_at")}
    return out


def cache_write(path, data):
    try:
        wjson(path, data)
    except OSError as e:  # a cache that cannot be written must never stop the reading being used (Windows)
        log("usage cache write failed: %s" % type(e).__name__)


def _login_problem(e):
    """Only these mean "log in again": 401/403 after any refresh, a refresh the server rejected, a missing or
    unreadable login."""
    if isinstance(e, urllib.error.HTTPError):
        return e.code in (401, 403)
    return isinstance(e, (RuntimeError, KeyError))


def _retry_after(e):
    """Seconds from a Retry-After header (delta-seconds or HTTP-date), capped at BACKOFF_MAX. A value below
    BACKOFF_MIN (0, 1, a date in the past) is no guidance: None, so the doubling back-off from 60 s applies.
    Live 2026-11-21 such a header, floored at 10 s, let every process refetch each 10 s (usage()'s fail())."""
    v = None
    try:
        raw = (e.headers.get("Retry-After") or "").strip()
    except AttributeError:
        raw = ""
    if raw:
        try:
            v = float(raw)
        except ValueError:
            try:
                from email.utils import parsedate_to_datetime
                v = parsedate_to_datetime(raw).timestamp() - now()
            except (TypeError, ValueError):
                v = None
    return None if v is None or v < BACKOFF_MIN else min(BACKOFF_MAX, v)


def usage_path(svc):
    return os.path.join(CACHE, "usage-" + hashlib.sha1(svc.encode()).hexdigest()[:10] + ".json")


def _norm(c):
    c = c or {}
    if "five_hour" in c:  # cache written by an older gate: a bare reading
        return {"good": c}
    return {} if "error" in c else c


def cache_update(path, fn):
    """Read-modify-write the shared usage cache under a lock, so concurrent hooks never lose each other's update."""
    try:
        cm = locked(path + ".lock", timeout=5)
        got = cm.__enter__()
    except OSError:
        cm, got = None, True  # no cache folder: go on without the lock (the write then fails and is logged)
    try:
        c = _norm(rjson(path))
        r = fn(c)
        if got:  # lock not acquired: the change stays in memory, a racing writer's file is never overwritten
            cache_write(path, c)
        return c, r
    finally:
        if cm:
            cm.__exit__(None, None, None)


def _fallback(c):
    """The last good reading while it is fresh enough (marked stale), else the error and how long it has lasted."""
    good, err = c.get("good"), c.get("err") or {}
    if err.get("login"):
        return {"ts": err.get("ts", now()), "error": err["error"], "login": True}
    if good and now() - good.get("ts", 0) < STALE_OK:
        return dict(good, stale=True)
    since = good.get("ts") if good else c.get("err_since", err.get("ts", now()))
    return {"ts": err.get("ts", now()), "error": err.get("error", "usage unreadable"), "transient": True,
            "kind": err.get("kind", "network"), "down_for": now() - since,
            "retry_at": c.get("retry_after_until") or (err.get("ts", now()) + HOOK_TTL)}


def notice_due(svc, key="err_told"):
    """True once an hour per account (not per session): the stamp lives in the shared cache."""
    def f(c):
        if now() - c.get(key, 0) > 3600:
            c[key] = now()
            return True
        return False
    return cache_update(usage_path(svc), f)[1]


def usage(svc, ttl):
    """Latest reading. The cache keeps the last good reading apart from errors (`good`), so a 429 or a network
    blip never erases it: it is returned marked `stale` while younger than STALE_OK. After a 429 every process
    skips fetching until `retry_after_until`."""
    fake = testenv("USAGE_GATE_FAKE")  # tests: "five_hour_pct,seven_day_pct"
    if fake:
        time.sleep(float(testenv("USAGE_GATE_FAKE_DELAY", 0)))
        a, b = (float(x) for x in fake.split(","))
        return {"ts": now() - float(testenv("USAGE_GATE_FAKE_AGE", 0)), "five_hour": {"pct": a, "resets_at": None}, "seven_day": {"pct": b, "resets_at": None}}
    path = usage_path(svc)

    def cached(c):
        """The reading to use without fetching, or None when a fetch is due."""
        good = c.get("good")
        if good and now() - good.get("ts", 0) < ttl:
            return good
        if now() < c.get("retry_after_until", 0):
            return _fallback(c)
        err = c.get("err")
        if err and ttl > 0 and "retry_after_until" not in c and now() - err.get("ts", 0) < min(ttl, HOOK_TTL):
            return _fallback(c)  # a fresh error is reused like a reading
        return None

    r = cached(_norm(rjson(path)))
    if r is not None:
        return r

    def claim(c):  # under the cache lock: exactly one process fetches when a window or reading expires
        r = cached(c)
        if r is None and now() < c.get("fetching_until", 0):
            return _fallback(c)  # another process is fetching right now
        if r is None:
            c["fetching_until"] = now() + FETCH_SLOT
        return r
    c, r = cache_update(path, claim)
    if r is not None:
        return r
    try:
        # one line per real HTTP fetch, so a log shows who hit the API (caller function, gate command)
        log("usage fetch for %s by %s (cmd %s)" % (svc, sys._getframe(1).f_code.co_name, " ".join(sys.argv[1:2]) or "-"))
        r = fetch_usage(svc)
    except Exception as e:
        def fail(c):
            c.pop("fetching_until", None)
            err = c["err"] = {"ts": now(), "error": "%s: %s" % (type(e).__name__, e)}
            if _login_problem(e):
                err["login"] = True
                return
            is429 = getattr(e, "code", 0) == 429
            err["kind"] = "rate-limited" if is429 else "network"
            c.setdefault("err_since", now())
            if is429:
                ra = _retry_after(e)
                if ra is None:
                    ra = min(BACKOFF_MAX, max(BACKOFF_MIN, 2 * c.get("backoff", BACKOFF_MIN / 2)))
                c["backoff"] = ra
                c["retry_after_until"] = now() + ra
            elif c.get("retry_after_until", 0) <= now():  # another process's still-valid 429 window stays
                c.pop("retry_after_until", None)
                c.pop("backoff", None)
        c, _ = cache_update(path, fail)
        log("usage error for %s: %s" % (svc, c["err"]["error"]))
        return _fallback(c)

    def ok(c):
        if not c.get("good") or c["good"].get("ts", 0) <= r.get("ts", 0):
            c["good"] = r
        for k in ("err", "err_since", "retry_after_until", "backoff", "fetching_until"):
            c.pop(k, None)
    c, _ = cache_update(path, ok)
    return c["good"]


def level(s, u):
    top = max(u[k]["pct"] for k, _ in WINDOWS)
    return "hard" if top >= s["hard"] else "soft" if top >= s["soft"] else "ok"


def figures(u):
    if "error" in u:
        return "usage unreadable"
    parts = []
    for k, label in WINDOWS:
        w = u[k]
        r = w.get("resets_at")
        when = ""
        if r:
            try:
                from datetime import datetime
                dt = datetime.fromisoformat(r.replace("Z", "+00:00")).astimezone()
                when = " (resets %s)" % dt.strftime("%a %H:%M")
            except Exception:
                pass
        parts.append("%s %d%%%s" % (label, w["pct"], when))
    return ", ".join(parts)


def session_service():
    uuid = os.environ.get("CLAUDE_CODE_ACCOUNT_UUID") if has_keychain() else None  # no keychain: the login file
    if uuid:
        acc = (rjson(CONFIG, {}) or {}).get("accounts", {})
        if uuid not in acc:
            cmd_accounts(quiet=True)
            acc = (rjson(CONFIG, {}) or {}).get("accounts", {})
        if uuid in acc:
            return acc[uuid]["service"], acc[uuid].get("email", "")
        raise SystemExit("usage-gate: no keychain login for this session's account (%s). "
                         "Log it in once: CLAUDE_CONFIG_DIR=~/.claude-appacct claude /login" % uuid)
    cfg = os.environ.get("CLAUDE_CONFIG_DIR")
    if cfg:
        svc = DEFAULT_SVC + "-" + hashlib.sha256(os.path.abspath(os.path.expanduser(cfg)).encode()).hexdigest()[:8]
    else:
        svc = DEFAULT_SVC
    return svc, email_for_service(svc)


def email_for_service(svc):
    # No account uuid (e.g. VS Code): find the email from the cached `accounts` map by login name.
    for a in ((rjson(CONFIG, {}) or {}).get("accounts", {}) or {}).values():
        if a.get("service") == svc:
            return a.get("email", "")
    return ""


# ---------- hook ----------
BLOCKED_CALL = {}  # set by hook(): who is calling, so every deny can be recorded in blocked.md


SECRET_CUT = re.compile(r"token|key|password|passw|pwd|secret|auth|bearer|sk-", re.I)
BLOCKED_MAX_LINES = 200
BLOCKED_KEEP = 3


def blocked_summary(tool, ti):
    """One short line about a denied call; anything after a secret-looking word is cut off."""
    ti = ti if isinstance(ti, dict) else {}
    text = ti.get("command") or ti.get("file_path") or ti.get("description") or ti.get("path") or ""
    text = " ".join(str(text).split())
    text = SECRET_CUT.split(text)[0].strip()
    return text[:80]


def blocked_path(sid):
    return os.path.join(note_dir(sid), "blocked.md")


def clear_blocked(sid):
    try:
        os.remove(blocked_path(sid))
    except OSError:
        pass


def archive_blocked(sid):
    """Move blocked.md to blocked-<time>.md (keeping the newest few); return the new path, or "" when none."""
    src = blocked_path(sid)
    if not os.path.exists(src):
        return ""
    dst = os.path.join(note_dir(sid), "blocked-%s.md" % time.strftime("%Y%m%d-%H%M%S"))
    try:
        os.replace(src, dst)
        old = sorted(f for f in os.listdir(note_dir(sid)) if re.fullmatch(r"blocked-\d{8}-\d{6}\.md", f))
        for f in old[:-BLOCKED_KEEP]:
            os.remove(os.path.join(note_dir(sid), f))
    except OSError:
        return ""
    return dst


def blocked_hint(sid):
    """The resume-message sentence about refused calls; archives blocked.md so the next resume lists new entries only."""
    arch = archive_blocked(sid)
    if arch:
        return ("Also read %s (the calls the gate refused meanwhile, formerly blocked.md): redo or deliberately "
                "drop each entry and say which in one line." % arch)
    return "blocked.md: no calls were refused."


def record_blocked(sid, tool, ti, aid):
    s = load_sess(sid) or {}
    if not s.get("armed"):
        return
    line = "%s | %s | %s | %s | %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), s.get("phase", "run"),
                                      aid or "lead", tool, blocked_summary(tool, ti))
    path = blocked_path(sid)
    os.makedirs(note_dir(sid), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(line + "\n")
    with open(path, encoding="utf-8") as f:
        lines = f.read().splitlines()
    if len(lines) > BLOCKED_MAX_LINES:
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines[-BLOCKED_MAX_LINES:]) + "\n")


def out(decision=None, reason=None, ctx=None, updated=None, event="PreToolUse"):
    h = {"hookEventName": event}
    if decision:
        h["permissionDecision"] = decision
    if reason:
        h["permissionDecisionReason"] = reason
    if updated:
        h["updatedInput"] = updated
    if ctx:
        h["additionalContext"] = ctx
    print(json.dumps({"hookSpecificOutput": h}), flush=True)
    if decision == "deny" and BLOCKED_CALL:  # after the decision is out: a log failure must never undo a deny
        try:
            record_blocked(*BLOCKED_CALL["call"])
        except Exception as e:
            log("blocked.md write failed: %s" % type(e).__name__)
    sys.exit(0)


def note_dir(sid):
    return os.path.join(NOTES, sid)


def hard_allowed(tool, ti, sid):
    if tool in HARD_OK or tool.startswith("mcp__ccd_turn__"):
        return True
    if tool in ("Write", "Edit"):
        p = os.path.abspath(os.path.expanduser(ti.get("file_path", "")))
        return p.startswith(note_dir(sid) + os.sep)
    return False


def msg_ask(s, u):
    return ("USAGE GATE (%g%% soft limit reached): %s. Do NOT start any new step, agent or session. "
            "Agents already running may keep going. Now: (1) ask the user in one line whether to continue "
            "or pause, via wrap_up with buttons 'Continue' / 'Pause', or in plain text if your app has no wrap_up "
            "(never AskUserQuestion: it has no timeout); "
            "(2) start the timeout in the background: `%s timer` (run_in_background); (3) end your turn. "
            "If the user says continue: run `%s continue`. If pause: run `%s pause`."
            % (s["soft"], figures(u), SELF, SELF, SELF))


def msg_pack(s, u):
    nd = note_dir(s["session"])
    return ("USAGE GATE: packing up (%s). Start nothing new. Let running agents finish and hand back. "
            "Then write a resume note to %s/RESUME.md (goal, what is done, what is next with exact steps, "
            "running state, files touched), then run in the background: `%s wait` and end your turn with one "
            "line to the user. It resumes by itself once both limits are below %g%%."
            % (figures(u), nd, SELF, s["soft"]))


def msg_hard(s, u, is_sub, aid):
    nd = note_dir(s["session"])
    if is_sub:
        return ("USAGE GATE HARD STOP (%g%%): %s. Stop working now. Write your progress (done, next steps, "
                "files touched, anything half-finished) to %s/agent-%s.md, then end with a short handback "
                "that points to that file." % (s["hard"], figures(u), nd, aid or "x"))
    return ("USAGE GATE HARD STOP (%g%%): %s. All tools are blocked except notes. Agents are being told to "
            "write checkpoints to %s/ and stop; wait for their handbacks (TaskStop any that hang). Then write "
            "%s/RESUME.md (goal, done, next, agent checkpoints to re-dispatch), run in the background "
            "`%s wait`, and end your turn with one line to the user." % (s["hard"], figures(u), nd, nd, SELF))


def msg_paused(s, hardish=False):
    if s.get("manual"):
        return ("USAGE GATE: the owner %s this session from Telegram. Do nothing more and end your turn; "
                "only the owner can resume it (/resume or /continue from the phone)."
                % ("stopped" if s["manual"] == "stop" else "paused"))
    if hardish or s.get("phase") == "paused_hard":
        return ("USAGE GATE: this session is paused for usage. If the user has just told you to resume, run "
                "`%s resume` first, then continue. Otherwise do nothing more and end your turn." % SELF)
    return ("USAGE GATE: this session is paused for usage. Finish your current work and start nothing new (no "
            "agents, sessions or claude commands); it resumes by itself once limits drop below %g%%. If the user "
            "has just told you to resume, run `%s resume` first." % (s["soft"], SELF))


def tell_once(s, ph, msg):
    """The session obeys a message, so soft phases tell instead of block: the first call after a phase starts gets
    `msg` as context (and the next PostToolUse repeats it once); later calls pass in silence."""
    if s.get("told") == ph:
        return
    s.update(told=ph, post_tell=msg)
    save_sess(s)
    out(ctx=msg)


def hook():
    try:
        inp = json.loads(sys.stdin.buffer.read().decode("utf-8-sig"))
    except Exception:
        sys.exit(0)
    ev = inp.get("hook_event_name", "PreToolUse")
    sid = inp.get("session_id") or ""
    tool = inp.get("tool_name") or ""
    ti = inp.get("tool_input") or {}
    aid = inp.get("agent_id") or ""
    is_sub = bool(aid)
    if ev == "PreToolUse":
        BLOCKED_CALL["call"] = (sid, tool, ti, aid)

    # While the owner's phone /stop or /pause holds, the model may not lift it by any spelling.
    if ev == "PreToolUse" and tool == "Bash" and sid:
        cmd = ti.get("command", "")
        owner = (load_sess(sid) or {}).get("manual") if "gate.py" in cmd else None
        if owner and re.search(r"\b(continue|resume|disarm|arm)\b", cmd):
            out("deny", "USAGE GATE: the owner %s this session from Telegram. Only the owner can undo that: "
                        "/resume or /continue from the phone, or `gate.py resume` in a terminal. Do not try to "
                        "lift it yourself; carry out the stop or pause." % ("stopped" if owner == "stop" else "paused"))

    # Test overrides (USAGE_GATE_*) are never the model's to set: an armed session may not run such a command.
    if ev == "PreToolUse" and tool == "Bash" and "USAGE_GATE_" in ti.get("command", "") and (load_sess(sid) or {}).get("armed"):
        out("deny", "USAGE GATE: commands that name USAGE_GATE_* variables are not allowed while the gate is armed.")

    # Gate commands: always allowed; inject the session id so they work in any app.
    if ev == "PreToolUse" and tool == "Bash" and GATE_CMD.match(ti.get("command", "")):
        cmd = ti["command"]
        if "--session" not in cmd and sid:
            new = dict(ti)
            new["command"] = cmd.rstrip() + " --session " + sid
            out("allow", "usage-gate command", updated=new)
        out("allow", "usage-gate command")

    s = load_sess(sid)
    if not s or not s.get("armed"):
        sys.exit(0)
    if s.get("debug_keys", 0) < 3:  # record what hook inputs look like (lead vs agent)
        s["debug_keys"] = s.get("debug_keys", 0) + 1
        log("hook input keys %s tool=%s agent=%s" % (sorted(inp.keys()), tool, aid))
        save_sess(s)

    start_poll_child()
    man = s.get("manual")  # owner's /stop or /pause from the phone: holds until /resume or /continue
    u = usage(s["service"], HOOK_TTL)
    s = load_sess(sid) or s  # usage() can take long: decide on the session as it is now
    if not s.get("armed"):
        sys.exit(0)
    man = s.get("manual")
    if "error" in u and not man:
        if ev == "PostToolUse" and u.get("login") and now() - s.get("err_told", 0) > 3600:
            s["err_told"] = now()
            save_sess(s)
            notify("Can't read usage for %s. Log in again." % (s.get("email") or s["service"]), s)
            out(ctx="usage-gate cannot read usage (%s). Tell the user in one line: log in again with "
                    "%s. Gating is off until then; carry on." % (u["error"],
                    "`CLAUDE_CONFIG_DIR=~/.claude-appacct claude /login` (app account) or `claude /login` (CLI "
                    "account)" if has_keychain() else "`claude /login`"), event="PostToolUse")
        elif (ev == "PostToolUse" and not u.get("login") and u.get("down_for", 0) > STALE_OK
              and notice_due(s["service"])):
            mins = int(u["down_for"] // 60)
            again = max(1, int(round((u.get("retry_at", now()) - now()) / 60)))
            msg = ("Can't read usage for %s for %d min (%s); gating paused until it reads again (next try in about "
                   "%d min)." % (s.get("email") or s["service"], mins, u.get("kind", "network"), again))
            notify(msg, s)
            out(ctx="usage-gate: %s Tell the user in one line; carry on." % msg, event="PostToolUse")
        sys.exit(0)
    if ev == "PostToolUse" and not is_sub and s.get("post_tell"):
        msg = s["post_tell"]
        s["post_tell"] = None
        save_sess(s)
        out(ctx=msg, event="PostToolUse")
    if ev != "PreToolUse":
        sys.exit(0)

    lv = "ok" if "error" in u else level(s, u)
    ph = s.get("phase", "run")
    ov = s.get("override")
    if lv == "ok" and u.get("stale") and (ph != "run" or ov):  # only a fresh reading may lift a stop or pause
        lv = "hard" if ph.endswith("hard") else "soft"
    t = now()
    if man == "stop":
        lv, ov = "hard", None
    elif man == "pause":
        lv, ov = ("soft" if lv == "ok" else lv), None

    if lv == "ok":
        if ph != "run" or ov:
            s.update(phase="run", override=None, told=None, post_tell=None)
            save_sess(s)
        sys.exit(0)

    if ph.startswith("paused"):
        hardish = ph == "paused_hard" or lv == "hard"
        if is_new_work(tool, ti):
            out("deny", msg_paused(s, hardish))
        if is_sub:
            sys.exit(0)
        if man:  # owner's /stop or /pause blocks everything
            out("deny", msg_paused(s))
        if hardish:  # usage past the hard limit: only notes and session tools, as at a hard stop
            if hard_allowed(tool, ti, sid):
                sys.exit(0)
            out("deny", msg_paused(s, True))
        tell_once(s, ph, msg_paused(s))
        sys.exit(0)

    if lv == "hard" and ov != "hard":
        if ph != "hard":
            s.update(phase="hard", told=None)
            save_sess(s)
            notify("Hard stop at %s%%: %s" % (s["hard"], figures(u)), s, u)
        if hard_allowed(tool, ti, sid):
            sys.exit(0)
        out("deny", msg_hard(s, u, is_sub, aid))

    if ov:  # user said continue at this level
        sys.exit(0)

    # soft level
    if ph == "run":
        s.update(phase="asked", asked_at=t, told=None)
        save_sess(s)
        notify("%s%% reached (%s). Answer in the session within %s min or it packs up."
               % (s["soft"], figures(u), s["ask"]), s, u)
        ph = "asked"
    if ph == "asked" and t - s.get("asked_at", t) > s["ask"] * 60:
        s.update(phase="packing", told=None)
        save_sess(s)
        notify("No answer: packing up the session.", s)
        ph = "packing"
    if is_sub:
        if is_new_work(tool, ti):
            out("deny", "USAGE GATE: no new agents now. Finish your current task and hand back.")
        sys.exit(0)
    msg = msg_ask(s, u) if ph == "asked" else msg_pack(s, u)
    if is_new_work(tool, ti):
        out("deny", msg)
    if s.get("told") != ph and man:  # the owner's phone /pause blocks the first call
        s["told"] = ph
        save_sess(s)
        out("deny", msg)
    tell_once(s, ph, msg)
    sys.exit(0)


# ---------- commands ----------
def args_session(argv):
    if "--session" in argv:
        return argv[argv.index("--session") + 1]
    return os.environ.get("CLAUDE_CODE_SESSION_ID", "")


def opt(argv, name, default):
    return float(argv[argv.index(name) + 1]) if name in argv else default


def need(sid):
    s = load_sess(sid)
    if not s:
        raise SystemExit("usage-gate: this session is not armed. Run /usage-gate first.")
    return s


def cmd_accounts(quiet=False):
    if not has_keychain():
        if not quiet:
            print("usage-gate: no keychain on this machine; accounts are not mapped, the login file is used.")
        return
    r = subprocess.run(["security", "dump-keychain"], capture_output=True, text=True)
    svcs = sorted(set(re.findall(r'"svce"<blob>="(Claude Code-credentials[^"]*)"', r.stdout)))
    cfg = rjson(CONFIG, {}) or {}
    acc = cfg.get("accounts", {})
    for svc in svcs:
        try:
            tok = kc_read(svc)["claudeAiOauth"]["accessToken"]
            try:
                p = http(API + "/api/oauth/profile", token=tok)
            except urllib.error.HTTPError:
                if svc == DEFAULT_SVC:
                    raise
                p = http(API + "/api/oauth/profile", token=refresh_unmanaged_ok(svc))
            a = p.get("account", {})
            acc[a["uuid"]] = {"service": svc, "email": a.get("email", ""), "managed": svc != DEFAULT_SVC}
        except Exception as e:
            log("accounts: %s: %s" % (svc, e))
    cfg["accounts"] = acc
    wjson(CONFIG, cfg)
    if not quiet:
        for k, v in acc.items():
            print("%s  %s  %s%s" % (k, v["email"], v["service"], "  (gate refreshes it)" if v["managed"] else ""))


def refresh_unmanaged_ok(svc):
    # During discovery a non-default login is ours to refresh (it was made only for the gate).
    cfg = rjson(CONFIG, {}) or {}
    cfg.setdefault("accounts", {})["_tmp"] = {"service": svc, "managed": True}
    wjson(CONFIG, cfg)
    try:
        return refresh(svc)
    finally:
        cfg = rjson(CONFIG, {}) or {}
        cfg.get("accounts", {}).pop("_tmp", None)
        wjson(CONFIG, cfg)


def main(argv):
    try:
        return main2(argv)
    except SaveBusy as e:
        raise SystemExit(str(e))


def main2(argv):
    if not argv:
        print(__doc__)
        return
    c = argv[0]
    if c == "hook":
        try:
            return hook()
        except Exception as e:  # the hook must never exit non-zero: that would let a blocked tool run
            log("hook failed: %s: %s" % (type(e).__name__, e))
            sys.exit(0)
    if c == "accounts":
        return cmd_accounts()
    if c == "telegram":
        return cmd_telegram(argv[1:])
    sid = args_session(argv)
    if not sid:
        raise SystemExit("usage-gate: no session id")
    if c == "arm":
        svc, email = session_service()
        old = load_sess(sid) or {}
        s = {"session": sid, "armed": True, "service": svc, "email": email,
             "soft": opt(argv, "--soft", old.get("soft", 80)), "hard": opt(argv, "--hard", old.get("hard", 95)),
             "ask": opt(argv, "--ask", old.get("ask", 10)), "phase": "run", "override": None,
             "armed_at": now(), "debug_keys": 0, "project": os.path.basename(os.getcwd())}
        must_save(s, replace=True)
        os.makedirs(note_dir(sid), exist_ok=True)
        u = usage(svc, 60)
        if "error" in u:
            print("Armed, but usage can't be read: %s" % u["error"])
            return
        print("Armed for %s: soft %g%%, hard %g%%, question timeout %g min.\nNow: %s -> %s"
              % (email or svc, s["soft"], s["hard"], s["ask"], figures(u), level(s, u)))
        return
    if c == "listen":
        return cmd_listen(sid)
    s = need(sid)
    if c == "disarm":
        s.update(armed=False, manual=None)
        must_save(s)
        clear_blocked(sid)
        print("Disarmed.")
    elif c == "status":
        u = usage(s["service"], HOOK_TTL)
        print(json.dumps({k: s.get(k) for k in ("email", "soft", "hard", "ask", "phase", "override", "armed")}))
        print(u.get("error") or "%s -> %s" % (figures(u), level(s, u)))
    elif c in ("continue", "resume"):
        print(do_continue(s))
    elif c == "pause":
        s.update(phase="packing", told=None, override=None, manual=None)
        must_save(s)
        u = usage(s["service"], HOOK_TTL)
        print(msg_pack(s, u) if "error" not in u else "Pack up, then run `%s wait` in the background." % SELF)
    elif c == "timer":
        st = resumed("timer")
        end = st.get("end", now() + s["ask"] * 60)
        seq0, asked0 = st.get("seq0", cmd_seq(s)), st["asked0"] if "asked0" in st else s.get("asked_at")

        def waiting(x):  # still the same question, still armed
            return x.get("phase") == "asked" and x.get("armed") and x.get("asked_at") == asked0

        def gone(x):
            print("usage-gate timer: no longer waiting for an answer (phase now %s). %s"
                  % (x.get("phase"), instruction(x, seq0) or "Nothing to do."))
        while True:
            tg_poll()  # a phone answer is read before any decision, including at the deadline
            s = need(sid)
            if not waiting(s):
                return gone(s)
            if now() >= end:
                break
            tick(10)
            reexec_if_updated("timer", {"end": end, "seq0": seq0, "asked0": asked0})
        path = sess_path(sid)
        with locked(path + ".lock") as got:  # check and set under the session lock
            cur = rjson(path) if got else None
            if got and not (cur and waiting(cur)):
                return gone(cur or s)
            if got:
                cur.update(phase="packing", told=None)
                wjson(path, cur)
                _SNAP[sid] = copy.deepcopy(cur)
                s = cur
        if not got:
            s.update(phase="packing", told=None)
            must_save(s)
        notify("No answer in %g min: packing up." % s["ask"], s)
        u = usage(s["service"], HOOK_TTL)
        print("usage-gate timer: no answer in %g min. %s" % (s["ask"], msg_pack(s, u) if "error" not in u else ""))
    elif c == "wait":
        st = resumed("wait")
        if not st:  # a restart into newer code goes straight back to waiting
            s.update(phase="paused_hard" if s.get("phase") == "hard" else "paused_soft", told=None)
            must_save(s)
            if s.get("manual"):  # owner's phone /stop or /pause holds regardless of usage
                notify("Session paused by you. It holds until you send /resume or /continue.", s)
            else:
                notify("Session paused. It resumes when limits drop below %g%%, or send /resume." % s["soft"], s)
        last, seq0 = st.get("last", 0), st.get("seq0", cmd_seq(s))
        while True:
            reexec_if_updated("wait", {"last": last, "seq0": seq0})
            tg_poll()
            s = need(sid)
            if not s.get("phase", "").startswith("paused") or not s.get("armed"):
                print("usage-gate: resumed by the user. %s" % (instruction(s, seq0) or "Nothing to do."))
                return
            if not s.get("manual") and now() - last >= WAIT_POLL:
                last = now()
                u = usage(s["service"], 60)
                if "error" not in u and not u.get("stale") and level(s, u) == "ok":
                    s.update(phase="run", override=None, told=None)
                    must_save(s)
                    notify("Limits reset (%s). Resuming." % figures(u), s, u)
                    print("USAGE GATE: limits are back below %g%% (%s). Resume now: read %s/RESUME.md and any "
                          "agent-*.md checkpoints there. %s Then continue the plan "
                          "(re-dispatch paused agents with their checkpoint). Tell the user in one line that you "
                          "resumed." % (s["soft"], figures(u), note_dir(sid), blocked_hint(sid)))
                    return
            tick(15)
    else:
        print(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
