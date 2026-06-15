# WARDEN

**An autonomous AI software pipeline that turns a plain-English task into reviewed, tested, approved code — and never merges anything a human didn't sign off on, from their phone.**

WARDEN takes a task like *"add a function that calculates a moving average, with tests"* and moves it down a fixed assembly line — `spec → plan → implement → test → review → approve → merge` — where **every stage transition is decided by a script that checks the real repository, never by a model's opinion of its own work.** A model can *claim* its tests passed; WARDEN re-runs them and believes only the result.

The entire pipeline is supervised from a phone. A task pings you over Telegram, you read the actual diff, and you approve or reject by tapping — or reject *with feedback in plain words*, and the system redoes the work to address it. Tasks run on your machine; you steer them from anywhere.

> **The core principle:** the repository is the only source of truth. The trust model isn't "the AI is honest" — it's "the AI's claims don't matter; only the facts the scripts verify."

---

## Why this exists

Most autonomous coding agents ask you to trust the model's self-assessment. WARDEN is built on the opposite assumption: that you should trust *verification*, not *claims*. Every stage ends with a deterministic gate — a script that re-runs the tests, checks the diff touched only declared files, and confirms the work still matches what was asked. Same input, same verdict, every time.

It's designed around three ideas that don't usually appear together:

- **Determinism at every gate.** No stage advances on a model saying "looks good." A script checks the repo and returns a binary verdict.
- **A human at the only irreversible step.** The merge — the one action you can't take back — is the one action that asks a person. That approval is cryptographically bound to the exact diff: change one character after approval and it's automatically voided.
- **A phone as the control surface.** The whole supervision loop is mobile. You direct real code changes in plain language from your pocket.

---

## How it works

```
  spec ──► plan ──► implement ──► test ──► review ──► approve ──► merge ──► done
   │        │          │           │         │           │          │
   ▼        ▼          ▼           ▼         ▼           ▼          ▼
 [gate]   [gate]    [gate]      [gate]    [gate]    [human +     [gate]
                                                    diff-hash]
```

Three agent roles, each on a model you choose:

- **Worker** writes the code.
- **Reviewer** judges by reading only — it literally cannot run code, so it can't talk itself into approving its own assumptions.
- **Goal Keeper** checks, after every stage, that the work still matches what was originally asked.

Between every stage sits a **deterministic gate**: a bash script that inspects the real repository and returns pass/fail. The Worker's report is never trusted on its own — the test gate re-runs the suite and believes only the exit code.

---

## What's in it

- **Deterministic gates** — every stage ends with a script that checks the real repo (runs tests, verifies the diff stayed in scope, confirms the work matches the plan). No vibes, just verdicts.
- **A human gate bound to the diff** — the only irreversible step (merge) is the only one that asks a human, and the approval is bound to a fingerprint of the exact diff reviewed. You can't accidentally approve something you didn't see.
- **Phone-based control with reject-and-feedback** — approve, reject, or view the diff from Telegram. Reject asks *why*; you answer in plain English and the Worker redoes the code to address it, then returns for fresh approval.
- **Circuit breakers** — per-stage retry limits, a total token budget, and a stuck-detector that trips on repeated identical failures. The worst case is "halted and you're notified" — never an infinite loop or a runaway bill.
- **A regression eval suite** — real tasks with known-good outcomes. Change a prompt or swap a model, run one command, and it tells you in numbers whether you improved or broke something.
- **Security that's been attacked** — each agent is confined to its workspace, secrets are blocked from tool arguments, and network egress is allowlisted. Tested against a planted prompt-injection file instructing the agent to exfiltrate credentials: the agent detected it, refused, reported it, and nothing moved.
- **Runs unattended** — auto-starts on boot, restarts itself on a crash, and an external heartbeat alerts your phone if the whole machine goes down.

---

## Quick start

```bash
pip install -r requirements.txt

# create and run a task (stub mode — no LLM, for testing the machinery)
python3 pipeline/run.py new task-001 "Create a function that reverses a string, with tests"
python3 pipeline/run.py run task-001

# run with real headless Claude Code
python3 pipeline/run.py run task-001 --agent-mode claude
python3 pipeline/run.py status task-001
```

For phone-based approvals, configure Telegram (see [Setup](#setup)) and run the approval listener:

```bash
python3 pipeline/approval_bot.py
```

---

## Repository layout

```
agents/      one rules file per agent role (worker, reviewer, goalkeeper)
schemas/     JSON schemas for stage artifacts — schema validation IS a gate
gates/       one executable script per gate (exit 0 = pass, JSON verdict on stdout)
pipeline/    the orchestrator (run.py), approval bot, supporting tools
hooks/       Claude Code security + notification hooks
evals/       regression benchmark tasks
deploy/      LaunchAgent configs for auto-start and heartbeat monitoring
bin/         operational scripts (bot control, autostart install, heartbeat)
tasks/       one folder per task: task.md, state.json, run.jsonl, artifacts/, workdir/
```

---

## Setup

**Secrets** live entirely outside the repository, in `~/.warden/secrets.env` (gitignored), and are loaded only by the orchestrator — never into an agent's prompt.

```bash
mkdir -p ~/.warden && cp secrets.env.example ~/.warden/secrets.env
# then fill in the values
```

**Telegram (for phone approvals):**
1. Message [@BotFather](https://t.me/botfather) → `/newbot` → copy the token into `TELEGRAM_BOT_TOKEN`.
2. Get your numeric user id from [@userinfobot](https://t.me/userinfobot) → put it in `TELEGRAM_USER_ID`.

**Heartbeat monitoring (optional):** create a check at [healthchecks.io](https://healthchecks.io), and put its ping URL in `HEALTHCHECK_URL`.

**Per-project gate commands** (so the gates run *your* tooling) are set via environment variables: `WARDEN_LINT_CMD`, `WARDEN_BUILD_CMD`, `WARDEN_TEST_CMD`. The test gate re-runs your real suite — it trusts the script, not the agent's report.

**Always-on:** `bin/install-autostart` registers a LaunchAgent so the approval bot starts at login and restarts if it crashes; the heartbeat agent pings your monitor every few minutes while the bot is alive.

---

## Design notes

A few decisions worth understanding:

- **Crash-safe by construction.** All state lives in `tasks/<id>/state.json`. Kill the orchestrator mid-stage and re-run — it resumes from the last recorded stage. There's no in-memory state to lose.
- **Rollback on failure.** When a stage exhausts its retries, the workdir is rolled back to the last good commit and the task halts for a human, rather than leaving a half-finished change.
- **Reviewer can't run code, on purpose.** Forcing the Reviewer to judge by reading prevents it from rationalizing its own assumptions by executing them.
- **The eval suite gates changes to WARDEN itself.** Modify a prompt or swap a model, and the benchmark tells you in numbers whether the change helped or hurt — the same "verify, don't trust" discipline, turned inward.

---

## What it's deliberately not

In the spirit of being honest about scope:

- **It's not a replacement for Copilot, Cursor, or Claude Code at your day job.** Those are more capable and have teams behind them. WARDEN is a personally-owned, readable, phone-controlled implementation of patterns serious teams have independently converged on — built small enough to understand every line.
- **It doesn't run tasks in parallel yet.** One task at a time. Parallel worktrees and a merge queue are the obvious next step, deliberately deferred — sequential is debuggable; parallel is where subtle bugs hide.
- **It never touches money or anything irreversible on its own.** It builds and tests; the dangerous calls stay human. (It's aimed at financial software eventually — which is exactly why credentials live entirely outside the agents' reach by design.)

---

*WARDEN: autonomous where it's safe, human where it counts.*
