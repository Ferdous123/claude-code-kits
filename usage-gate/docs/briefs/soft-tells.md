# Brief: soft-tells
Model: middle
Branch: soft-tells
Touch: gate.py, tests/**, skill/**, docs/**
Done when: `./check.sh`

## Task
Owner's rule (2026-11-21, D21): past the soft limit the session finishes its current work without obstruction; only NEW work is refused (NEW_WORK set: Agent, Task, Workflow, start_session, …). The session obeys a message, so soft phases tell, not block.
Incident (IELTS lead session): at the 80% soft limit the hook (gate.py hook()) denied a SendMessage to an already-running builder, because the first tool call after the soft level is always denied once ("told" logic). After the session packed up and ran `gate.py wait` (phase paused_soft), every tool was denied, including Edit on RESUME.md and recording a builder's hand-back. Even `gate.py resume` was denied when combined with another command in one Bash call (that part is intended: lone gate commands only, D20).

1. asked / packing: tell once through additionalContext (`out(ctx=msg)`), no deny; deny only NEW_WORK tools.
2. paused_soft: allow every non-NEW_WORK tool with the paused message as context; deny NEW_WORK.
3. Hard stop, paused_hard and the owner's phone /stop or /pause stay blocking exactly as now.
4. Test helper: no permissionDecision means allow; the reason may come from additionalContext. Fix the tests that expect deny-once (test_soft_asks_lead_once…, test_question_timeout_packs_up, test_pause_packs_up_now, test_paused_session_tells_lead_to_resume_first). Add: in paused_soft, Edit and SendMessage allowed, Agent and start_session denied. A first attempt elsewhere that flipped these phases broke 8 other tests: check each for intent; change none just to pass — if one encodes the old rule, update it and say why in the commit.
5. skill/SKILL.md: rewrite "Soft" and "Packing" to match (the gate tells; Claude stops starting new work, finishes current work, then asks / packs).
6. Every new or changed test must fail on the old behaviour for the right reason.
