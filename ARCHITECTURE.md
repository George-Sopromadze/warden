# WARDEN — Architecture

WARDEN is a deterministic, multi-agent pipeline that turns a plain-English task
into reviewed, tested, human-approved code. Its guiding principle:

> **The repository is the only source of truth. Every stage transition is decided
> by a script, never by a language model's opinion.**

Language models do the *work* (writing specs, code, reviews). Scripts decide
whether that work is good enough to proceed. A human approves the one
irreversible step. This separation is what makes the system safe to run
unattended.

---

## The pipeline at a glance

A task moves through seven fixed stages, in order, and can never skip one:

```
spec → plan → implement → test → review → approve → merge → done
```

| Stage | Who acts | Produces | Gate checks |
|-------|----------|----------|-------------|
| spec | Worker (LLM) | `spec.json` — goal, scope, acceptance criteria | criteria present, executable ones are real commands |
| plan | Worker (LLM) | `plan.json` — task list with files | plan well-formed, files declared |
| implement | Worker (LLM) | `implement.json` + edited files | diff touches only declared files |
| test | Worker (LLM) | `test-report.json` | suite ran; **acceptance criteria re-run by script** |
| review | Reviewer (LLM) | `review.json` | no blocking findings |
| approve | **Human** | `approval.json` | approval exists and is bound to the current diff hash |
| merge | orchestrator | `merge.json` | merge recorded |

After plan, implement, test, and review, an independent **Goal Keeper** also
judges whether the work still serves the original goal.

---

## Core design principles

**1. Scripts gate, models work.** Each stage ends with a gate script in `gates/`.
The gate returns pass/fail by checking the artifact and the repository — not by
asking an LLM. A model can *claim* its tests passed; `gate_acceptance.sh`
independently re-runs them and believes only the result. (This caught a model
fabricating a test pass during development.)

**2. Typed artifacts.** Every stage emits a JSON artifact validated against a
schema in `schemas/`. A malformed or incomplete artifact fails the stage. The
orchestrator writes these files itself — the model returns JSON on stdout, never
writes to disk directly (headless `claude -p` has no write permission by default).

**3. Determinism and resumability.** All task state lives in `state.json`,
written atomically. If the process is killed mid-stage, re-running `run` resumes
from exactly where it stopped. Coordination decisions (which stage is next,
whether to retry, when to escalate) are pure Python — same input, same outcome.

**4. Budgets and circuit breakers.** Each stage has a retry ceiling; the task has
a total-attempt and total-token budget. Two identical failed diffs in a row trips
a stuck-detector. Any limit hit → the task **escalates**: it halts, rolls the
workdir back to the last good commit, and waits for a human. Failure stops the
line; it never loops unattended.

**5. The human boundary.** Only `merge` is irreversible, so only `approve`
requires a human. Approval is requested over Telegram and **bound to the diff
hash** — if the code changes after you approve, the approval is void and is
re-requested. The bot acts only on taps from a whitelisted numeric ID; chat text
is never executed as a command.

**6. Untrusted input is data, never instructions.** Content the agent reads
(files, fetched pages, fixtures) is data. Instructions embedded in it are
reported in the artifact's `flags`, never followed. A `PreToolUse` hook enforces
this at the tool level (see Security).

---

## Directory map

```
warden/
  pipeline/
    run.py            orchestrator — the state machine, agent calls, gates, budgets
    warden.py         observability CLI — report / replay / costs / list
    approval_bot.py   Telegram bot — sends approval requests, receives taps
    diff_hash.sh      computes the diff hash that binds an approval to a diff
  agents/             system-prompt rules per role
    worker.md         spec, plan, implement, test
    reviewer.md       review (judges by reading; no execution tools)
    goalkeeper.md     goal-alignment judge, runs after each agent stage
    dispatcher.md     (reserved for Phase 8 multi-task dispatch)
  gates/              one bash script per stage; the deterministic deciders
    common.sh         shared helpers (json_get, verdict, require_file)
    gate_*.sh         per-stage gates
    gate_acceptance.sh  re-runs executable acceptance criteria from the workdir
  schemas/            JSON Schema for every artifact + state.json
  hooks/
    pretooluse.py     blocks path-escape, secret patterns, unknown-host egress
    scan_secrets.py   audits logs/artifacts for leaked secrets
    notify.sh         Telegram notification helper
  evals/
    eval.py           runs the benchmark suite, scores pass/fail + cost, gates regressions
    benchmarks/       seed tasks with expected outcomes
    baseline.json     known-good reference for regression checks
  tasks/              per-task working dirs (gitignored scratch; not the deliverable)
    <id>/
      task.md         the task description
      state.json      the task's position in the state machine
      run.jsonl       append-only event log (the audit trail)
      artifacts/      the JSON artifact from each stage
      workdir/        the actual code, its own git repo (rollback target)
  .claude/settings.json   wires the PreToolUse and notify hooks into Claude Code
```

Secrets live **outside** the repo, in `~/.warden/secrets.env` (gitignored),
never in any task or commit.

---

## How one task flows (data flow)

1. `run.py new <id> "<description>"` creates `tasks/<id>/` with `task.md`,
   `state.json` (stage = spec), and an empty `workdir/` git repo.
2. `run.py run <id> --agent-mode claude` enters the loop. For each stage:
   - The orchestrator builds a prompt: the role's rules (`agents/<role>.md`) +
     the task + prior artifacts + the required output schema.
   - It calls `claude -p` with the stage's allowed tools and model.
   - It extracts the JSON the model returns, validates it against the schema,
     and writes the artifact.
   - It runs the stage's gate script. Pass → record an atomic transition to the
     next stage. Fail → increment attempts; retry or escalate.
   - After agent stages, it runs the Goal Keeper.
3. At `approve`, the loop parks and sends a Telegram request. It exits.
4. The human taps Approve. `approval_bot.py` records a diff-bound approval and
   re-invokes `run`, which clears the approve gate, merges, and reaches `done`.

Every event — stage start, agent message, gate result, transition, escalation,
approval — is appended to `run.jsonl`. `warden.py report <id>` renders that log
as a readable timeline; `warden.py replay <id> <stage>` reconstructs exactly what
a stage saw.

---

## Models

Roles are assigned models per run via environment variables, so a strong model
can do the reasoning-heavy work while cheap models handle judging:

- `WARDEN_WORKER_MODEL` — spec/plan/implement/test (e.g. Sonnet for hard tasks)
- `WARDEN_REVIEWER_MODEL` — review
- `WARDEN_GK_MODEL` — Goal Keeper

Optional **triple blind review** (`WARDEN_TRIPLE_REVIEW=1`,
`WARDEN_REVIEW_PANEL="m1,m2,m3"`) runs three reviewers independently and decides
by deterministic majority vote (2+ blocking = fail). Off by default; intended for
high-stakes tasks.

---

## Security model (Phase 6)

- **Filesystem confinement.** The `PreToolUse` hook rejects any file tool whose
  path resolves outside the task's `workdir`. Path traversal is caught after
  resolution.
- **Secret blocking.** Tool arguments containing token/key patterns are denied.
- **Egress allowlist.** Bash/web tool calls to hosts not on
  `WARDEN_ALLOWED_HOSTS` (default `api.anthropic.com`) are denied.
- **Fail-closed.** If the hook can't parse an event or doesn't know the workdir,
  it denies.
- **Secret hygiene.** `scan_secrets.py` audits the repo for leaked secrets;
  production credentials never enter an agent's environment.

Validated against a planted prompt-injection: the agent reported the attack in
its `flags` and never touched the secret; the hook independently blocks the same
class of action.

---

## What is intentionally NOT here yet

- **Parallelism / merge queue (Phase 8).** One task runs at a time, single writer
  to state. This is deliberate — sequential execution keeps the system debuggable
  and is sufficient until throughput is the bottleneck. Phase 8 is the most
  dangerous, least valuable phase and is deferred until needed.
- **OS/container isolation.** The agent is confined at the file and tool level by
  the hook, but runs in the host user environment, not a container. Full
  isolation arrives when WARDEN moves to a dedicated always-on machine.
- **Real-money actions.** By design, WARDEN builds and tests only. Anything that
  spends money or touches a live account is a manual human action, with
  credentials kept outside any agent's reach.

---

## Glossary

- **Artifact** — the JSON a stage produces, validated against a schema.
- **Gate** — a bash script that decides pass/fail for a stage. Deterministic.
- **Goal Keeper** — an independent LLM judge of goal alignment, after each agent stage.
- **Escalation** — halt + rollback + wait for human, triggered by any budget/retry limit.
- **Diff-bound approval** — an approval tied to a specific diff hash; voided if the code changes.
- **Workdir** — `tasks/<id>/workdir/`, the agent's sandbox and its own git repo.
