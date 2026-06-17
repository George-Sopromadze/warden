# WARDEN

*Workflow with Agents, Rules, Determinism, Escalation, Notifications*

**Double AI model review (Claude+Gemini)· 3 active agent roles · 7-stage pipeline · 8 deterministic gates · regression eval suite · security-hardened**

**An autonomous AI software pipeline that turns a plain-English task into reviewed, tested, approved code, and never merges anything a human hasn't signed off on, from their phone.**

WARDEN takes a task like *"add a function that calculates a moving average, with tests"* and moves it down a fixed assembly line: `spec → plan → implement → test → review → approve → merge`. At every stage, the decision to advance is made by a script that checks the real repository, not by a model's opinion of its own work. A model can claim its tests passed; WARDEN re-runs them and believes only the result.

The whole pipeline is supervised from a phone. A task pings you over Telegram, you read the actual diff, and you approve or reject by tapping. You can also reject with feedback in plain words, and the system redoes the work to address it. Tasks run on your machine, and you steer them from anywhere.

## What it feels like to use

You set a task in the terminal at home, leave the computer running, and go about your day. WARDEN works on its own. As each stage passes, your phone gets a quiet update over Telegram. When the code is ready, you read the actual diff on your phone and approve it with a tap, or reject it and type in plain words what to change, and the system redoes the work. You are reviewing and directing real code from wherever you happen to be, instead of spending hours, days, or weeks writing or fixing it yourself.

> **The core principle:** the repository is the only source of truth. The trust model isn't "the AI is honest." It's "the AI's claims don't matter, only the facts the scripts verify."

## Why this exists

Most autonomous coding agents ask you to trust the model's self-assessment. WARDEN is built on the opposite assumption: that you should trust verification, not claims. Every stage ends with a gate, a script that re-runs the tests, checks the diff touched only the files it was supposed to, and confirms the work still matches what was asked. Same input, same verdict, every time.

It's built around three ideas that don't usually appear together:

- **Determinism at every gate.** No stage advances because a model said "looks good." A script checks the repository and returns a yes or no.
- **A human at the only irreversible step.** The merge is the one action you can't take back, so it's the one action that asks a person. That approval is tied to the exact diff: change one character after approval and it's automatically voided.
- **A phone as the control surface.** The whole supervision loop is mobile. You direct real code changes in plain language from your pocket.

## How it works

```
  spec ──► plan ──► implement ──► test ──► review ──► approve ──► merge ──► done
   │        │          │           │         │           │          │
   ▼        ▼          ▼           ▼         ▼           ▼          ▼
 [gate]   [gate]    [gate]      [gate]    [gate]    [human +     [gate]
                                                    diff-hash]
```

There are three agent roles, each on a model you choose:

- **Worker** writes the code.
- **Reviewer** judges by reading only. It cannot run code, so it can't talk itself into approving its own assumptions.
- **Goal Keeper** checks, after every stage, that the work still matches what was originally asked.

Between every stage sits a gate: a bash script that inspects the real repository and returns pass or fail. The Worker's report is never trusted on its own. The test gate re-runs the suite and believes only the exit code.

## What's in it

- **Deterministic gates.** Every stage ends with a script that checks the real repository: it runs the tests, verifies the diff stayed in scope, and confirms the work matches the plan.
- **A human gate tied to the diff.** The only irreversible step, the merge, is the only one that asks a human, and the approval is bound to a fingerprint of the exact diff reviewed. You can't accidentally approve something you didn't see.
- **Phone-based control with reject-and-feedback.** Approve, reject, or view the diff from Telegram. Reject asks why, you answer in plain English, and the Worker redoes the code to address it before coming back for fresh approval.
- **Circuit breakers.** Per-stage retry limits, a total token budget, and a stuck-detector that trips on repeated identical failures. The worst case is "halted and you're notified," never an infinite loop or a runaway bill.
- **A regression eval suite.** Real tasks with known-good outcomes. Change a prompt or swap a model, run one command, and it tells you in numbers whether you improved or broke something.
- **Multi-model review with an independent judge.** Every review uses three different model families: Claude and Gemini each review the code independently, then GPT acts as a neutral judge that wrote neither review and checks every concern against the actual code. Because the judge shares no kinship with either reviewer, no model grades its own work. It catches issues a single model misses, and falls back to single-model review if a provider is unavailable.
- **Security that's been attacked.** Each agent is confined to its workspace, secrets are blocked from tool arguments, and network access is allowlisted. I tested it with a planted prompt-injection file telling the agent to leak credentials. The agent detected it, refused, reported it, and nothing moved.
- **Runs unattended.** It starts on boot, restarts itself after a crash, and an external heartbeat alerts your phone if the whole machine goes down.

## Quick start

```bash
pip install -r requirements.txt

# create and run a task (stub mode, no LLM, for testing the machinery)
python3 pipeline/run.py new task-001 "Create a function that reverses a string, with tests"
python3 pipeline/run.py run task-001

# run with real headless Claude Code
python3 pipeline/run.py run task-001 --agent-mode claude
python3 pipeline/run.py status task-001
```

For phone-based approvals, configure Telegram (see Setup) and run the approval listener:

```bash
python3 pipeline/approval_bot.py
```

## Repository layout

```
agents/      one rules file per agent role (worker, reviewer, goalkeeper)
schemas/     JSON schemas for stage artifacts; schema validation is a gate
gates/       one executable script per gate (exit 0 = pass, JSON verdict on stdout)
pipeline/    the orchestrator (run.py), approval bot, supporting tools
hooks/       Claude Code security and notification hooks
evals/       regression benchmark tasks
deploy/      LaunchAgent configs for auto-start and heartbeat monitoring
bin/         operational scripts (bot control, autostart install, heartbeat)
tasks/       one folder per task: task.md, state.json, run.jsonl, artifacts/, workdir/
```

## Setup

Secrets live entirely outside the repository, in `~/.warden/secrets.env` (which is gitignored), and are loaded only by the orchestrator, never into an agent's prompt.

```bash
mkdir -p ~/.warden && cp secrets.env.example ~/.warden/secrets.env
# then fill in the values
```

For phone approvals, set up Telegram: message [@BotFather](https://t.me/botfather), run `/newbot`, and copy the token into `TELEGRAM_BOT_TOKEN`. Get your numeric user id from [@userinfobot](https://t.me/userinfobot) and put it in `TELEGRAM_USER_ID`.

For heartbeat monitoring (optional), create a check at [healthchecks.io](https://healthchecks.io) and put its ping URL in `HEALTHCHECK_URL`.

Per-project gate commands are set through environment variables (`WARDEN_LINT_CMD`, `WARDEN_BUILD_CMD`, `WARDEN_TEST_CMD`), so the gates run your tooling. The test gate re-runs your real suite and trusts the script, not the agent's report.

To run unattended, `bin/install-autostart` registers a LaunchAgent so the approval bot starts at login and restarts if it crashes. The heartbeat agent pings your monitor every few minutes while the bot is alive.

## Design notes

A few decisions worth understanding:

- **Crash-safe by construction.** All state lives in `tasks/<id>/state.json`. Kill the orchestrator mid-stage and re-run, and it resumes from the last recorded stage. There's no in-memory state to lose.
- **Rollback on failure.** When a stage exhausts its retries, the workdir is rolled back to the last good commit and the task halts for a human, rather than leaving a half-finished change.
- **The Reviewer can't run code, on purpose.** Forcing it to judge by reading prevents it from rationalising its own assumptions by executing them.
- **The eval suite gates changes to WARDEN itself.** Modify a prompt or swap a model, and the benchmark tells you in numbers whether the change helped or hurt. It's the same "verify, don't trust" idea turned inward.

## What it's deliberately not

In the interest of being honest about scope:

- **It's not a replacement for Copilot, Cursor, or Claude Code at your day job.** Those are more capable and have teams behind them. WARDEN is a personally-owned, readable, phone-controlled implementation of patterns serious teams have independently converged on, built small enough to understand every line.
- **It doesn't run tasks in parallel yet.** One task at a time. Parallel worktrees and a merge queue are the obvious next step, and the one I deliberately deferred. Sequential is debuggable; parallel is where subtle bugs hide.
- **It never touches money or anything irreversible on its own.** It builds and tests. The dangerous calls stay human. It's aimed at financial software eventually, which is exactly why credentials live entirely outside the agents' reach by design.

---

© 2026 Georgy Sopromadze. Licensed under the MIT License.
