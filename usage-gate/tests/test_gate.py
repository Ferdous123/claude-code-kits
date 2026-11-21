"""Gate state machine tests. Isolated: HOME points at a temp folder, usage is faked, notifications are quiet."""
import http.server, json, os, shutil, subprocess, sys, tempfile, threading, time, unittest

GATE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "gate.py")
SID = "test-sess"


class GateBase(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="ug home ")
        self.env = dict(os.environ, HOME=self.home, USAGE_GATE_QUIET="1", USAGE_GATE_TEST="1", CLAUDE_CODE_SESSION_ID=SID)
        self.env.pop("CLAUDE_CODE_ACCOUNT_UUID", None)
        self.env.pop("CLAUDE_CONFIG_DIR", None)

    def cmd(self, *args, fake="10,10"):
        env = dict(self.env, USAGE_GATE_FAKE=fake)
        return subprocess.run([sys.executable, "-I", GATE] + list(args), env=env,
                              capture_output=True, text=True, timeout=30).stdout

    def hook(self, fake, tool, agent="", ti=None, event="PreToolUse"):
        inp = {"hook_event_name": event, "session_id": SID, "tool_name": tool, "tool_input": ti or {}}
        if agent:
            inp["agent_id"] = agent
        env = dict(self.env, USAGE_GATE_FAKE=fake)
        r = subprocess.run([sys.executable, "-I", GATE, "hook"], input=json.dumps(inp), env=env,
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        if not r.stdout.strip():
            return "allow", ""
        h = json.loads(r.stdout)["hookSpecificOutput"]  # no permissionDecision means allow; a tell rides in additionalContext
        return h.get("permissionDecision", "allow"), h.get("permissionDecisionReason") or h.get("additionalContext", "")

    def sess(self):
        with open(os.path.join(self.home, ".claude", "usage-gate", "sessions", SID + ".json")) as f:
            return json.load(f)

    def arm(self, ask="10"):
        self.cmd("arm", "--ask", ask)


class GateTest(GateBase):
    def blocked(self):
        path = os.path.join(self.home, ".claude", "usage-gate", "notes", SID, "blocked.md")
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as f:
            return f.read().splitlines()

    def test_unarmed_session_is_untouched(self):
        self.assertEqual(self.hook("99,99", "Bash")[0], "allow")

    def test_below_soft_allows(self):
        self.arm()
        self.assertEqual(self.hook("10,10", "Agent")[0], "allow")

    def test_soft_asks_lead_once_then_blocks_only_new_work(self):
        self.arm()
        d, r = self.hook("50,82", "SendMessage")  # first call after soft is a tell, never a block
        self.assertEqual(d, "allow")
        self.assertIn("ask the user", r)
        self.assertEqual(self.sess()["phase"], "asked")
        self.assertEqual(self.hook("50,82", "Edit")[0], "allow")
        self.assertEqual(self.hook("50,82", "Agent")[0], "deny")
        self.assertEqual(self.hook("50,82", "mcp__ccd_session__start_session")[0], "deny")

    def test_denied_new_work_at_soft_is_logged_in_blocked_md(self):
        self.arm()
        self.hook("50,82", "Bash")
        self.assertEqual(self.hook("50,82", "Agent", ti={"description": "build the thing token=SECRET123"})[0], "deny")
        lines = self.blocked()
        self.assertEqual(len(lines), 1, lines)
        for part in ("asked", "lead", "Agent", "build the thing"):
            self.assertIn(part, lines[0])
        self.assertNotIn("SECRET123", lines[0])
        self.assertEqual(self.hook("50,82", "Edit")[0], "allow")  # allowed calls add nothing
        self.assertEqual(len(self.blocked()), 1)

    def test_denied_call_at_hard_is_logged_with_agent_id(self):
        self.arm()
        self.assertEqual(self.hook("50,96", "Bash", agent="a1", ti={"command": "ls " + "x" * 200})[0], "deny")
        self.assertEqual(self.hook("50,96", "Bash", ti={"command": "rm -rf /tmp/zz"})[0], "deny")
        lines = self.blocked()
        self.assertEqual(len(lines), 2, lines)
        self.assertIn("a1", lines[0])
        self.assertIn("hard", lines[0])
        self.assertLessEqual(len(lines[0].split(" | ")[-1]), 80)
        self.assertIn("lead", lines[1])
        self.assertIn("rm -rf /tmp/zz", lines[1])

    def test_limits_back_message_names_blocked_md(self):
        self.arm()
        self.cmd("pause", fake="50,82")
        self.assertIn("blocked.md", self.cmd("wait", fake="5,3"))

    def test_limits_back_hands_over_blocked_md_once_and_keeps_the_last_three_archives(self):
        self.arm()
        notes = os.path.join(self.home, ".claude", "usage-gate", "notes", SID)
        os.makedirs(notes, exist_ok=True)
        for i in range(4):
            with open(os.path.join(notes, "blocked-2020010%d-000000.md" % i), "w") as f:
                f.write("old\n")
        self.hook("50,96", "Bash", ti={"command": "rm -rf /tmp/zz"})
        self.cmd("pause", fake="50,96")
        out = self.cmd("wait", fake="5,3")
        self.assertRegex(out, r"blocked-\d{8}-\d{6}")
        self.assertEqual(self.blocked(), [])  # handed over: the next resume will not list it again
        archives = sorted(f for f in os.listdir(notes) if f.startswith("blocked-"))
        self.assertEqual(len(archives), 3, archives)
        with open(os.path.join(notes, archives[-1])) as f:
            self.assertIn("rm -rf /tmp/zz", f.read())

    def test_disarm_clears_blocked_md(self):
        self.arm()
        self.hook("50,96", "Bash", ti={"command": "ls"})
        self.assertEqual(len(self.blocked()), 1)
        self.cmd("disarm")
        self.assertEqual(self.blocked(), [])

    def test_soft_lets_subagents_finish_but_not_spawn(self):
        self.arm()
        self.assertEqual(self.hook("50,82", "Bash", agent="a1")[0], "allow")
        self.assertEqual(self.hook("50,82", "Agent", agent="a1")[0], "deny")

    def test_question_timeout_packs_up(self):
        self.arm(ask="0.01")
        self.hook("50,82", "Bash")
        time.sleep(1.2)
        d, r = self.hook("50,82", "Read")
        self.assertEqual((d, self.sess()["phase"]), ("allow", "packing"))
        self.assertIn("RESUME.md", r)
        self.assertEqual(self.hook("50,82", "Edit")[0], "allow")
        self.assertEqual(self.hook("50,82", "Workflow")[0], "deny")

    def test_timer_exits_with_pack_message_when_unanswered(self):
        self.arm(ask="0.01")
        self.hook("50,82", "Bash")
        out = self.cmd("timer", fake="50,82")
        self.assertIn("no answer", out)
        self.assertEqual(self.sess()["phase"], "packing")

    def test_continue_overrides_until_hard(self):
        self.arm()
        self.hook("50,82", "Bash")
        self.assertIn("Next gate at 95", self.cmd("continue", fake="50,82"))
        self.assertEqual(self.hook("50,88", "Agent")[0], "allow")
        d, r = self.hook("50,96", "Bash")
        self.assertEqual(d, "deny")
        self.assertIn("HARD STOP", r)

    def test_hard_stop_allows_only_notes_and_session_tools(self):
        self.arm()
        notes = os.path.join(self.home, ".claude", "usage-gate", "notes", SID)
        self.assertEqual(self.hook("50,96", "Edit", agent="a1", ti={"file_path": "/tmp/x.py"})[0], "deny")
        self.assertEqual(self.hook("50,96", "Write", agent="a1",
                                   ti={"file_path": os.path.join(notes, "agent-a1.md")})[0], "allow")
        self.assertEqual(self.hook("50,96", "Write", agent="a1",
                                   ti={"file_path": notes + "/../../x.md"})[0], "deny")
        self.assertEqual(self.hook("50,96", "mcp__ccd_turn__wrap_up")[0], "allow")
        self.assertEqual(self.hook("50,96", "TaskStop")[0], "allow")
        self.assertEqual(self.hook("50,96", "SubagentHandback", agent="a1")[0], "allow")
        self.assertEqual(self.hook("50,96", "SubagentHandback")[0], "allow")
        self.assertEqual(self.hook("50,96", "SubagentHandbackX", agent="a1")[0], "deny")

    def test_soft_question_falls_back_to_plain_text_without_wrap_up(self):
        self.arm()
        d, r = self.hook("85,10", "Bash")
        self.assertEqual(d, "allow")
        self.assertIn("plain text", r)
        self.assertIn("wrap_up", r)

    def test_gate_commands_get_session_injected(self):
        d, _ = self.hook("99,99", "Bash", ti={"command": "python3 -I ~/.claude/usage-gate/gate.py status"})
        self.assertEqual(d, "allow")
        inp = {"hook_event_name": "PreToolUse", "session_id": SID, "tool_name": "Bash",
               "tool_input": {"command": "python3 -I ~/.claude/usage-gate/gate.py status"}}
        r = subprocess.run([sys.executable, "-I", GATE, "hook"], input=json.dumps(inp), env=self.env,
                           capture_output=True, text=True)
        cmd = json.loads(r.stdout)["hookSpecificOutput"]["updatedInput"]["command"]
        self.assertTrue(cmd.endswith("status --session " + SID))

    def test_telegram_command_allowed_at_hard_stop(self):
        self.arm()
        self.hook("50,99", "Bash")
        d, _ = self.hook("50,99", "Bash", ti={"command": "python3 -I ~/.claude/usage-gate/gate.py telegram test"})
        self.assertEqual(d, "allow")

    CHAINED = ["ls; python3 -I ~/.claude/usage-gate/gate.py status --session 00000000-0000-4000-8000-000000000000",
               "python3 -I ~/.claude/usage-gate/gate.py status; ls",
               "true && python3 -I ~/.claude/usage-gate/gate.py status",
               "ls | python3 -I ~/.claude/usage-gate/gate.py status",
               "FOO=1 python3 -I ~/.claude/usage-gate/gate.py status",
               "USAGE_GATE_TEST=1 USAGE_GATE_TELEGRAM_API=http://127.0.0.1:1 python3 -I ~/.claude/usage-gate/gate.py telegram poll",
               "echo $(python3 -I ~/.claude/usage-gate/gate.py status)",
               "echo `python3 -I ~/.claude/usage-gate/gate.py status`",
               "python3 -I ~/.claude/usage-gate/gate.py status\nls",
               "python3 -I ~/.claude/usage-gate/gate.py status > /tmp/x"]

    def test_chained_env_prefixed_or_subshell_gate_commands_are_denied_at_hard(self):
        self.arm()
        self.hook("50,99", "Bash")
        for c in self.CHAINED:
            self.assertEqual(self.hook("50,99", "Bash", ti={"command": c})[0], "deny", c)
        self.assertEqual(self.hook("50,99", "Bash", ti={"command": "python3 -I ~/.claude/usage-gate/gate.py status"})[0], "allow")

    def test_chained_gate_commands_are_denied_under_owner_stop(self):
        self.arm()
        p = os.path.join(self.home, ".claude", "usage-gate", "sessions", SID + ".json")
        s = self.sess()
        s["manual"] = "stop"
        with open(p, "w") as f:
            json.dump(s, f)
        for c in self.CHAINED:
            self.assertEqual(self.hook("10,10", "Bash", ti={"command": c})[0], "deny", c)

    def test_usage_gate_env_vars_in_a_bash_command_are_denied_even_when_armed_and_low(self):
        self.arm()
        c = "USAGE_GATE_TEST=1 USAGE_GATE_TELEGRAM_API=http://127.0.0.1:1 python3 -I ~/.claude/usage-gate/gate.py telegram poll"
        self.assertEqual(self.hook("10,10", "Bash", ti={"command": c})[0], "deny")
        self.assertEqual(self.hook("10,10", "Bash", ti={"command": "echo hi"})[0], "allow")

    def test_test_overrides_are_ignored_without_the_test_flag(self):
        env = {k: v for k, v in self.env.items() if k != "USAGE_GATE_TEST"}
        env["USAGE_GATE_API"] = "http://127.0.0.1:1"
        r = subprocess.run([sys.executable, "-I", "-c", "import sys; sys.path.insert(0, %r); import gate; print(gate.API)"
                            % os.path.dirname(GATE)], env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(r.stdout.strip(), "https://api.anthropic.com")

    def test_the_installed_copy_ignores_test_overrides_even_with_the_test_flag(self):
        d = os.path.join(self.home, ".claude", "usage-gate")
        os.makedirs(d)
        shutil.copy(GATE, os.path.join(d, "gate.py"))
        env = dict(self.env, USAGE_GATE_API="http://127.0.0.1:1", USAGE_GATE_NO_KEYCHAIN="1", USAGE_GATE_QUIET="1")
        code = ("import sys; sys.path.insert(0, %r); import gate; print(gate.API, gate.has_keychain() or 'x', "
                "'quiet' if gate.testenv('USAGE_GATE_QUIET') else 'loud')" % d)
        r = subprocess.run([sys.executable, "-I", "-c", code], env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(r.stdout.split()[0], "https://api.anthropic.com")
        self.assertEqual(r.stdout.split()[-1], "loud")

    def test_arm_and_python_form_gate_commands_are_allowed_at_hard(self):
        self.arm()
        self.hook("50,99", "Bash")
        for c in ("python3 -I ~/.claude/usage-gate/gate.py arm --session 00000000-0000-4000-8000-000000000000 --soft 99 --hard 100 --ask 1",
                  "python -I ~/.claude/usage-gate/gate.py status"):
            self.assertEqual(self.hook("50,99", "Bash", ti={"command": c})[0], "allow", c)

    def test_arm_without_account_uuid_finds_email_by_login(self):
        os.makedirs(os.path.join(self.home, ".claude", "usage-gate"), exist_ok=True)
        with open(os.path.join(self.home, ".claude", "usage-gate", "config.json"), "w") as f:
            json.dump({"accounts": {"u1": {"service": "Claude Code-credentials", "email": "a@b.c",
                                           "managed": False}}}, f)
        self.arm()
        self.assertEqual(self.sess()["email"], "a@b.c")

    def pause_session(self, phase="paused_soft", fake="50,82"):
        self.arm()
        self.hook("50,82", "Bash")
        self.cmd("continue", fake="50,82")
        s = self.sess()
        s["phase"] = phase
        with open(os.path.join(self.home, ".claude", "usage-gate", "sessions", SID + ".json"), "w") as f:
            json.dump(s, f)

    def test_paused_session_tells_lead_to_resume_first(self):
        self.pause_session()
        d, r = self.hook("50,82", "Bash")
        self.assertEqual(d, "allow")
        self.assertIn("resume", r)
        for tool in ("Edit", "SendMessage"):
            self.assertEqual(self.hook("50,82", tool)[0], "allow", tool)
        for tool in ("Agent", "mcp__ccd_session__start_session"):
            d, r = self.hook("50,82", tool)
            self.assertEqual(d, "deny", tool)
            self.assertIn("resume", r)

    def test_paused_soft_tells_once_and_says_to_finish_current_work(self):
        self.pause_session()
        d, r = self.hook("50,82", "Bash")
        self.assertEqual(d, "allow")
        self.assertIn("finish", r.lower())
        self.assertIn("resumes by itself", r)
        self.assertEqual(self.hook("50,82", "Bash"), ("allow", ""))  # told once, then silent

    def test_paused_hard_without_phone_stop_blocks_like_a_hard_stop(self):
        self.pause_session(phase="paused_hard", fake="50,96")
        for tool, ti in (("Bash", {"command": "ls"}), ("Edit", {"file_path": "/tmp/x.py"})):
            d, r = self.hook("50,82", tool, ti=ti)
            self.assertEqual(d, "deny", tool)
            self.assertIn("resume", r)
        notes = os.path.join(self.home, ".claude", "usage-gate", "notes", SID)
        self.assertEqual(self.hook("50,82", "Edit", ti={"file_path": os.path.join(notes, "RESUME.md")})[0], "allow")
        self.assertEqual(self.hook("50,82", "SendMessage")[0], "allow")

    def test_paused_soft_past_the_hard_limit_blocks_like_a_hard_stop(self):
        self.pause_session()
        self.assertEqual(self.hook("50,96", "Bash", ti={"command": "ls"})[0], "deny")
        self.assertEqual(self.hook("50,96", "Read")[0], "allow")

    def test_paused_phases_refuse_new_work_to_subagents_too(self):
        for phase in ("paused_soft", "paused_hard"):
            self.pause_session(phase=phase)
            self.assertEqual(self.hook("50,82", "Agent", agent="a1")[0], "deny", phase)
            self.assertEqual(self.hook("50,82", "Read", agent="a1")[0], "allow", phase)

    def test_a_claude_command_in_bash_counts_as_new_work(self):
        self.arm()
        self.hook("50,82", "Bash")
        for c in ("claude -p hi", "cd x && claude --resume", "ls; claude", "echo a|claude"):
            self.assertEqual(self.hook("50,82", "Bash", ti={"command": c})[0], "deny", c)
        for c in ("ls claude-notes", "echo claudette"):
            self.assertEqual(self.hook("50,82", "Bash", ti={"command": c})[0], "allow", c)

    def test_soft_tell_is_repeated_once_on_the_next_post_tool_use(self):
        self.arm()
        d, r = self.hook("50,82", "Bash")
        self.assertIn("ask the user", r)
        d, r = self.hook("50,82", "Read", event="PostToolUse")
        self.assertIn("ask the user", r)
        self.assertEqual(self.hook("50,82", "Read", event="PostToolUse"), ("allow", ""))

    def test_blocked_summary_cuts_secrets(self):
        self.arm()
        self.hook("50,96", "Bash")
        for i, c in enumerate(("curl -H 'Authorization: Bearer abc'", "x sk-ant-123", "mysql --passw=1", "a pwd=zz",
                               "echo SECRET=1")):
            self.hook("50,96", "Bash", ti={"command": "run " + c})
        self.assertEqual(len(self.blocked()), 6)  # the opening call plus the five
        text = "\n".join(self.blocked())
        for leak in ("abc", "123", "=1", "zz"):
            self.assertNotIn(leak, text)

    def test_blocked_md_is_capped_at_200_lines(self):
        self.arm()
        for i in range(205):
            self.hook("50,96", "Bash", ti={"command": "ls %d" % i})
        lines = self.blocked()
        self.assertEqual(len(lines), 200)
        self.assertIn("ls 204", lines[-1])

    def test_a_denied_call_with_non_ascii_text_still_prints_the_decision_under_an_ascii_locale(self):
        self.arm()
        self.env.update(LC_ALL="C", LANG="C", PYTHONUTF8="0", PYTHONIOENCODING="ascii:backslashreplace")
        d, r = self.hook("50,96", "Bash", ti={"command": "echo caf\u00e9 \u4e2d\u6587"})
        self.assertEqual(d, "deny")
        self.assertEqual(len(self.blocked()), 1)

    def test_resume_at_hard_turns_gating_off_until_reset(self):
        self.arm()
        self.hook("50,96", "Bash")
        self.assertIn("off until", self.cmd("resume", fake="50,96"))
        self.assertEqual(self.hook("50,97", "Agent")[0], "allow")
        self.hook("5,3", "Bash")
        self.assertEqual((self.sess()["phase"], self.sess()["override"]), ("run", None))

    def test_wait_exits_when_limits_drop(self):
        self.arm()
        self.cmd("pause", fake="50,82")
        out = self.cmd("wait", fake="5,3")
        self.assertIn("limits are back", out)
        self.assertEqual(self.sess()["phase"], "run")

    def test_usage_error_tells_session_once_and_never_blocks(self):
        self.arm()
        s = self.sess()
        s["service"] = "no-such-login-for-tests"
        with open(os.path.join(self.home, ".claude", "usage-gate", "sessions", SID + ".json"), "w") as f:
            json.dump(s, f)
        env = dict(self.env)
        inp = json.dumps({"hook_event_name": "PostToolUse", "session_id": SID, "tool_name": "Bash", "tool_input": {}})
        r1 = subprocess.run([sys.executable, "-I", GATE, "hook"], input=inp, env=env, capture_output=True, text=True)
        r2 = subprocess.run([sys.executable, "-I", GATE, "hook"], input=inp, env=env, capture_output=True, text=True)
        self.assertIn("cannot read usage", r1.stdout)
        self.assertEqual(r2.stdout.strip(), "")

class FakeTelegram(http.server.BaseHTTPRequestHandler):
    sent, updates, fail, hang, code, gets, calls = [], [], False, 0, 0, [], []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        cls = type(self)
        if cls.hang:
            time.sleep(cls.hang)
        if cls.fail or cls.code:
            self.send_response(cls.code or 500)
            self.end_headers()
            return
        method = self.path.rsplit("/", 1)[-1]
        cls.calls.append((method, body))
        if method == "getUpdates":
            cls.gets.append(body)
            if body.get("offset"):  # Telegram forgets updates below the offset
                cls.updates[:] = [u for u in cls.updates if u.get("update_id", 0) >= body["offset"]]
            allowed = body.get("allowed_updates")  # like Telegram: only the kinds asked for come back
            res = [u for u in cls.updates if allowed is None or any(k in u for k in allowed)]
        else:
            res = {"message_id": 101 + len(cls.sent)}
        if method == "sendMessage":
            cls.sent.append(body)
        data = json.dumps({"ok": True, "result": res}).encode()
        self.send_response(200)
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


class TelegramBase(GateBase):
    def setUp(self):
        super().setUp()
        FakeTelegram.sent, FakeTelegram.updates, FakeTelegram.fail, FakeTelegram.hang = [], [], False, 0
        FakeTelegram.code, FakeTelegram.gets, FakeTelegram.calls = 0, [], []
        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeTelegram)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.shutdown)
        self.env.pop("USAGE_GATE_QUIET")
        self.env.update(USAGE_GATE_NO_OSASCRIPT="1", USAGE_GATE_TELEGRAM_API="http://127.0.0.1:%d" % self.srv.server_port)
        self.conf = os.path.join(self.home, ".claude", "usage-gate", "telegram.json")

    def configure(self, **extra):
        os.makedirs(os.path.dirname(self.conf), exist_ok=True)
        with open(self.conf, "w") as f:
            json.dump(dict(token="TESTTOKEN", chat_id=42, machine="box", **extra), f)

    def wait_sent(self, n=1, t=8):
        end = time.time() + t
        while len(FakeTelegram.sent) < n and time.time() < end:
            time.sleep(0.1)

    def logtext(self):
        with open(os.path.join(self.home, ".claude", "usage-gate", "gate.log")) as f:
            return f.read()

    def setup(self, tok="SECRETTOK\n", *args):
        return subprocess.run([sys.executable, "-I", GATE, "telegram", "setup"] + list(args), input=tok, env=self.env,
                              capture_output=True, text=True, timeout=30)


class TelegramTest(TelegramBase):
    def test_unconfigured_sends_nothing(self):
        self.arm()
        self.hook("50,82", "Bash")
        self.assertEqual(FakeTelegram.sent, [])

    def test_soft_alert_has_prefix_figures_and_age(self):
        self.configure()
        self.arm()
        self.assertEqual(self.hook("50,82", "Bash")[0], "allow")
        self.wait_sent()
        self.assertEqual(len(FakeTelegram.sent), 1)
        m = FakeTelegram.sent[0]
        self.assertEqual(m["chat_id"], 42)
        self.assertTrue(m["text"].startswith("[box] ") or m["text"].startswith("[box \u00b7 "), m["text"])
        self.assertIn("82", m["text"])
        self.assertIn("s old", m["text"])

    def test_alert_names_project_and_session(self):
        self.configure()
        proj = os.path.join(self.home, "myproj")
        os.makedirs(proj)
        subprocess.run([sys.executable, "-I", GATE, "arm", "--ask", "10"], cwd=proj,
                       env=dict(self.env, USAGE_GATE_FAKE="10,10"), capture_output=True, timeout=30)
        self.assertEqual(self.hook("50,82", "Bash")[0], "allow")
        self.wait_sent()
        self.assertIn("myproj #" + SID[:4], FakeTelegram.sent[0]["text"])

    def test_quiet_silences_both(self):
        self.configure()
        self.env["USAGE_GATE_QUIET"] = "1"
        self.arm()
        self.hook("50,82", "Bash")
        self.assertEqual(FakeTelegram.sent, [])

    def test_server_error_does_not_change_decision(self):
        self.configure()
        self.arm()
        FakeTelegram.fail = True
        self.assertEqual(self.hook("50,82", "Bash")[0], "allow")
        self.assertEqual(self.sess()["phase"], "asked")
        time.sleep(1.5)
        log = self.logtext()
        self.assertIn("telegram send failed", log)
        self.assertNotIn("TESTTOKEN", log)

    def test_stale_reading_is_not_sent(self):
        self.configure()
        self.arm()
        self.env["USAGE_GATE_FAKE_AGE"] = "400"
        self.assertEqual(self.hook("50,82", "Bash")[0], "allow")
        self.assertEqual(FakeTelegram.sent, [])
        self.assertIn("telegram skipped", self.logtext())

    def test_setup_reads_token_from_stdin_and_saves_600(self):
        FakeTelegram.updates = [{"message": {"chat": {"id": 77, "type": "private", "first_name": "A"}, "text": "/start"}}]
        r = self.setup()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("SECRETTOK", r.stdout + r.stderr)
        self.assertEqual(oct(os.stat(self.conf).st_mode & 0o777), "0o600")
        with open(self.conf) as f:
            c = json.load(f)
        self.assertEqual((c["token"], c["chat_id"]), ("SECRETTOK", 77))
        self.assertEqual(len(FakeTelegram.sent), 1)

    def test_setup_fixes_existing_0644(self):
        os.makedirs(os.path.dirname(self.conf))
        with open(self.conf, "w") as f:
            f.write("{}")
        os.chmod(self.conf, 0o644)
        FakeTelegram.updates = [{"message": {"chat": {"id": 7, "type": "private", "first_name": "A"}}}]
        self.assertEqual(self.setup().returncode, 0)
        self.assertEqual(oct(os.stat(self.conf).st_mode & 0o777), "0o600")

    def test_setup_ignores_groups_and_dedupes(self):
        FakeTelegram.updates = [
            {"message": {"chat": {"id": -5, "type": "group", "title": "G"}}},
            {"message": {"chat": {"id": 9, "type": "private", "first_name": "Zed"}}},
            {"message": {"chat": {"id": 9, "type": "private", "first_name": "Zed"}}}]
        r = self.setup()
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(self.conf) as f:
            self.assertEqual(json.load(f)["chat_id"], 9)

    def test_setup_several_private_chats_needs_chat_flag(self):
        FakeTelegram.updates = [
            {"message": {"chat": {"id": 9, "type": "private", "first_name": "Zed"}}},
            {"message": {"chat": {"id": 10, "type": "private", "first_name": "Amy"}}},
            {"message": {"chat": {"id": -5, "type": "group", "title": "G"}}}]
        r = self.setup()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("--chat", r.stdout + r.stderr)
        self.assertIn("Amy", r.stdout + r.stderr)
        self.assertFalse(os.path.exists(self.conf))
        r = self.setup("SECRETTOK\n", "--chat", "10")
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(self.conf) as f:
            self.assertEqual(json.load(f)["chat_id"], 10)

    def test_setup_only_group_says_send_start(self):
        FakeTelegram.updates = [{"message": {"chat": {"id": -5, "type": "group", "title": "G"}}}]
        r = self.setup()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("/start", r.stdout + r.stderr)
        self.assertFalse(os.path.exists(self.conf))

    def test_hook_returns_fast_when_telegram_hangs(self):
        self.configure()
        self.arm()
        FakeTelegram.hang = 30
        t = time.time()
        self.assertEqual(self.hook("50,82", "Bash")[0], "allow")
        self.assertLess(time.time() - t, 2)

    def test_token_is_scrubbed_from_error_log(self):
        os.makedirs(os.path.dirname(self.conf), exist_ok=True)
        with open(self.conf, "w") as f:
            json.dump(dict(token="BAD TOKEN", chat_id=1), f)
        self.arm()
        self.hook("50,82", "Bash")
        end = time.time() + 8
        while "telegram send failed" not in (self.logtext() if True else "") and time.time() < end:
            time.sleep(0.2)
        log = self.logtext()
        self.assertIn("telegram send failed", log)
        self.assertNotIn("BAD TOKEN", log)
        self.assertIn("<token>", log)

    def test_second_hook_call_in_same_phase_sends_nothing(self):
        self.configure()
        self.arm()
        self.hook("50,82", "Bash")
        self.wait_sent()
        self.hook("50,82", "Bash")
        time.sleep(1)
        self.assertEqual(len(FakeTelegram.sent), 1)

    def test_telegram_test_command(self):
        self.configure()
        r = subprocess.run([sys.executable, "-I", GATE, "telegram", "test"], env=self.env,
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(FakeTelegram.sent), 1)


SID2 = "test-sess-2"


class CommandTest(TelegramBase):
    """Telegram commands (poll, addressing, wake-up loops). Poll rate limit is off unless a test turns it on."""
    def setUp(self):
        super().setUp()
        self.env["USAGE_GATE_POLL_INTERVAL"] = "0"
        self.env["USAGE_GATE_TICK"] = "0.2"
        self.configure()
        self.uid = 1000

    def upd(self, text, chat=42, reply_to=None, age=0):
        self.uid += 1
        m = {"message_id": self.uid, "chat": {"id": chat, "type": "private"}, "date": int(time.time() - age), "text": text}
        if reply_to:
            m["reply_to_message"] = {"message_id": reply_to}
        FakeTelegram.updates.append({"update_id": self.uid, "message": m})

    def run_gate(self, *args, env=None, fake="10,10", timeout=30):
        e = dict(env or self.env, USAGE_GATE_FAKE=fake)
        return subprocess.run([sys.executable, "-I", GATE] + list(args), env=e, capture_output=True, text=True,
                              timeout=timeout)

    def poll_once(self, env=None, fake="10,10"):
        r = self.run_gate("telegram", "poll", env=env, fake=fake)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def unhandled(self, env=None):
        """Fresh updates from the owner's chat that this machine has not handled yet."""
        home = (env or self.env)["HOME"]
        try:
            with open(os.path.join(home, ".claude", "usage-gate", "telegram-last.json")) as f:
                last = json.load(f).get("last_update_id", 0)
        except (OSError, ValueError):
            last = 0
        def mine(u):
            m = u.get("message")
            return u.get("callback_query") is not None or (m and m.get("chat", {}).get("id") == 42
                                                          and time.time() - m.get("date", 0) < 300)
        return [u for u in FakeTelegram.updates if u["update_id"] > last and mine(u)]

    def poll_until(self, done=None, env=None, fake="10,10", t=20):
        """A hook's background poll child may hold the poll lock, so one poll can do nothing: retry until `done()`
        holds (default: every fresh update from the owner has been handled) or the deadline passes.
        Returns whether `done()` held."""
        return self.poll_loop(done, env, fake, t)[1]

    def poll_loop(self, done, env, fake, t):
        done = done or (lambda: not self.unhandled(env))
        end = time.time() + t
        while True:
            out = self.poll_once(env=env, fake=fake)
            ok = bool(done())
            if ok or time.time() >= end:
                return out, ok
            time.sleep(0.2)

    def poll(self, env=None, fake="10,10"):
        return self.poll_loop(None, env, fake, 20)[0]

    def texts(self):
        return [m["text"] for m in FakeTelegram.sent]

    def other_machine(self, name="box2", sid=SID2):
        home = tempfile.mkdtemp()
        env = dict(self.env, HOME=home, CLAUDE_CODE_SESSION_ID=sid)
        conf = os.path.join(home, ".claude", "usage-gate", "telegram.json")
        os.makedirs(os.path.dirname(conf))
        with open(conf, "w") as f:
            json.dump(dict(token="TESTTOKEN", chat_id=42, machine=name), f)
        self.run_gate("arm", env=env)
        return env, home

    def arm_second_session(self):
        self.run_gate("arm", env=dict(self.env, CLAUDE_CODE_SESSION_ID=SID2))

    def sess_of(self, sid, home=None):
        with open(os.path.join(home or self.home, ".claude", "usage-gate", "sessions", sid + ".json")) as f:
            return json.load(f)

    def test_poll_helper_retries_while_a_poll_child_holds_the_poll_lock(self):
        import fcntl
        self.arm()
        self.upd("/pause")
        path = os.path.join(self.home, ".claude", "usage-gate", "telegram-poll.lock")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        f = open(path, "a+")
        fcntl.flock(f, fcntl.LOCK_EX)
        threading.Timer(1.5, lambda: (fcntl.flock(f, fcntl.LOCK_UN), f.close())).start()
        self.poll()
        self.assertEqual(self.sess()["phase"], "packing")

    def test_status_replies_with_prefix_and_phase(self):
        self.arm()
        self.upd("/status")
        self.poll()
        self.assertEqual(len(FakeTelegram.sent), 1)
        t = FakeTelegram.sent[0]["text"]
        self.assertTrue(t.startswith("[box"), t)
        self.assertIn("run", t)
        self.assertIn("5-hour 10%", t)

    def test_stop_is_a_hard_stop_even_at_low_usage(self):
        self.arm()
        self.upd("/stop")
        self.poll()
        self.assertIn("hard", self.texts()[0].lower())
        d, r = self.hook("10,10", "Bash")
        self.assertEqual(d, "deny")
        self.assertIn("HARD STOP", r)
        self.assertEqual(self.hook("10,10", "Read")[0], "allow")
        self.assertEqual(self.hook("10,10", "Agent", agent="a1")[0], "deny")

    def test_pause_packs_up_now(self):
        self.arm()
        self.upd("/pause")
        self.poll()
        self.assertEqual(self.sess()["phase"], "packing")
        d, r = self.hook("10,10", "Bash")
        self.assertEqual(d, "deny")
        self.assertIn("RESUME.md", r)
        self.assertIn("pack", self.texts()[0].lower())

    def test_continue_after_soft_question(self):
        self.arm()
        self.hook("50,82", "Bash")
        self.upd("/continue")
        end = time.time() + 20  # a background poll child may hold the poll lock under load: poll until it lands
        while time.time() < end:
            self.poll(fake="50,82")
            if self.sess()["phase"] == "run":
                break
            time.sleep(0.2)
        self.assertEqual((self.sess()["phase"], self.sess()["override"]), ("run", "soft"))
        self.assertEqual(self.hook("50,82", "Agent")[0], "allow")

    def test_resume_clears_pause_and_stop(self):
        self.arm()
        self.upd("/stop")
        self.poll()
        self.upd("/resume")
        self.poll()
        s = self.sess()
        self.assertEqual((s["phase"], s.get("manual")), ("run", None))
        self.assertEqual(self.hook("10,10", "Agent")[0], "allow")

    def test_off_disarms(self):
        self.arm()
        self.upd("/off")
        self.poll()
        self.assertFalse(self.sess()["armed"])
        self.assertEqual(self.hook("99,99", "Bash")[0], "allow")

    def test_help_lists_commands(self):
        self.arm()
        self.upd("/help")
        self.poll()
        for w in ("/status", "/stop", "/pause", "/continue", "/resume", "/off"):
            self.assertIn(w, self.texts()[0])

    def test_reply_addresses_one_session(self):
        self.arm()
        self.arm_second_session()
        self.hook("50,82", "Bash")           # alert for SID goes out as message 101
        self.wait_sent()
        self.upd("/stop", reply_to=101)
        self.poll()
        self.assertEqual(self.sess_of(SID).get("manual"), "stop")
        self.assertIsNone(self.sess_of(SID2).get("manual"))

    def test_machine_name_addresses_that_machine_only(self):
        self.arm()
        env2, home2 = self.other_machine()
        self.upd("/stop box2")
        self.poll()
        self.poll(env=env2)
        self.assertIsNone(self.sess().get("manual"))
        self.assertEqual(self.sess_of(SID2, home2).get("manual"), "stop")

    def test_bare_command_reaches_every_armed_session_on_every_machine(self):
        self.arm()
        self.arm_second_session()
        env2, home2 = self.other_machine()
        self.upd("/off")
        self.poll()
        self.poll(env=env2)
        self.assertFalse(self.sess_of(SID)["armed"])
        self.assertFalse(self.sess_of(SID2)["armed"])
        self.assertFalse(self.sess_of(SID2, home2)["armed"])
        self.assertEqual(len(FakeTelegram.sent), 3)

    def test_foreign_chat_ignored(self):
        self.arm()
        self.upd("/off", chat=999)
        self.poll()
        self.assertTrue(self.sess()["armed"])
        self.assertEqual(FakeTelegram.sent, [])

    def test_old_message_ignored_and_acknowledged(self):
        self.arm()
        self.upd("/off", age=700)
        self.poll()
        self.assertTrue(self.sess()["armed"])
        self.assertEqual(FakeTelegram.updates, [])        # acknowledged so the queue stays short

    def test_fresh_updates_are_not_acknowledged_for_other_machines(self):
        self.arm()
        self.upd("/status")
        self.poll()
        self.assertEqual(len(FakeTelegram.updates), 1)
        self.assertFalse(any(g.get("offset") for g in FakeTelegram.gets))

    def test_command_applied_once(self):
        self.arm()
        self.upd("/pause")
        self.poll()
        self.run_gate("resume")
        self.poll()                                       # same update still in the queue
        self.assertEqual(self.sess()["phase"], "run")

    def test_bot_suffix_and_case(self):
        self.arm()
        self.upd("/Off@my_bot")
        self.poll()
        self.assertFalse(self.sess()["armed"])

    def test_poll_rate_limit(self):
        self.arm()
        self.env["USAGE_GATE_POLL_INTERVAL"] = "15"
        self.poll()
        self.poll()
        self.assertEqual(len(FakeTelegram.gets), 1)

    def test_conflict_409_backs_off(self):
        self.arm()
        FakeTelegram.code = 409
        self.env["USAGE_GATE_POLL_INTERVAL"] = "1"
        self.poll()
        FakeTelegram.code = 0
        self.upd("/off")
        time.sleep(1.5)                                   # past the normal interval, inside the back-off
        self.poll()
        self.assertEqual(FakeTelegram.gets, [])
        self.assertTrue(self.sess()["armed"])

    def test_unarmed_machine_stays_silent_on_commands(self):
        self.upd("/stop")
        self.poll()
        self.assertEqual(FakeTelegram.sent, [])

    def test_hook_starts_a_poll_that_applies_commands(self):
        self.arm()
        self.upd("/stop")
        self.assertEqual(self.hook("10,10", "Bash")[0], "allow")   # command not applied yet
        end = time.time() + 8
        while time.time() < end and self.sess().get("manual") != "stop":
            time.sleep(0.2)
        self.assertEqual(self.sess().get("manual"), "stop")
        self.assertEqual(self.hook("10,10", "Bash")[0], "deny")

    def test_hook_fast_with_hanging_server_when_polling(self):
        self.arm()
        FakeTelegram.hang = 30
        t = time.time()
        self.hook("10,10", "Bash")
        self.assertLess(time.time() - t, 2)

    def test_hook_does_not_poll_when_unarmed(self):
        self.hook("10,10", "Bash")
        time.sleep(1)
        self.assertEqual(FakeTelegram.gets, [])

    def spawn(self, *args, fake="50,82"):
        return subprocess.Popen([sys.executable, "-I", GATE] + list(args), env=dict(self.env, USAGE_GATE_FAKE=fake),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def finish(self, p, t=15):
        try:
            out, err = p.communicate(timeout=t)
        except subprocess.TimeoutExpired:
            p.kill()
            self.fail("background loop did not exit")
        return out

    def test_timer_ends_on_continue_and_pause(self):
        for name in ("/continue", "/pause"):
            self.arm()
            self.hook("50,82", "Bash")
            p = self.spawn("timer")
            time.sleep(0.5)
            self.upd(name)
            out = self.finish(p)
            self.assertIn(name, out)
            self.assertIn("owner", out)

    def test_wait_ends_on_resume_and_continue(self):
        for name in ("/resume", "/continue"):
            self.arm()
            self.run_gate("pause", fake="50,82")
            p = self.spawn("wait")
            time.sleep(0.5)
            self.upd(name)
            out = self.finish(p)
            self.assertIn(name, out)
            self.assertEqual(self.sess()["phase"], "run")

    def test_wait_ignores_pause_and_holds_a_stop(self):
        self.arm()
        self.upd("/stop")
        self.poll(fake="5,3")
        p = self.spawn("wait", fake="5,3")
        time.sleep(2)
        self.assertIsNone(p.poll())                       # limits are low but the owner stopped it
        self.upd("/resume")
        out = self.finish(p)
        self.assertIn("/resume", out)

    def test_listen_exits_on_continue_with_instruction(self):
        self.arm()
        p = self.spawn("listen", fake="10,10")
        time.sleep(0.5)
        self.upd("/status")
        time.sleep(1)
        self.assertIsNone(p.poll())                       # /status does not wake the session
        self.upd("/continue")
        out = self.finish(p)
        self.assertIn("/continue", out)

    def test_listen_exits_on_pause_with_packing_message(self):
        self.arm()
        p = self.spawn("listen", fake="10,10")
        time.sleep(0.5)
        self.upd("/pause")
        self.assertIn("RESUME.md", self.finish(p))

    def test_listen_exits_on_stop_with_hard_stop_message(self):
        self.arm()
        p = self.spawn("listen", fake="10,10")
        time.sleep(0.5)
        self.upd("/stop")
        self.assertIn("HARD STOP", self.finish(p))

    def test_new_listen_ends_the_old_one(self):
        self.arm()
        p1 = self.spawn("listen", fake="10,10")
        time.sleep(1)
        p2 = self.spawn("listen", fake="10,10")
        out1 = self.finish(p1)
        self.assertIn("newer", out1)
        self.assertIsNone(p2.poll())
        self.upd("/off")
        self.assertIn("off", self.finish(p2).lower())

    def test_stop_applied_during_slow_hook_survives(self):
        self.arm()
        env = dict(self.env, USAGE_GATE_FAKE="50,82", USAGE_GATE_FAKE_DELAY="3")
        inp = json.dumps({"hook_event_name": "PreToolUse", "session_id": SID, "tool_name": "Bash", "tool_input": {}})
        p = subprocess.Popen([sys.executable, "-I", GATE, "hook"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             env=env, text=True)
        p.stdin.write(inp)
        p.stdin.close()
        time.sleep(1)
        self.upd("/stop")
        self.poll(fake="50,82")
        p.wait(timeout=15)
        self.assertEqual(self.sess().get("manual"), "stop")

    def test_continue_slow_does_not_undo_concurrent_command(self):
        self.arm()
        self.hook("50,82", "Bash")
        env = dict(self.env, USAGE_GATE_FAKE="50,82", USAGE_GATE_FAKE_DELAY="2")
        p = subprocess.Popen([sys.executable, "-I", GATE, "continue"], stdout=subprocess.PIPE, env=env, text=True)
        time.sleep(0.7)
        self.upd("/off")
        self.poll(fake="50,82")
        p.wait(timeout=15)
        self.assertFalse(self.sess()["armed"])

    def test_session_armed_after_a_bare_command_ignores_it(self):
        self.upd("/off", age=10)
        time.sleep(1.2)
        self.arm()
        self.poll()
        self.assertTrue(self.sess()["armed"])
        self.assertEqual(FakeTelegram.sent, [])

    def test_burst_of_hooks_spawns_at_most_one_poll(self):
        self.arm()
        self.env["USAGE_GATE_POLL_INTERVAL"] = "15"
        counter = os.path.join(self.home, "spawns")
        open(counter, "w").close()
        self.env["USAGE_GATE_SPAWN_COUNT"] = counter
        inp = json.dumps({"hook_event_name": "PreToolUse", "session_id": SID, "tool_name": "Bash", "tool_input": {}})
        ps = [subprocess.Popen([sys.executable, "-I", GATE, "hook"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               env=dict(self.env, USAGE_GATE_FAKE="10,10"), text=True) for _ in range(6)]
        for p in ps:
            p.communicate(inp, timeout=20)
        time.sleep(1)
        with open(counter) as f:
            self.assertLessEqual(len(f.read().split()), 1)

    def test_wait_after_phone_stop_says_resume_not_usage(self):
        self.configure()
        self.arm()
        p = os.path.join(self.home, ".claude", "usage-gate", "sessions", SID + ".json")
        d = json.load(open(p))
        d.update(manual="stop", phase="hard")
        json.dump(d, open(p, "w"))
        w = subprocess.Popen([sys.executable, "-I", GATE, "wait", "--session", SID],
                             env=dict(self.env, USAGE_GATE_FAKE="10,10"), stdout=subprocess.DEVNULL)
        self.wait_sent()
        w.kill()
        txt = FakeTelegram.sent[0]["text"]
        self.assertIn("/resume", txt)
        self.assertNotIn("drop below", txt)

    def test_model_cannot_undo_phone_stop(self):
        self.arm()
        self.upd("/stop")
        self.poll()
        for c in ("continue", "resume", "disarm", "arm"):
            d, r = self.hook("10,10", "Bash", ti={"command": "python3 -I ~/.claude/usage-gate/gate.py " + c})
            self.assertEqual(d, "deny", c)
            self.assertIn("owner", r)
        self.assertEqual(self.hook("10,10", "Bash", ti={"command": "python3 -I ~/.claude/usage-gate/gate.py wait --session " + SID})[0], "allow")
        self.assertEqual(self.sess().get("manual"), "stop")
        for c in ("python3 -I ~/.claude/usage-gate/gate.py status; python3 ~/.claude/usage-gate/gate.py resume",
                  "cd ~/.claude/usage-gate && python3 gate.py resume",
                  "cd ~/.claude/usage-gate && python3 gate.py disarm"):
            self.assertEqual(self.hook("10,10", "Bash", ti={"command": c})[0], "deny", c)
        s = self.sess()
        s["phase"] = "paused_hard"
        with open(os.path.join(self.home, ".claude", "usage-gate", "sessions", SID + ".json"), "w") as f:
            json.dump(s, f)
        d, r = self.hook("10,10", "Bash")
        self.assertEqual(d, "deny")
        self.assertNotIn("gate.py resume", r)
        self.upd("/resume")
        self.poll()
        d, _ = self.hook("10,10", "Bash", ti={"command": "python3 -I ~/.claude/usage-gate/gate.py continue"})
        self.assertEqual(d, "allow")

    def test_session_files_only_json_are_read(self):
        self.arm()
        sd = os.path.join(self.home, ".claude", "usage-gate", "sessions")
        with open(os.path.join(sd, SID + ".json.123.tmp"), "w") as f:
            f.write(open(os.path.join(sd, SID + ".json")).read().replace(SID, "ghost"))
        self.upd("/status")
        self.poll()
        self.assertEqual(len(FakeTelegram.sent), 1)

    def test_bare_help_and_status_from_idle_machine_stay_silent(self):
        self.upd("/help")
        self.upd("/status")
        self.poll()
        self.assertEqual(FakeTelegram.sent, [])
        self.upd("/status " + "box")
        self.poll()
        self.assertEqual(len(FakeTelegram.sent), 1)

    def test_listen_after_disarm_by_cli_prints_something(self):
        self.arm()
        p = self.spawn("listen", fake="10,10")
        time.sleep(0.5)
        self.run_gate("disarm")
        self.assertIn("isarmed", self.finish(p))

    def test_listen_without_telegram_says_so(self):
        os.remove(self.conf)
        self.arm()
        self.assertIn("not set up", self.run_gate("listen", fake="10,10").stdout)


# ---------- Linux / Windows port (D10, D12) ----------
def load_gate(home, platform=None):
    """Import gate.py as a fresh module with HOME (and USERPROFILE) pointing at `home`."""
    import importlib.util
    old = {k: os.environ.get(k) for k in ("HOME", "USERPROFILE", "USAGE_GATE_PLATFORM")}
    os.environ["HOME"] = os.environ["USERPROFILE"] = home
    if platform:
        os.environ["USAGE_GATE_PLATFORM"] = platform
    else:
        os.environ.pop("USAGE_GATE_PLATFORM", None)
    try:
        spec = importlib.util.spec_from_file_location("gate_under_test", GATE)
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        return m
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class FakeApi(http.server.BaseHTTPRequestHandler):
    auth, paths, code, five, retry_after, delay = [], [], 200, 31, None, 0

    def do_GET(self):
        cls = type(self)
        cls.auth.append(self.headers.get("Authorization"))
        cls.paths.append(self.path)
        if cls.delay:
            time.sleep(cls.delay)
        if cls.code != 200:
            self.send_response(cls.code)
            if cls.retry_after:
                self.send_header("Retry-After", str(cls.retry_after))
            self.end_headers()
            return
        data = json.dumps({"five_hour": {"utilization": cls.five, "resets_at": None},
                           "seven_day": {"utilization": 42, "resets_at": None}}).encode()
        self.send_response(200)
        self.end_headers()
        self.wfile.write(data)

    do_POST = do_GET

    def log_message(self, *a):
        pass


class CredentialsFileTest(GateBase):
    """No keychain: the login is read from ~/.claude/.credentials.json, read-only (D12)."""

    def setUp(self):
        super().setUp()
        FakeApi.auth, FakeApi.paths, FakeApi.code = [], [], 200
        self.srv = http.server.HTTPServer(("127.0.0.1", 0), FakeApi)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.shutdown)
        self.env.update(USAGE_GATE_NO_KEYCHAIN="1", USAGE_GATE_API="http://127.0.0.1:%d" % self.srv.server_port)
        self.cred = os.path.join(self.home, ".claude", ".credentials.json")

    def write_cred(self, expires_ms):
        os.makedirs(os.path.dirname(self.cred), exist_ok=True)
        with open(self.cred, "w") as f:
            json.dump({"claudeAiOauth": {"accessToken": "fake-access-tok", "refreshToken": "fake-refresh-tok",
                                         "expiresAt": expires_ms}}, f)
        os.utime(self.cred, (1000000000, 1000000000))
        with open(self.cred, "rb") as f:
            self.before = (f.read(), os.stat(self.cred).st_mtime)

    def unchanged(self):
        with open(self.cred, "rb") as f:
            self.assertEqual((f.read(), os.stat(self.cred).st_mtime), self.before)

    def run_nofake(self, *args):
        return subprocess.run([sys.executable, "-I", GATE] + list(args), env=self.env,
                              capture_output=True, text=True, timeout=30)

    def test_reads_usage_with_the_file_token_and_never_writes_it(self):
        self.write_cred(int((time.time() + 3600) * 1000))
        r = self.run_nofake("arm")
        self.assertIn("5-hour 31%", r.stdout)
        self.assertEqual(FakeApi.auth, ["Bearer fake-access-tok"])
        self.unchanged()

    def test_expired_token_is_not_refreshed(self):
        self.write_cred(int((time.time() - 3600) * 1000))
        FakeApi.code = 401
        r = self.run_nofake("arm")
        self.assertIn("login expired", r.stdout)
        self.assertEqual(FakeApi.paths, [])  # expired an hour ago: no token endpoint call and no usage read
        self.unchanged()

    def test_missing_file_is_a_clear_no_login_status(self):
        r = self.run_nofake("arm")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("no login", r.stdout)
        self.assertNotIn("Traceback", r.stderr)
        self.assertFalse(os.path.exists(self.cred))
        r = self.run_nofake("status")
        self.assertIn("no login", r.stdout)

    def test_hook_still_denies_at_hard_when_the_cache_cannot_be_written(self):
        self.write_cred(int((time.time() + 3600) * 1000))
        self.run_nofake("arm")
        FakeApi.five = 97
        self.addCleanup(setattr, FakeApi, "five", 31)
        cache = os.path.join(self.home, ".claude", "usage-gate", "cache")
        shutil.rmtree(cache)
        open(cache, "w").close()  # a file where the cache folder should be: every cache write raises OSError
        inp = json.dumps({"hook_event_name": "PreToolUse", "session_id": SID, "tool_name": "Agent", "tool_input": {}})
        r = subprocess.run([sys.executable, "-I", GATE, "hook"], input=inp, env=self.env, capture_output=True,
                           text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_accounts_without_keychain_says_so_in_one_line(self):
        r = self.run_nofake("accounts")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(r.stdout.strip().splitlines()), 1)
        self.assertIn("no keychain", r.stdout)



class RateLimitTest(GateBase):
    """HTTP 429 and other transient errors must not erase the last good reading (live 2026-11-21)."""
    write_cred = CredentialsFileTest.write_cred
    run_nofake = CredentialsFileTest.run_nofake

    def setUp(self):
        GateBase.setUp(self)
        FakeApi.auth, FakeApi.paths, FakeApi.code = [], [], 200
        self.srv = http.server.HTTPServer(("127.0.0.1", 0), FakeApi)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.shutdown)
        self.env.update(USAGE_GATE_NO_KEYCHAIN="1", USAGE_GATE_API="http://127.0.0.1:%d" % self.srv.server_port)
        self.cred = os.path.join(self.home, ".claude", ".credentials.json")
        FakeApi.retry_after, FakeApi.five = None, 31
        self.addCleanup(setattr, FakeApi, "retry_after", None)
        self.addCleanup(setattr, FakeApi, "five", 31)
        self.write_cred(int((time.time() + 3600) * 1000))

    def cache_file(self):
        d = os.path.join(self.home, ".claude", "usage-gate", "cache")
        return os.path.join(d, [n for n in os.listdir(d) if n.startswith("usage-") and n.endswith(".json")][0])

    def edit_cache(self, fn):
        with open(self.cache_file()) as f:
            c = json.load(f)
        fn(c)
        with open(self.cache_file(), "w") as f:
            json.dump(c, f)

    def age(self, sec):
        def f(c):
            g = c.get("good") or c  # new format, or main's bare reading
            g["ts"] -= sec
            if "err_since" in c:
                c["err_since"] -= sec
        self.edit_cache(f)

    def nofake_hook(self, tool="Agent", event="PreToolUse", sid=SID):
        inp = json.dumps({"hook_event_name": event, "session_id": sid, "tool_name": tool, "tool_input": {}})
        r = subprocess.run([sys.executable, "-I", GATE, "hook"], input=inp,
                           env=dict(self.env, CLAUDE_CODE_SESSION_ID=sid), capture_output=True,
                           text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def log_text(self):
        with open(os.path.join(self.home, ".claude", "usage-gate", "gate.log")) as f:
            return f.read()

    def test_continue_while_usage_unreadable_holds_until_hard(self):
        """Live 2026-11-21: /continue during a 429 left override None, so the soft ask came straight back."""
        FakeApi.five = 85
        self.run_nofake("arm")
        out = self.nofake_hook()
        self.assertEqual(json.loads(out)["hookSpecificOutput"]["permissionDecision"], "deny")
        FakeApi.code = 429
        self.age(1000)  # no good reading left: usage is unreadable
        r = self.run_nofake("continue")
        self.assertIn("95", r.stdout)
        FakeApi.code = 200
        self.edit_cache(lambda c: [c.pop(k, None) for k in ("retry_after_until", "backoff")])
        self.age(1000)
        out = self.nofake_hook()
        self.assertFalse(out.strip() and json.loads(out)["hookSpecificOutput"].get("permissionDecision") == "deny", out)

    def test_429_keeps_the_old_reading_and_still_denies_at_hard(self):
        FakeApi.five = 97
        self.run_nofake("arm")
        FakeApi.code = 429
        self.age(300)  # older than HOOK_TTL, younger than STALE_OK
        out = self.nofake_hook()
        self.assertEqual(json.loads(out)["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertNotIn("Can't read usage", self.log_text())

    def test_retry_after_stops_fetches_across_processes_until_it_passes(self):
        self.run_nofake("arm")
        self.age(300)
        FakeApi.code, FakeApi.retry_after = 429, 300
        n0 = len(FakeApi.paths)
        self.nofake_hook()
        self.assertEqual(len(FakeApi.paths), n0 + 1)
        self.nofake_hook()
        self.nofake_hook("Bash", "PostToolUse")
        self.assertEqual(len(FakeApi.paths), n0 + 1)  # later hook processes skip the fetch
        FakeApi.code = 200
        self.nofake_hook()
        self.assertEqual(len(FakeApi.paths), n0 + 1)
        self.edit_cache(lambda c: c.update(retry_after_until=time.time() - 1))
        self.nofake_hook()
        self.assertEqual(len(FakeApi.paths), n0 + 2)
        with open(self.cache_file()) as f:
            self.assertFalse(json.load(f).get("retry_after_until"))  # reset on success

    def test_429_without_retry_after_backs_off_from_60_seconds(self):
        self.run_nofake("arm")
        self.age(300)
        FakeApi.code = 429
        self.nofake_hook()
        with open(self.cache_file()) as f:
            wait = json.load(f)["retry_after_until"] - time.time()
        self.assertTrue(50 < wait <= 60, wait)

    def test_401_still_says_log_in_again(self):
        self.run_nofake("arm")
        self.age(300)
        FakeApi.code = 401
        out = self.nofake_hook("Bash", "PostToolUse")
        self.assertIn("log in again", out)
        self.assertIn("Log in again.", self.log_text())

    def test_expired_cli_token_waits_for_the_cli_without_calling_the_api(self):
        """Live 2026-11-21 22:28: an expired CLI token drew 401s ("Log in again", wrong) and then a 429."""
        FakeApi.five = 97
        self.run_nofake("arm")
        self.age(300)
        self.write_cred(int((time.time() - 60) * 1000))
        n0 = len(FakeApi.paths)
        out = self.nofake_hook("Bash", "PostToolUse")
        self.assertNotIn("log in again", out.lower())
        out = self.nofake_hook()
        self.assertEqual(json.loads(out)["hookSpecificOutput"]["permissionDecision"], "deny")  # stale reading gates
        self.assertEqual(len(FakeApi.paths), n0)
        self.assertNotIn("Log in again", self.log_text())

    def test_cli_token_expired_beyond_30_min_says_log_in_again(self):
        self.run_nofake("arm")
        self.age(300)
        self.write_cred(int((time.time() - 31 * 60) * 1000))
        n0 = len(FakeApi.paths)
        out = self.nofake_hook("Bash", "PostToolUse")
        self.assertIn("log in again", out)
        self.assertEqual(len(FakeApi.paths), n0)

    def test_waiting_for_refresh_beyond_15_min_says_so(self):
        self.run_nofake("arm")
        self.age(20 * 60)
        self.write_cred(int((time.time() - 5 * 60) * 1000))
        self.nofake_hook("Bash", "PostToolUse")
        log = self.log_text()
        self.assertIn("for 20 min (waiting for Claude Code to refresh its login); gating paused", log)
        self.assertNotIn("Log in again", log)

    def test_stale_beyond_15_min_notifies_once_with_the_new_wording(self):
        self.run_nofake("arm")
        self.age(20 * 60)
        FakeApi.code = 429
        FakeApi.retry_after = 120
        self.nofake_hook("Bash", "PostToolUse")
        self.nofake_hook("Bash", "PostToolUse")
        log = self.log_text()
        self.assertEqual(log.count("Can't read usage for"), 1)
        self.assertIn("for 20 min (rate-limited); gating paused until it reads again", log)
        self.assertNotIn("Log in again", log)

    def py(self, code):
        """Run `code` in a fresh process with gate.py importable (isolated HOME); returns its stdout."""
        pre = "import sys; sys.path.insert(0, %r); import gate, json, os, time, hashlib, urllib.error\n" % os.path.dirname(GATE)
        env = {k: v for k, v in self.env.items() if k != "USAGE_GATE_FAKE"}
        r = subprocess.run([sys.executable, "-I", "-c", pre + code], env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def set_phase(self, phase):
        p = os.path.join(self.home, ".claude", "usage-gate", "sessions", SID + ".json")
        with open(p) as f:
            s = json.load(f)
        s["phase"] = phase
        with open(p, "w") as f:
            json.dump(s, f)

    def test_network_error_after_a_429_is_not_refetched_by_every_hook(self):
        self.run_nofake("arm")
        self.age(300)
        FakeApi.code, FakeApi.retry_after = 429, 10
        self.nofake_hook()
        self.edit_cache(lambda c: c.update(retry_after_until=time.time() - 1))
        FakeApi.code, FakeApi.retry_after = 500, None
        n0 = len(FakeApi.paths)
        for _ in range(3):
            self.nofake_hook()
        self.assertEqual(len(FakeApi.paths), n0 + 1)  # the error is reused for the TTL, not refetched each time

    def test_an_older_reading_never_overwrites_a_newer_one(self):
        out = self.py("""
svc = 'svc-x'
path = os.path.join(gate.CACHE, 'usage-' + hashlib.sha1(svc.encode()).hexdigest()[:10] + '.json')
os.makedirs(gate.CACHE, exist_ok=True)
new = {'ts': time.time() - 5, 'five_hour': {'pct': 97.0, 'resets_at': None}, 'seven_day': {'pct': 1.0, 'resets_at': None}}
json.dump({'good': new}, open(path, 'w'))
old = {'ts': time.time() - 60, 'five_hour': {'pct': 10.0, 'resets_at': None}, 'seven_day': {'pct': 1.0, 'resets_at': None}}
gate.fetch_usage = lambda s: old
u = gate.usage(svc, 0)
c = json.load(open(path))
print(u['five_hour']['pct'], (c.get('good') or c)['five_hour']['pct'])
""")
        self.assertEqual(out.split(), ["97.0", "97.0"])

    def test_hard_stop_is_not_lifted_by_a_stale_reading(self):
        self.run_nofake("arm")
        self.set_phase("hard")
        self.age(300)
        FakeApi.code = 429
        out = self.nofake_hook()
        self.assertEqual(json.loads(out)["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual(self.sess()["phase"], "hard")

    def test_wait_does_not_resume_on_a_stale_reading(self):
        self.run_nofake("arm")
        self.set_phase("hard")
        self.age(300)
        FakeApi.code = 429
        env = dict(self.env, USAGE_GATE_TICK="0.2")
        with self.assertRaises(subprocess.TimeoutExpired):
            subprocess.run([sys.executable, "-I", GATE, "wait"], env=env, capture_output=True, text=True, timeout=5)
        self.assertTrue(self.sess()["phase"].startswith("paused"))

    def test_rejected_refresh_is_a_login_problem(self):
        out = self.py("""
def fake_http(url, token=None, body=None):
    raise urllib.error.HTTPError(url, 400, 'invalid_grant', {}, None)
gate.http = fake_http
gate.has_keychain = lambda: True
gate.managed = lambda svc: True
gate.kc_write = lambda svc, data: None
gate.kc_read = lambda svc: {'claudeAiOauth': {'accessToken': 'a', 'refreshToken': 'r', 'expiresAt': 0}}
u = gate.usage('svc-y', 0)
print(bool(u.get('login')), bool(u.get('transient')))
""")
        self.assertEqual(out.split(), ["True", "False"])

    def test_retry_after_http_date_floor_and_cap(self):
        from email.utils import formatdate
        def wait_for(value):
            self.run_nofake("arm")
            self.age(300)
            FakeApi.code, FakeApi.retry_after = 429, value
            self.nofake_hook()
            with open(self.cache_file()) as f:
                return json.load(f)["retry_after_until"] - time.time()
        w = wait_for(formatdate(time.time() + 200, usegmt=True))
        self.assertTrue(150 < w <= 201, w)
        self.assertTrue(wait_for(1) >= 55, "a Retry-After under 60 s is no guidance: back off from 60 s")
        self.assertTrue(wait_for(99999) <= 901, "cap 15 min")

    def test_processes_meeting_an_expired_window_make_one_fetch(self):
        """Four hooks start while the cache is stale: only one may fetch."""
        self.run_nofake("arm")
        self.age(300)
        FakeApi.delay = 1
        self.addCleanup(setattr, FakeApi, "delay", 0)
        self.srv.shutdown()
        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeApi)  # concurrent requests
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.env["USAGE_GATE_API"] = "http://127.0.0.1:%d" % self.srv.server_port
        n0 = len(FakeApi.paths)
        inp = json.dumps({"hook_event_name": "PreToolUse", "session_id": SID, "tool_name": "Agent", "tool_input": {}})
        ps = [subprocess.Popen([sys.executable, "-I", GATE, "hook"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               env=self.env, text=True) for _ in range(4)]
        for p in ps:
            p.stdin.write(inp)
            p.stdin.close()
        for p in ps:
            p.wait(timeout=30)
        self.assertEqual(len(FakeApi.paths) - n0, 1)

    def test_cache_update_without_the_lock_writes_nothing(self):
        r = self.py("""
os.makedirs(gate.CACHE, exist_ok=True)
p = gate.usage_path("svc")
with gate.locked(p + ".lock"):
    c, _ = gate.cache_update(p, lambda c: c.update(x=1))
print(os.path.exists(p) and "x" in json.load(open(p)), c.get("x"))
""")
        self.assertEqual(r.split(), ["False", "1"])

    def test_a_timeout_during_a_429_window_keeps_the_window(self):
        r = self.py("""
os.makedirs(gate.CACHE, exist_ok=True)
def setw(c):
    c["retry_after_until"] = time.time() + 300
    c["backoff"] = 300
def f(svc):
    gate.cache_update(gate.usage_path(svc), setw)  # another process meets a 429 while we are fetching
    raise TimeoutError("timed out")
gate.fetch_usage = f
gate.usage("svc", 60)
print(json.load(open(gate.usage_path("svc"))).get("retry_after_until", 0) - time.time() > 200)
""")
        self.assertEqual(r.strip(), "True")

    def test_two_sessions_on_one_account_send_one_notice_per_hour(self):
        self.run_nofake("arm")
        subprocess.run([sys.executable, "-I", GATE, "arm"], env=dict(self.env, CLAUDE_CODE_SESSION_ID="test-sess-2"),
                       capture_output=True, text=True, timeout=30)
        self.age(20 * 60)
        FakeApi.code, FakeApi.retry_after = 429, 120
        self.nofake_hook("Bash", "PostToolUse")
        self.nofake_hook("Bash", "PostToolUse", sid="test-sess-2")
        self.assertEqual(self.log_text().count("Can't read usage for"), 1)


class LauncherTest(GateBase):
    def test_launcher_choice_by_platform(self):
        g = load_gate(self.home)
        self.assertEqual(g.launcher("win32"), "python")
        self.assertEqual(g.launcher("linux"), "python3")
        self.assertEqual(g.launcher("darwin"), "python3")

    def test_messages_use_injected_platform(self):
        self.arm()
        for plat, want, bad in (("win32", "python -I", "python3"), ("linux", "python3 -I", None)):
            self.env["USAGE_GATE_PLATFORM"] = plat
            out = self.cmd("pause", fake="50,82")
            self.assertIn(want, out)
            if bad:
                self.assertNotIn(bad, out)

    def test_soft_hook_message_uses_injected_platform(self):
        self.arm()
        self.env["USAGE_GATE_PLATFORM"] = "win32"
        d, r = self.hook("50,82", "Bash")
        self.assertIn("python -I", r)
        self.assertNotIn("python3", r)


class PrivateWriteTest(GateBase):
    def test_writes_survive_a_missing_fchmod_and_stay_600_on_posix(self):
        g = load_gate(self.home)
        saved = os.fchmod
        del os.fchmod
        try:
            p = os.path.join(self.home, "x", "telegram.json")
            g.write_private_json(p, {"token": "t"})
            g.wjson(os.path.join(self.home, "x", "s.json"), {"a": 1})
        finally:
            os.fchmod = saved
        self.assertEqual(json.load(open(p)), {"token": "t"})
        if os.name == "posix":
            self.assertEqual(os.stat(p).st_mode & 0o777, 0o600)
            self.assertEqual(os.stat(os.path.join(self.home, "x", "s.json")).st_mode & 0o777, 0o600)


class FallbackLockTest(GateBase):
    def gate(self, timeout=None):
        g = load_gate(self.home)
        g.fcntl = None
        if timeout is not None:
            g.LOCK_TIMEOUT = timeout
        return g

    def test_stale_lock_is_broken_within_the_wait_timeout(self):
        g = self.gate()
        path = os.path.join(self.home, "a.lock")
        open(path + ".lk", "w").close()
        os.utime(path + ".lk", (time.time() - 5, time.time() - 5))
        t = time.time()
        with g.locked(path, timeout=2) as got:
            self.assertTrue(got)
        self.assertLess(time.time() - t, 1.5)

    def test_fresh_lock_is_respected(self):
        g = self.gate()
        path = os.path.join(self.home, "b.lock")
        open(path + ".lk", "w").close()
        with g.locked(path, timeout=0.3, stale=30) as got:
            self.assertFalse(got)
        self.assertTrue(os.path.exists(path + ".lk"))  # not ours, left alone

    def test_save_sess_does_not_write_without_the_lock(self):
        g = self.gate(timeout=0.3)
        g.LOCK_STALE = 30
        s = {"session": "lost", "armed": True}
        os.makedirs(os.path.dirname(g.sess_path("lost")), exist_ok=True)
        open(g.sess_path("lost") + ".lock.lk", "w").close()
        self.assertFalse(g.save_sess(s, replace=True))
        with self.assertRaises(g.SaveBusy):
            g.must_save(s, replace=True)
        self.assertFalse(os.path.exists(g.sess_path("lost")))

    def test_lock_going_stale_mid_wait_is_taken_over(self):
        g = self.gate()
        path = os.path.join(self.home, "c.lock")
        open(path + ".lk", "w").close()
        threading.Timer(0.3, lambda: os.utime(path + ".lk", (time.time() - 60, time.time() - 60))).start()
        t = time.time()
        with g.locked(path, timeout=3, stale=1) as got:
            self.assertTrue(got)
        self.assertLess(time.time() - t, 2.5)

    def test_release_only_removes_our_own_lock_file(self):
        g = self.gate()
        path = os.path.join(self.home, "d.lock")
        with g.locked(path) as got:
            self.assertTrue(got)
            with open(path + ".lk", "w") as f:  # someone broke our lock and took it over
                f.write("other-owner")
        self.assertEqual(open(path + ".lk").read(), "other-owner")

    def test_failed_restore_of_a_fresh_lock_means_lock_lost_and_not_acquired(self):
        from unittest import mock
        g = self.gate()
        path = os.path.join(self.home, "e.lock")
        open(path + ".lk", "w").close()
        ages = iter([100, 0])  # looked stale, but what the rename took turned out fresh
        with mock.patch.object(g, "_lock_age", side_effect=lambda lk: next(ages, 0)), \
                mock.patch.object(g.os, "link", side_effect=OSError("no")):
            with g.locked(path, timeout=0.3, stale=30) as got:
                self.assertFalse(got)
        self.assertIn("lock lost", open(g.LOG).read())

    def test_poll_lock_has_a_longer_stale_age(self):
        g = self.gate()
        self.assertGreater(g.POLL_LOCK_STALE, 10)

    def test_save_sess_writes_with_the_lock(self):
        g = self.gate(timeout=0.3)
        g.save_sess({"session": "ok", "armed": True}, replace=True)
        self.assertTrue(json.load(open(g.sess_path("ok")))["armed"])


import fcntl as _fcntl


class BusyLockTest(TelegramBase):
    upd, run_gate = CommandTest.upd, CommandTest.run_gate
    poll = CommandTest.poll_once  # these tests hold a lock on purpose: one poll, the update must stay unhandled

    def setUp(self):
        TelegramBase.setUp(self)
        self.env["USAGE_GATE_POLL_INTERVAL"] = "0"
        self.configure()
        self.uid = 1000

    def hold(self):
        p = os.path.join(self.home, ".claude", "usage-gate", "sessions", SID + ".json.lock")
        f = open(p, "a+")
        _fcntl.flock(f, _fcntl.LOCK_EX)
        self.addCleanup(f.close)
        return f

    def test_hook_still_gates_when_the_session_lock_is_busy(self):
        self.env["USAGE_GATE_LOCK_TIMEOUT"] = "0.3"
        self.arm()
        self.hold()
        d, r = self.hook("50,82", "Bash")  # hook() asserts exit code 0; soft now tells instead of denying
        self.assertEqual(d, "allow")
        self.assertIn("USAGE GATE", r)
        self.assertEqual(self.hook("50,82", "Agent")[0], "deny")

    def test_hook_keeps_a_hard_stop_when_the_lock_is_busy(self):
        self.env["USAGE_GATE_LOCK_TIMEOUT"] = "0.3"
        self.arm()
        self.hold()
        self.assertEqual(self.hook("50,97", "Agent")[0], "deny")

    def test_cli_command_reports_busy(self):
        self.env["USAGE_GATE_LOCK_TIMEOUT"] = "0.3"
        self.arm()
        self.hold()
        r = self.run_gate("pause")
        self.assertIn("busy", r.stdout + r.stderr)
        self.assertNotEqual(r.returncode, 0)

    def test_failed_apply_does_not_advance_the_update_id_and_is_retried(self):
        self.env["USAGE_GATE_LOCK_TIMEOUT"] = "0.3"
        self.arm()
        f = self.hold()
        self.upd("/stop")
        self.poll()
        last = os.path.join(self.home, ".claude", "usage-gate", "telegram-last.json")
        self.assertFalse(os.path.exists(last))
        _fcntl.flock(f, _fcntl.LOCK_UN)
        self.poll()
        self.assertEqual(self.sess().get("manual"), "stop")
        self.assertTrue(os.path.exists(last))


    def arm2(self):
        self.run_gate("arm", env=dict(self.env, CLAUDE_CODE_SESSION_ID=SID2))

    def sess2(self):
        with open(os.path.join(self.home, ".claude", "usage-gate", "sessions", SID2 + ".json")) as f:
            return json.load(f)

    def test_retry_after_busy_does_not_reapply_or_rereply_to_done_sessions(self):
        self.env["USAGE_GATE_LOCK_TIMEOUT"] = "0.3"
        self.arm()
        self.arm2()
        f = self.hold()  # SID busy; SID2 sorts first and is applied before the failure
        self.upd("/stop")
        self.poll()
        self.assertEqual(len(FakeTelegram.sent), 1)
        _fcntl.flock(f, _fcntl.LOCK_UN)
        self.poll()
        self.assertEqual(len(FakeTelegram.sent), 2)  # only SID's reply is new
        self.assertEqual(self.sess().get("manual"), "stop")
        self.assertEqual(self.sess2().get("manual"), "stop")

    def test_other_errors_are_skipped_and_the_update_id_advances(self):
        self.arm()
        self.arm2()
        self.upd("/stop")
        env = dict(self.env, USAGE_GATE_TEST_APPLY_FAIL=SID2)
        self.poll(env=env)
        last = os.path.join(self.home, ".claude", "usage-gate", "telegram-last.json")
        self.assertEqual(json.load(open(last))["last_update_id"], self.uid)
        self.assertEqual(self.sess().get("manual"), "stop")  # the other session still got it
        self.assertIsNone(self.sess2().get("manual"))
        n = len(FakeTelegram.sent)
        self.poll()
        self.assertEqual(len(FakeTelegram.sent), n)  # not retried


class WjsonRetryTest(GateBase):
    def test_replace_retries_on_permission_error_then_succeeds(self):
        g = load_gate(self.home)
        real, calls = g.os.replace, []

        def flaky(a, b):
            calls.append(1)
            if len(calls) < 3:
                raise PermissionError("in use")
            return real(a, b)
        g.os.replace = flaky
        try:
            p = os.path.join(self.home, "w", "x.json")
            g.wjson(p, {"a": 1})
        finally:
            g.os.replace = real
        self.assertEqual(json.load(open(p)), {"a": 1})
        self.assertEqual(len(calls), 3)

    def test_failure_cleans_tmp_and_save_sess_does_not_raise(self):
        g = load_gate(self.home)
        real = g.os.replace
        g.os.replace = lambda a, b: (_ for _ in ()).throw(PermissionError("in use"))
        try:
            g.wjson.__defaults__  # noqa
            self.assertFalse(g.save_sess({"session": "pe", "armed": True}, replace=True))
        finally:
            g.os.replace = real
        d = os.path.dirname(g.sess_path("pe"))
        self.assertEqual([n for n in os.listdir(d) if n.endswith(".tmp")], [])


class KeychainChoiceTest(GateBase):
    def test_keychain_preferred_over_credentials_file_on_darwin(self):
        from unittest import mock
        os.makedirs(os.path.join(self.home, ".claude"))
        with open(os.path.join(self.home, ".claude", ".credentials.json"), "w") as f:
            json.dump({"claudeAiOauth": {"accessToken": "FILE"}}, f)
        g = load_gate(self.home, platform="darwin")
        os.environ.pop("USAGE_GATE_NO_KEYCHAIN", None)
        fake = mock.Mock(returncode=0, stdout=json.dumps({"claudeAiOauth": {"accessToken": "KEYCHAIN"}}))
        with mock.patch.object(g.shutil, "which", return_value="/usr/bin/security"), \
                mock.patch.object(g.subprocess, "run", return_value=fake):
            self.assertEqual(g.kc_read("x")["claudeAiOauth"]["accessToken"], "KEYCHAIN")
        with mock.patch.object(g, "platform", return_value="linux"):
            self.assertEqual(g.kc_read("x")["claudeAiOauth"]["accessToken"], "FILE")

    def test_no_keychain_ignores_account_uuid_and_never_manages(self):
        from unittest import mock
        g = load_gate(self.home)
        os.environ["CLAUDE_CODE_ACCOUNT_UUID"] = "some-uuid"
        try:
            with mock.patch.object(g, "platform", return_value="linux"):
                svc, email = g.session_service()
                g.wjson(g.CONFIG, {"accounts": {"u": {"service": svc, "managed": True}}})
                self.assertTrue(g.managed(svc))
                self.assertFalse(g.has_keychain())
        finally:
            os.environ.pop("CLAUDE_CODE_ACCOUNT_UUID")
        self.assertEqual(svc, g.DEFAULT_SVC)


class HookEncodingTest(GateBase):
    def test_hook_reads_utf8_bytes_on_stdin(self):
        inp = json.dumps({"hook_event_name": "PreToolUse", "session_id": SID, "tool_name": "Bash",
                          "tool_input": {"command": "echo h\u00e9llo \u2603"}}, ensure_ascii=False).encode("utf-8")
        env = dict(self.env, PYTHONIOENCODING="ascii", PYTHONUTF8="0", LC_ALL="C")
        r = subprocess.run([sys.executable, "-I", GATE, "hook"], input=inp, env=env, capture_output=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_utf8_session_file_roundtrips_under_ascii_locale(self):
        env = dict(self.env, LC_ALL="C", PYTHONUTF8="0", USAGE_GATE_FAKE="10,10")
        os.makedirs(os.path.join(self.home, "caf\u00e9"))
        r = subprocess.run([sys.executable, "-I", GATE, "arm"], env=env, capture_output=True, text=True,
                           cwd=os.path.join(self.home, "caf\u00e9"), timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.sess()["project"], "caf\u00e9")


INSTALL = os.path.join(os.path.dirname(GATE), "install.py")


class InstallerTest(GateBase):
    def setUp(self):
        super().setUp()
        self.env.update(USERPROFILE=self.home, USAGE_GATE_NO_KEYCHAIN="1")
        self.settings = os.path.join(self.home, ".claude", "settings.json")
        os.makedirs(os.path.dirname(self.settings))
        other = {"type": "command", "command": "echo other-hook"}
        old = {"type": "command", "command": "python3 -I ~/.claude/usage-gate/gate.py hook", "timeout": 20}
        with open(self.settings, "w") as f:
            json.dump({"theme": "dark", "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [other]},
                                                                {"matcher": "*", "hooks": [old]}]}}, f)

    def install(self):
        return subprocess.run([sys.executable, INSTALL], env=self.env, capture_output=True, text=True, timeout=60)

    def cmds(self, ev):
        d = json.load(open(self.settings))
        return [h["command"] for g in d["hooks"][ev] for h in g["hooks"]]

    def test_merges_hooks_with_absolute_paths_and_no_duplicate(self):
        for _ in range(2):
            r = self.install()
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        gate = os.path.join(self.home, ".claude", "usage-gate", "gate.py")
        self.assertTrue(os.path.exists(gate))
        self.assertTrue(os.path.exists(os.path.join(self.home, ".claude", "skills", "usage-gate", "SKILL.md")))
        pre = self.cmds("PreToolUse")
        self.assertIn("echo other-hook", pre)
        ours = [c for c in pre if "gate.py" in c]
        self.assertEqual(len(ours), 1)
        self.assertTrue(ours[0].startswith('"%s" ' % (shutil.which("python3") or sys.executable)), ours[0])
        self.assertIn(gate.replace("\\", "/"), ours[0])
        self.assertNotIn("~", ours[0])
        self.assertNotIn("python3 -I", ours[0])
        self.assertEqual(len([c for c in self.cmds("PostToolUse") if "gate.py" in c]), 1)
        self.assertEqual(json.load(open(self.settings))["theme"], "dark")

    def test_accounts_step_is_skipped_with_a_note_without_keychain(self):
        r = self.install()
        self.assertIn("no keychain", r.stdout)


    def test_command_runs_through_a_shell_with_a_space_in_home(self):
        self.install()
        ours = [c for c in self.cmds("PreToolUse") if "gate.py" in c][0]
        self.assertIn(" ", self.home)
        r = subprocess.run(ours, shell=True, input="{}", env=self.env, capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_interpreter_is_not_a_pinned_versioned_path(self):
        self.install()
        ours = [c for c in self.cmds("PreToolUse") if "gate.py" in c][0]
        want = shutil.which("python3") or sys.executable
        self.assertTrue(ours.startswith('"%s" ' % want), ours)

    @unittest.skipIf(os.name == "nt", "posix PATH layout")
    def test_python_from_a_venv_or_shims_dir_is_skipped(self):
        root = tempfile.mkdtemp()
        dirs = {n: os.path.join(root, n) for n in ("venv/bin", "pyenv/shims", "real")}
        for d in dirs.values():
            os.makedirs(d)
            with open(os.path.join(d, "python3"), "w") as f:
                f.write("#!/bin/sh\n")
            os.chmod(os.path.join(d, "python3"), 0o755)
        self.env.update(VIRTUAL_ENV=os.path.join(root, "venv"),
                        PATH=os.pathsep.join([dirs["venv/bin"], dirs["pyenv/shims"], dirs["real"]]))
        self.install()
        ours = [c for c in self.cmds("PreToolUse") if "gate.py" in c][0]
        self.assertTrue(ours.startswith('"%s" ' % os.path.join(dirs["real"], "python3")), ours)

    def test_missing_settings_is_created(self):
        os.remove(self.settings)
        r = self.install()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(self.cmds("PreToolUse")), 1)

    def test_malformed_settings_stops_before_writing_anything(self):
        with open(self.settings, "w") as f:
            f.write("{ not json")
        r = self.install()
        self.assertEqual(r.returncode, 1)
        self.assertIn("settings.json", r.stdout + r.stderr)
        self.assertEqual(open(self.settings).read(), "{ not json")
        self.assertFalse(os.path.exists(os.path.join(self.home, ".claude", "usage-gate", "gate.py")))
        self.assertEqual([n for n in os.listdir(os.path.dirname(self.settings)) if n.endswith(".tmp")], [])

    def test_bom_settings_is_read_and_others_empty_groups_are_kept(self):
        d = json.load(open(self.settings))
        d["hooks"]["Stop"] = [{"matcher": "", "hooks": []}]
        with open(self.settings, "w", encoding="utf-8-sig") as f:
            json.dump(d, f)
        r = self.install()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.load(open(self.settings))["hooks"]["Stop"], [{"matcher": "", "hooks": []}])

    def test_installed_gate_runs(self):
        self.install()
        gate = os.path.join(self.home, ".claude", "usage-gate", "gate.py")
        r = subprocess.run([sys.executable, "-I", gate, "hook"], input="{}", env=self.env,
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0)


class StormTest(CommandTest):
    """A 429 stops every fetch on the machine (hook, poll child, listen, continue) for the whole window."""
    def setUp(self):
        super().setUp()
        FakeApi.auth, FakeApi.paths, FakeApi.code, FakeApi.retry_after, FakeApi.five = [], [], 200, None, 85
        self.api = http.server.HTTPServer(("127.0.0.1", 0), FakeApi)
        threading.Thread(target=self.api.serve_forever, daemon=True).start()
        self.addCleanup(self.api.shutdown)
        self.addCleanup(setattr, FakeApi, "retry_after", None)
        self.addCleanup(setattr, FakeApi, "five", 31)
        self.env.update(USAGE_GATE_NO_KEYCHAIN="1", USAGE_GATE_API="http://127.0.0.1:%d" % self.api.server_port)
        cred = os.path.join(self.home, ".claude", ".credentials.json")
        os.makedirs(os.path.dirname(cred), exist_ok=True)
        with open(cred, "w") as f:
            json.dump({"claudeAiOauth": {"accessToken": "t", "refreshToken": "r",
                                         "expiresAt": int((time.time() + 3600) * 1000)}}, f)

    def real(self, *args):
        return subprocess.run([sys.executable, "-I", GATE] + list(args), env=self.env, capture_output=True,
                              text=True, timeout=30)

    def real_hook(self, tool="Bash", event="PreToolUse"):
        inp = json.dumps({"hook_event_name": event, "session_id": SID, "tool_name": tool, "tool_input": {}})
        return subprocess.run([sys.executable, "-I", GATE, "hook"], input=inp, env=self.env, capture_output=True,
                              text=True, timeout=30).stdout

    def cache(self):
        d = os.path.join(self.home, ".claude", "usage-gate", "cache")
        return os.path.join(d, [n for n in os.listdir(d) if n.startswith("usage-") and n.endswith(".json")][0])

    def shift_cache(self, sec):
        with open(self.cache()) as f:
            c = json.load(f)
        c["good"]["ts"] -= sec
        if "retry_after_until" in c:
            c["retry_after_until"] -= sec
            c["err"]["ts"] -= sec
        with open(self.cache(), "w") as f:
            json.dump(c, f)

    def test_429_with_tiny_retry_after_holds_across_hook_poll_listen_and_continue(self):
        """Live 2026-11-21 10:17: Retry-After floored at 10 s, so every process refetched about every 10 s."""
        self.real("arm", "--ask", "10")
        self.shift_cache(1000)  # no usable reading left
        FakeApi.code, FakeApi.retry_after = 429, 1
        self.real_hook()
        n = len(FakeApi.paths)
        self.shift_cache(11)  # 11 s later: a 10 s back-off would be over, a 60 s one is not
        self.upd("/continue")
        self.real("telegram", "poll")  # poll child -> do_continue
        self.upd("/continue")
        self.real("listen")  # listen -> tg_poll -> do_continue
        self.real_hook()
        self.real("continue")
        self.assertEqual(len(FakeApi.paths), n, "no fetch while the back-off holds")
        self.assertEqual(self.sess()["override"], "soft")  # /continue during the 429 keeps the override
        log = self.logtext()
        self.assertEqual(log.count("usage error"), 1)
        self.assertEqual(log.count("usage fetch"), log.count("usage error") + 1)  # the arm fetch and the 429

    def test_fetch_log_names_the_caller(self):
        self.real("arm")
        self.shift_cache(1000)
        self.upd("/continue")
        self.real("telegram", "poll")
        log = self.logtext()
        self.assertIn("by main2 (cmd arm)", log)
        self.assertIn("by do_continue (cmd telegram)", log)


class ButtonsTest(CommandTest):
    def press(self, act, sid=SID, chat=42, age=0):
        self.uid += 1
        FakeTelegram.updates.append({"update_id": self.uid, "callback_query": {
            "id": "cb%d" % self.uid, "from": {"id": 42}, "data": "%s:%s" % (act, sid[:12]),
            "message": {"message_id": 7, "chat": {"id": chat, "type": "private"}, "date": int(time.time() - age)}}})

    def buttons(self, i=-1):
        rows = FakeTelegram.sent[i]["reply_markup"]["inline_keyboard"]
        return [(b["text"], b["callback_data"].split(":")[0]) for r in rows for b in r]

    def test_setup_sets_the_command_menu(self):
        os.remove(self.conf) if os.path.exists(self.conf) else None
        FakeTelegram.updates.append({"update_id": 1, "message": {"message_id": 1, "date": int(time.time()),
                                     "chat": {"id": 42, "type": "private", "first_name": "A"}, "text": "/start"}})
        self.assertEqual(self.setup().returncode, 0)
        menu = [b for m, b in FakeTelegram.calls if m == "setMyCommands"]
        self.assertEqual(len(menu), 1)
        self.assertEqual([c["command"] for c in menu[0]["commands"]],
                         ["status", "stop", "pause", "continue", "resume", "off", "help"])
        self.assertTrue(all(c["description"] for c in menu[0]["commands"]))

    def test_telegram_menu_command(self):
        r = self.run_gate("telegram", "menu")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len([1 for m, _ in FakeTelegram.calls if m == "setMyCommands"]), 1)

    def test_alerts_carry_buttons_for_their_phase(self):
        self.arm()
        self.hook("50,82", "Bash")
        self.wait_sent(1)
        self.assertEqual(self.buttons(), [("Continue", "continue"), ("Pause", "pause")])
        self.assertTrue(FakeTelegram.sent[0]["reply_markup"]["inline_keyboard"][0][0]["callback_data"].endswith(SID[:12]))
        self.hook("50,99", "Agent")
        self.wait_sent(2)
        self.assertEqual(self.buttons(), [("Resume", "resume"), ("Status", "status")])  # hard stop
        s = self.sess()
        s.update(phase="paused_soft")
        with open(os.path.join(self.home, ".claude", "usage-gate", "sessions", SID + ".json"), "w") as f:
            json.dump(s, f)
        subprocess.run([sys.executable, "-I", GATE, "telegram", "send", "", SID], input="Session paused.", env=self.env,
                       capture_output=True, text=True, timeout=30)
        self.assertEqual(self.buttons(), [("Resume", "resume"), ("Status", "status")])
        s.update(phase="packing")
        with open(os.path.join(self.home, ".claude", "usage-gate", "sessions", SID + ".json"), "w") as f:
            json.dump(s, f)
        subprocess.run([sys.executable, "-I", GATE, "telegram", "send", "", SID], input="No answer.", env=self.env,
                       capture_output=True, text=True, timeout=30)
        self.assertEqual(self.buttons(), [("Status", "status")])

    def sess_of(self, sid):
        with open(os.path.join(self.home, ".claude", "usage-gate", "sessions", sid + ".json")) as f:
            return json.load(f)

    def answers(self, wait=0):
        """The answerCallbackQuery calls so far; the child that applied a press answers just after it applies it."""
        end = time.time() + wait
        while True:
            a = [x for m, x in FakeTelegram.calls if m == "answerCallbackQuery"]
            if a or time.time() >= end:
                return a
            time.sleep(0.1)

    def test_button_applies_to_its_own_session_only(self):
        self.arm()
        self.run_gate("arm", env=dict(self.env, CLAUDE_CODE_SESSION_ID=SID2))
        self.hook("50,82", "Bash")
        subprocess.run([sys.executable, "-I", GATE, "hook"], env=dict(self.env, USAGE_GATE_FAKE="50,82"),
                       input=json.dumps({"hook_event_name": "PreToolUse", "session_id": SID2, "tool_name": "Bash",
                                         "tool_input": {}}), capture_output=True, text=True, timeout=30)
        self.press("continue", SID2)
        self.assertTrue(self.poll_until(lambda: self.sess_of(SID2)["phase"] == "run", fake="50,82"))
        b = self.sess_of(SID2)
        self.assertEqual((b["phase"], b["override"]), ("run", "soft"))
        self.assertEqual(self.sess()["phase"], "asked")  # session A untouched
        ans = self.answers(wait=10)
        self.assertEqual(len(ans), 1)
        self.assertTrue(ans[0]["callback_query_id"].startswith("cb"))

    def test_pause_and_resume_buttons(self):
        self.arm()
        self.press("pause")
        self.assertTrue(self.poll_until(lambda: self.sess()["phase"] == "packing"))
        self.press("resume")
        self.assertTrue(self.poll_until(lambda: self.sess()["phase"] == "run"))

    def test_button_from_another_chat_is_ignored(self):
        self.arm()
        self.hook("50,82", "Bash")
        self.press("continue", chat=999)
        self.poll(fake="50,82")
        self.assertEqual(self.sess()["phase"], "asked")
        self.assertEqual([m for m, _ in FakeTelegram.calls if m == "answerCallbackQuery"], [])

    def test_a_machine_back_from_sleep_does_not_delete_another_machines_fresh_press(self):
        self.arm()
        ok = os.path.join(self.home, ".claude", "usage-gate", "telegram-ok")
        with open(ok, "w") as f:
            f.write(str(time.time() - 7200))
        self.press("resume", sid=SID2)  # a press for a session of another machine
        self.poll_until(lambda: False, t=2)
        self.assertEqual(len([u for u in FakeTelegram.updates if "callback_query" in u]), 1)

    def test_resume_on_an_old_hard_stop_alert_still_applies_when_the_machine_was_polling(self):
        """Live-style: the press is new, the alert it sits on is an hour old."""
        self.arm()
        self.hook("50,99", "Bash")
        self.assertEqual(self.sess()["phase"], "hard")
        self.press("resume", age=3600)
        self.assertTrue(self.poll_until(lambda: self.sess()["phase"] == "run", fake="10,10"))
        self.assertEqual(len(self.answers(wait=10)), 1)

    def test_press_after_a_long_gap_is_answered_too_old_and_not_applied(self):
        self.arm()
        self.hook("50,99", "Bash")
        ok = os.path.join(self.home, ".claude", "usage-gate", "telegram-ok")
        with open(ok, "w") as f:
            f.write(str(time.time() - 7200))  # this machine last read Telegram two hours ago
        self.press("resume")
        self.poll_until(lambda: self.answers(), fake="50,99")
        self.assertIn("too old", self.answers()[0]["text"].lower())
        self.assertEqual(self.sess()["phase"], "hard")

    def test_press_for_a_disarmed_session_is_answered(self):
        self.arm()
        self.cmd("disarm")
        self.press("continue")
        self.poll_until(lambda: self.answers())
        self.assertIn("no longer armed", self.answers()[0]["text"])

    def test_typed_commands_still_work(self):
        self.arm()
        self.hook("50,82", "Bash")
        self.upd("/continue")
        self.poll(fake="50,82")
        self.assertEqual(self.sess()["phase"], "run")


class PhoneContinueTest(CommandTest):
    def test_timer_of_an_old_question_does_not_pack_a_newer_one(self):
        self.arm(ask="0.1")  # 6 s: room for a loaded machine
        self.hook("50,82", "Bash")
        p = subprocess.Popen([sys.executable, "-I", GATE, "timer"], env=dict(self.env, USAGE_GATE_FAKE="50,82"),
                             stdout=subprocess.PIPE, text=True)
        time.sleep(0.4)
        path = os.path.join(self.home, ".claude", "usage-gate", "sessions", SID + ".json")
        s = self.sess()
        s["asked_at"] = time.time()  # a fresh question was asked meanwhile: its own timer owns it
        with open(path, "w") as f:
            json.dump(s, f)
        out = p.communicate(timeout=30)[0]
        self.assertNotIn("no answer", out)
        self.assertEqual(self.sess()["phase"], "asked")

    def test_soft_ask_then_phone_continue_then_hooks_pass_until_hard(self):
        self.arm(ask="0")
        self.assertEqual(self.hook("50,82", "Bash")[0], "allow")  # the soft question
        self.upd("/continue")
        self.poll(fake="50,82")
        out = self.run_gate("timer", fake="50,82").stdout  # the timer of the question wakes after the answer
        self.assertNotIn("no answer", out)
        self.assertEqual(self.sess()["phase"], "run")
        for tool in ("Bash", "Agent", "Edit", "Agent"):
            self.assertEqual(self.hook("50,82", tool)[0], "allow", tool)
        self.assertEqual(self.hook("50,96", "Agent")[0], "deny")  # still gates at the hard limit


class ReexecTest(CommandTest):
    """A long-running loop picks up a new gate.py without changing pid or stdout (live 2026-11-21: an old listen
    kept consuming button presses after the update was installed)."""
    def copy(self):
        d = os.path.join(self.home, "gatecopy")
        os.makedirs(d)
        self.copy_path = os.path.join(d, "gate.py")
        shutil.copy(GATE, self.copy_path)
        self.marker = os.path.join(self.home, "marker")
        self.addCleanup(lambda: os.path.exists(self.marker) and None)

    def update_copy(self, cmd):
        with open(self.copy_path) as f:
            src = f.read()
        marker = ('if sys.argv[1:2] == [%r]: open(%r, "a").write("%%d %%d" %% (os.getpid(), os.getppid()) + chr(10))\n'
                  % (cmd, self.marker))
        src = src.replace("DEFAULT_SVC =", marker + "DEFAULT_SVC =", 1)
        with open(self.copy_path, "w") as f:
            f.write(src)

    def wait_marker(self, t=10):
        end = time.time() + t
        while time.time() < end:
            if os.path.exists(self.marker):
                with open(self.marker) as f:
                    return f.read().split()[:1]
            time.sleep(0.1)
        return []

    def spawn(self, cmd, fake="50,82"):
        p = subprocess.Popen([sys.executable, "-I", self.copy_path, cmd], env=dict(self.env, USAGE_GATE_FAKE=fake),
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(lambda: p.poll() is None and p.kill())
        return p

    def test_listen_restarts_into_the_new_code_with_the_same_pid_and_still_hears_commands(self):
        self.copy()
        self.arm()
        p = self.spawn("listen")
        time.sleep(1.0)
        self.update_copy("listen")
        self.assertEqual(self.wait_marker(), [str(p.pid)])  # new code ran in the same process
        self.upd("/continue")
        out = p.communicate(timeout=30)[0]
        self.assertIn("Carry on", out)  # the restarted listen kept its start state and heard the command

    def test_windows_path_runs_the_new_code_as_a_child_and_passes_exit_and_output_through(self):
        self.copy()
        self.arm()
        self.env["USAGE_GATE_NT"] = "1"  # injected platform flag: take the os.name == "nt" wrapper path
        p = self.spawn("listen")
        time.sleep(1.0)
        self.update_copy("listen")
        end = time.time() + 10
        while not os.path.exists(self.marker) and time.time() < end:
            time.sleep(0.1)
        with open(self.marker) as f:
            child, parent = f.read().split()[:2]
        self.assertNotEqual(child, str(p.pid))  # a new process ...
        self.assertEqual(parent, str(p.pid))    # ... with the old one left as the wrapper
        self.upd("/continue")  # state (seq0) survived: the child hears the command
        out = p.communicate(timeout=30)[0]
        self.assertIn("Carry on", out)
        self.assertIn("blocked.md", out)  # phone /continue tells Claude to read blocked.md
        self.assertEqual(p.returncode, 0)

    def test_a_broken_new_file_is_not_executed_and_is_logged_once(self):
        self.copy()
        self.arm()
        p = self.spawn("listen")
        time.sleep(1.0)
        with open(self.copy_path, "a") as f:
            f.write("\ndef broken(:\n")
        time.sleep(3.0)  # many ticks at 0.2 s
        self.assertIsNone(p.poll(), "the old code keeps running")
        self.assertEqual(self.logtext().count("not loadable"), 1)
        self.upd("/continue")
        self.assertIn("Carry on", p.communicate(timeout=30)[0])

    def test_timer_restarts_into_the_new_code_and_keeps_its_deadline(self):
        self.copy()
        self.arm(ask="0.2")  # 12 s
        self.hook("50,82", "Bash")
        t0 = time.time()
        p = self.spawn("timer")
        time.sleep(4.0)
        self.update_copy("timer")
        self.assertEqual(self.wait_marker(), [str(p.pid)])
        out = p.communicate(timeout=40)[0]
        took = time.time() - t0
        self.assertIn("no answer", out)
        self.assertLess(took, 14.5, "the countdown must not restart (a restart would end near 16 s)")
        self.assertEqual(self.sess()["phase"], "packing")

    def test_wait_restarts_into_the_new_code(self):
        self.copy()
        self.arm()
        self.hook("50,99", "Bash")
        p = self.spawn("wait", fake="50,99")
        time.sleep(1.0)
        self.update_copy("wait")
        self.assertEqual(self.wait_marker(), [str(p.pid)])
        self.upd("/resume")
        self.assertIn("resumed by the user", p.communicate(timeout=40)[0])


for _cls in (StormTest, ButtonsTest, PhoneContinueTest, ReexecTest):  # reuse CommandTest's helpers without re-running its tests
    for _n in dir(CommandTest):
        if _n.startswith("test") and _n not in _cls.__dict__:
            setattr(_cls, _n, None)


if __name__ == "__main__":
    unittest.main()