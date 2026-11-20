"""Gate state machine tests. Isolated: HOME points at a temp folder, usage is faked, notifications are quiet."""
import http.server, json, os, shutil, subprocess, sys, tempfile, threading, time, unittest

GATE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "gate.py")
SID = "test-sess"


class GateBase(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="ug home ")
        self.env = dict(os.environ, HOME=self.home, USAGE_GATE_QUIET="1", CLAUDE_CODE_SESSION_ID=SID)
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
        h = json.loads(r.stdout)["hookSpecificOutput"]
        return h.get("permissionDecision", "?"), h.get("permissionDecisionReason", "")

    def sess(self):
        with open(os.path.join(self.home, ".claude", "usage-gate", "sessions", SID + ".json")) as f:
            return json.load(f)

    def arm(self, ask="10"):
        self.cmd("arm", "--ask", ask)


class GateTest(GateBase):
    def test_unarmed_session_is_untouched(self):
        self.assertEqual(self.hook("99,99", "Bash")[0], "allow")

    def test_below_soft_allows(self):
        self.arm()
        self.assertEqual(self.hook("10,10", "Agent")[0], "allow")

    def test_soft_asks_lead_once_then_blocks_only_new_work(self):
        self.arm()
        d, r = self.hook("50,82", "Bash")
        self.assertEqual(d, "deny")
        self.assertIn("ask the user", r)
        self.assertEqual(self.sess()["phase"], "asked")
        self.assertEqual(self.hook("50,82", "Edit")[0], "allow")
        self.assertEqual(self.hook("50,82", "Agent")[0], "deny")
        self.assertEqual(self.hook("50,82", "mcp__ccd_session__start_session")[0], "deny")

    def test_soft_lets_subagents_finish_but_not_spawn(self):
        self.arm()
        self.assertEqual(self.hook("50,82", "Bash", agent="a1")[0], "allow")
        self.assertEqual(self.hook("50,82", "Agent", agent="a1")[0], "deny")

    def test_question_timeout_packs_up(self):
        self.arm(ask="0.01")
        self.hook("50,82", "Bash")
        time.sleep(1.2)
        d, r = self.hook("50,82", "Read")
        self.assertEqual((d, self.sess()["phase"]), ("deny", "packing"))
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
        self.assertEqual(d, "deny")
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

    def test_arm_without_account_uuid_finds_email_by_login(self):
        os.makedirs(os.path.join(self.home, ".claude", "usage-gate"), exist_ok=True)
        with open(os.path.join(self.home, ".claude", "usage-gate", "config.json"), "w") as f:
            json.dump({"accounts": {"u1": {"service": "Claude Code-credentials", "email": "a@b.c",
                                           "managed": False}}}, f)
        self.arm()
        self.assertEqual(self.sess()["email"], "a@b.c")

    def test_paused_session_tells_lead_to_resume_first(self):
        self.arm()
        self.hook("50,82", "Bash")
        self.cmd("continue", fake="50,82")
        s = self.sess()
        s["phase"] = "paused_soft"
        with open(os.path.join(self.home, ".claude", "usage-gate", "sessions", SID + ".json"), "w") as f:
            json.dump(s, f)
        d, r = self.hook("50,96", "Bash")
        self.assertEqual(d, "deny")
        self.assertIn("resume", r)

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
    sent, updates, fail, hang, code, gets = [], [], False, 0, 0, []

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
        if method == "getUpdates":
            cls.gets.append(body)
            if body.get("offset"):  # Telegram forgets updates below the offset
                cls.updates[:] = [u for u in cls.updates if u.get("update_id", 0) >= body["offset"]]
            res = list(cls.updates)
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
        FakeTelegram.code, FakeTelegram.gets = 0, []
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
        self.assertEqual(self.hook("50,82", "Bash")[0], "deny")
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
        self.assertEqual(self.hook("50,82", "Bash")[0], "deny")
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
        self.assertEqual(self.hook("50,82", "Bash")[0], "deny")
        self.assertEqual(self.sess()["phase"], "asked")
        time.sleep(1.5)
        log = self.logtext()
        self.assertIn("telegram send failed", log)
        self.assertNotIn("TESTTOKEN", log)

    def test_stale_reading_is_not_sent(self):
        self.configure()
        self.arm()
        self.env["USAGE_GATE_FAKE_AGE"] = "400"
        self.assertEqual(self.hook("50,82", "Bash")[0], "deny")
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
        self.assertEqual(self.hook("50,82", "Bash")[0], "deny")
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

    def poll(self, env=None, fake="10,10"):
        r = self.run_gate("telegram", "poll", env=env, fake=fake)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

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
        self.poll(fake="50,82")
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
    auth, paths, code, five = [], [], 200, 31

    def do_GET(self):
        cls = type(self)
        cls.auth.append(self.headers.get("Authorization"))
        cls.paths.append(self.path)
        if cls.code != 200:
            self.send_response(cls.code)
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
        self.assertEqual(FakeApi.paths, ["/api/oauth/usage"])  # no token endpoint call, only the usage read
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
    upd, run_gate, poll = CommandTest.upd, CommandTest.run_gate, CommandTest.poll

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

    def test_hook_still_denies_when_the_session_lock_is_busy(self):
        self.env["USAGE_GATE_LOCK_TIMEOUT"] = "0.3"
        self.arm()
        self.hold()
        d, r = self.hook("50,82", "Bash")  # hook() asserts exit code 0
        self.assertEqual(d, "deny")
        self.assertIn("USAGE GATE", r)

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


if __name__ == "__main__":
    unittest.main()