# WARDEN — Phases 0 & 1 scaffold

Workflow with Agents, Rules, Determinism, Escalation, Notifications.
Repo is the only source of truth; every transition is gated by a script.

## Layout
```
agents/      one .md rules file per agent role
schemas/     JSON schemas for stage artifacts (validation IS a gate)
gates/       one executable script per gate (exit 0 = pass, JSON verdict on stdout)
pipeline/    orchestrator (run.py)
tasks/       one folder per task: task.md, state.json, run.jsonl, artifacts/, workdir/
evals/       benchmark tasks (Phase 7)
hooks/       Claude Code hook scripts (.claude/settings.json wires them)
```

## Quick start
```bash
pip install -r requirements.txt
python3 pipeline/run.py new task-001 "Create a README with project overview"
python3 pipeline/run.py run task-001                # stub agents, no LLM needed
python3 pipeline/run.py run task-001 --agent-mode claude   # real headless Claude Code
python3 pipeline/run.py status task-001
```

## Phase 1 "done when" checks
1. Toy task flows through all stages: the quick start above, stub mode.
2. kill -9 resumability: start a run, kill it mid-stage, run again — it
   resumes from state.json at the recorded stage.
3. Corrupted artifact rejected: hand-edit tasks/<id>/artifacts/spec.json to
   break the schema, re-run — the stage fails schema validation and escalates.

## Per-project gate config (env vars)
- WARDEN_LINT_CMD, WARDEN_BUILD_CMD — used by gate_implement.sh
- WARDEN_TEST_CMD — gate_test.sh re-runs the real suite (trust scripts, not reports)

## Secrets hygiene (Phase 0, item 4)
Secrets live OUTSIDE the repo in ~/.warden/secrets.env; `.env*` is gitignored.
Load them only in the orchestrator process — never into agent prompts.

## Claude Code invocation
run.py uses `claude -p <prompt> --append-system-prompt <rules> --output-format json`
per current headless docs. Verify against `claude --help` on your machine, and
run agents inside a container with no production credentials (Phase 0, step 5).

## What is deliberately stubbed (and where it gets real)
- gate_approve.sh auto-passes        -> Phase 4 (Telegram approval, diff-hash bound)
- gate_merge.sh records a marker     -> Phase 8 (merge queue + rebase gate)
- escalate() halts + NEEDS_HUMAN     -> Phase 2 (retries, rollback) + Phase 4 (notify)
- attempts/max_attempts in state     -> Phase 2 retry loop consumes them
- hooks/notify.sh logs to a file     -> Phase 4 Telegram curl

## Phase 4 — Telegram notifications + approvals
Setup (one time):
1. Telegram: talk to @BotFather -> /newbot -> copy the token.
2. Get your NUMERIC user id from @userinfobot.
3. `mkdir -p ~/.warden && cp secrets.env.example ~/.warden/secrets.env` and fill both values.

Notifications: every stage transition and escalation calls hooks/notify.sh
(Telegram when configured, /tmp/warden-notify.log otherwise).

Approvals: the approve stage parks the task (`awaiting_approval`) and posts
Approve / Reject / Show diff buttons. Run the listener on the same machine:
    python3 pipeline/approval_bot.py
Approvals bind to the diff hash at request time; if the workdir changes
afterwards the approval is void and gets re-requested. Only callbacks from
TELEGRAM_USER_ID are honored; everything else is ignored and logged.

No Telegram configured? The pipeline writes APPROVAL_NEEDED with the manual
fallback:  python3 pipeline/run.py approve <task-id>
