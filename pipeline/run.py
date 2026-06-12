#!/usr/bin/env python3
"""
WARDEN orchestrator loop — Phase 1.

Moves a task through fixed stages. Every stage:
  1. runs an agent (Claude Code headless, or a deterministic stub for testing),
  2. validates the produced artifact against its JSON schema (validation IS a gate),
  3. runs the stage's gate script from gates/,
  4. on pass, atomically advances state.json (temp file -> fsync -> rename).

Resumability rule: this process must be killable at any instant. On startup it
reads state.json and continues from the recorded stage. Nothing is held only
in memory.

Usage:
    python3 pipeline/run.py new <task-id> "task description"
    python3 pipeline/run.py run <task-id> [--agent-mode stub|claude]
    python3 pipeline/run.py status <task-id>

Phase 2 hooks (attempts / max_attempts / escalation / diff-hash stuck detection)
are stubbed where marked TODO(phase2) so the state format won't need migrating.
"""

import argparse
import hashlib
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

try:
    import jsonschema
except ImportError:
    jsonschema = None  # falls back to _mini_validate (subset validator below)

ROOT = Path(__file__).resolve().parent.parent
SCHEMAS_DIR = ROOT / "schemas"
GATES_DIR = ROOT / "gates"
AGENTS_DIR = ROOT / "agents"
TASKS_DIR = ROOT / "tasks"

STAGES = ["spec", "plan", "implement", "test", "review", "approve", "merge"]

# stage -> (agent role, artifact filename, schema filename, gate script)
# agent None  => orchestrator-only stage (gate script does the work).
STAGE_CONFIG = {
    "spec":      ("worker",   "spec.json",        "spec.schema.json",        "gate_spec.sh"),
    "plan":      ("worker",   "plan.json",        "plan.schema.json",        "gate_plan.sh"),
    "implement": ("worker",   "implement.json",   "implement.schema.json",   "gate_implement.sh"),
    "test":      ("worker",   "test-report.json", "test-report.schema.json", "gate_test.sh"),
    "review":    ("reviewer", "review.json",      "review.schema.json",      "gate_review.sh"),
    "approve":   (None,       "approval.json",    None,                      "gate_approve.sh"),
    "merge":     (None,       "merge.json",       None,                      "gate_merge.sh"),
}

DEFAULT_MAX_ATTEMPTS = 3  # TODO(phase2): consumed by retry loop


# ----------------------------------------------------------------------------
# State: atomic, append-only logging, resumable
# ----------------------------------------------------------------------------

def task_dir(task_id: str) -> Path:
    return TASKS_DIR / task_id


def state_path(task_id: str) -> Path:
    return task_dir(task_id) / "state.json"


def atomic_write_json(path: Path, data: dict) -> None:
    """Write temp file in same dir, fsync, rename, fsync dir. Survives kill -9."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    dir_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def load_state(task_id: str) -> dict:
    with open(state_path(task_id)) as f:
        return json.load(f)


def save_state(task_id: str, state: dict) -> None:
    atomic_write_json(state_path(task_id), state)


def log_event(task_id: str, event_type: str, payload: dict) -> None:
    """Append-only run log (Phase 5 builds reports on top of this)."""
    line = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "event": event_type,
        **payload,
    }
    with open(task_dir(task_id) / "run.jsonl", "a") as f:
        f.write(json.dumps(line) + "\n")
        f.flush()
        os.fsync(f.fileno())


# ----------------------------------------------------------------------------
# Agents
# ----------------------------------------------------------------------------

def run_agent(task_id: str, stage: str, role: str, mode: str) -> None:
    """Produce the stage artifact, either via Claude Code headless or a stub."""
    artifact_name = STAGE_CONFIG[stage][1]
    artifact_path = task_dir(task_id) / "artifacts" / artifact_name
    artifact_path.parent.mkdir(parents=True, exist_ok=True)

    if mode == "stub":
        _stub_agent(task_id, stage, artifact_path)
        log_event(task_id, "agent_message", {"stage": stage, "agent": "stub"})
        return

    rules = (AGENTS_DIR / f"{role}.md").read_text()
    task_desc = (task_dir(task_id) / "task.md").read_text()
    prior = _prior_artifacts_summary(task_id, stage)

    prompt = (
        f"WARDEN stage: {stage}\n"
        f"Task folder: {task_dir(task_id)}\n\n"
        f"## Task\n{task_desc}\n\n"
        f"## Prior stage artifacts\n{prior}\n\n"
        f"Produce the `{stage}` stage artifact and write it as valid JSON "
        f"(schema: schemas/{STAGE_CONFIG[stage][2]}) to:\n{artifact_path}\n"
        f"Do the stage's actual work first; the artifact describes what you did."
    )

    # Headless Claude Code. Verify flags against `claude --help` on your box;
    # current docs: -p (print mode) + --append-system-prompt + --output-format json.
    cmd = [
        "claude", "-p", prompt,
        "--append-system-prompt", rules,
        "--output-format", "json",
    ]
    log_event(task_id, "agent_start", {"stage": stage, "role": role,
                                       "cmd": " ".join(shlex.quote(c) for c in cmd[:2])})
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          cwd=task_dir(task_id) / "workdir")
    usage = _extract_usage(proc.stdout)
    log_event(task_id, "agent_message", {
        "stage": stage, "role": role, "exit": proc.returncode,
        "stdout_tail": proc.stdout[-2000:], "stderr_tail": proc.stderr[-1000:],
        "usage": usage,
    })
    if usage:
        _accumulate_tokens(task_id, usage)
    if proc.returncode != 0:
        raise StageFailure(f"agent exited {proc.returncode}")


def _extract_usage(stdout: str) -> dict:
    """Best-effort token/cost capture from `--output-format json` (Phase 5 expands)."""
    try:
        data = json.loads(stdout)
        return {k: data[k] for k in ("usage", "total_cost_usd", "duration_ms")
                if k in data}
    except (json.JSONDecodeError, TypeError):
        return {}


def _accumulate_tokens(task_id: str, usage: dict) -> None:
    state = load_state(task_id)
    budget = state.setdefault("budget", {})
    u = usage.get("usage", {})
    spent = u.get("input_tokens", 0) + u.get("output_tokens", 0)
    budget["tokens_spent"] = budget.get("tokens_spent", 0) + spent
    save_state(task_id, state)
    # TODO(phase2): if tokens_spent > max_token_spend -> escalate()


def _prior_artifacts_summary(task_id: str, stage: str) -> str:
    parts = []
    for s in STAGES[: STAGES.index(stage)]:
        p = task_dir(task_id) / "artifacts" / STAGE_CONFIG[s][1]
        if p.exists():
            parts.append(f"### {s}\n```json\n{p.read_text().strip()}\n```")
    return "\n".join(parts) or "(none)"


def _stub_agent(task_id: str, stage: str, artifact_path: Path) -> None:
    """Deterministic artifacts so the pipeline is testable without an LLM."""
    stubs = {
        "spec": {
            "goal": f"Toy goal for {task_id}: create README with required content",
            "scope": ["README.md"],
            "out_of_scope": ["everything else"],
            "acceptance_criteria": [
                {"id": "AC-1", "type": "executable",
                 "check": "test -f workdir/README.md", "expected": "exit 0"},
                {"id": "AC-2", "type": "judgment",
                 "check": "Is the README understandable to a newcomer?"},
            ],
        },
        "plan": {
            "tasks": [{"id": "T-1", "description": "Write the README",
                       "files": ["README.md"], "depends_on": []}],
        },
        "implement": {
            "summary": "Created README.md with the requested content",
            "files_changed": ["README.md"],
            "diff_hash": hashlib.sha256(b"stub-diff").hexdigest(),
        },
        "test": {"suites_run": 1, "passed": 1, "failed": 0, "coverage": 100.0},
        "review": {"verdict": "approve", "findings": [], "blocking": False},
    }
    if stage == "implement":  # actually do the toy work
        wd = task_dir(task_id) / "workdir"
        wd.mkdir(exist_ok=True)
        (wd / "README.md").write_text(f"# {task_id}\n\nCreated by WARDEN stub agent.\n")
    if os.environ.get("WARDEN_STUB_BREAK") == stage:
        # Deliberately invalid artifact — for testing schema-gate rejection
        # and the escalation path without an LLM in the loop.
        atomic_write_json(artifact_path, {"deliberately": "broken"})
        return
    atomic_write_json(artifact_path, stubs[stage])


# ----------------------------------------------------------------------------
# Validation + gates
# ----------------------------------------------------------------------------

class StageFailure(Exception):
    pass


def _mini_validate(obj, schema, path="$"):
    """Tiny fallback validator covering the subset these schemas use.
    Prefer `pip install jsonschema`; this exists so the pipeline still hard-fails
    on bad artifacts when the dependency is missing."""
    t = schema.get("type")
    types = {"object": dict, "array": list, "string": str,
             "integer": int, "number": (int, float), "boolean": bool}
    if t:
        allowed = t if isinstance(t, list) else [t]
        ok = any(
            (obj is None) if a == "null"
            else isinstance(obj, types[a]) and not (a in ("integer", "number") and isinstance(obj, bool))
            for a in allowed
        )
        if not ok:
            raise StageFailure(f"{path}: expected {t}")
    if "enum" in schema and obj not in schema["enum"]:
        raise StageFailure(f"{path}: {obj!r} not in {schema['enum']}")
    if isinstance(obj, str):
        if len(obj) < schema.get("minLength", 0):
            raise StageFailure(f"{path}: shorter than minLength")
        if "pattern" in schema:
            import re
            if not re.search(schema["pattern"], obj):
                raise StageFailure(f"{path}: does not match {schema['pattern']}")
    if isinstance(obj, (int, float)) and not isinstance(obj, bool):
        if obj < schema.get("minimum", float("-inf")):
            raise StageFailure(f"{path}: below minimum")
        if obj > schema.get("maximum", float("inf")):
            raise StageFailure(f"{path}: above maximum")
    if isinstance(obj, dict):
        for req in schema.get("required", []):
            if req not in obj:
                raise StageFailure(f"{path}: missing required key '{req}'")
        props = schema.get("properties", {})
        addl = schema.get("additionalProperties", True)
        for k, v in obj.items():
            if k in props:
                _mini_validate(v, props[k], f"{path}.{k}")
            elif isinstance(addl, dict):
                _mini_validate(v, addl, f"{path}.{k}")
            elif addl is False:
                raise StageFailure(f"{path}: unexpected key '{k}'")
    if isinstance(obj, list):
        if len(obj) < schema.get("minItems", 0):
            raise StageFailure(f"{path}: fewer than minItems")
        if "items" in schema:
            for i, item in enumerate(obj):
                _mini_validate(item, schema["items"], f"{path}[{i}]")


def validate_artifact(task_id: str, stage: str) -> None:
    """Schema validation is itself a gate: invalid artifact = stage failed."""
    _, artifact_name, schema_name, _ = STAGE_CONFIG[stage]
    if schema_name is None:
        return
    artifact_path = task_dir(task_id) / "artifacts" / artifact_name
    if not artifact_path.exists():
        raise StageFailure(f"agent produced no artifact: {artifact_name}")
    try:
        artifact = json.loads(artifact_path.read_text())
    except json.JSONDecodeError as e:
        raise StageFailure(f"artifact is not valid JSON: {e}")
    schema = json.loads((SCHEMAS_DIR / schema_name).read_text())
    if jsonschema is not None:
        try:
            jsonschema.validate(artifact, schema)
        except jsonschema.ValidationError as e:
            raise StageFailure(f"schema violation in {artifact_name}: {e.message}")
    else:
        try:
            _mini_validate(artifact, schema)
        except StageFailure as e:
            raise StageFailure(f"schema violation in {artifact_name}: {e}")
    log_event(task_id, "schema_valid", {"stage": stage, "artifact": artifact_name})


def run_gate(task_id: str, stage: str) -> None:
    gate = STAGE_CONFIG[stage][3]
    proc = subprocess.run(
        [str(GATES_DIR / gate), str(task_dir(task_id))],
        capture_output=True, text=True,
    )
    try:
        gate_verdict = json.loads(proc.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        gate_verdict = {"gate": gate, "pass": proc.returncode == 0,
                        "reason": "no JSON verdict on stdout"}
    log_event(task_id, "gate_result", {"stage": stage, **gate_verdict,
                                       "exit": proc.returncode})
    if proc.returncode != 0:
        raise StageFailure(f"{gate} failed: {gate_verdict.get('reason', '?')}")


def commit_artifact(task_id: str, stage: str) -> None:
    """Commit the stage artifact to the task branch if the task dir is git-tracked."""
    td = task_dir(task_id)
    if not (ROOT / ".git").exists():
        return
    artifact = td / "artifacts" / STAGE_CONFIG[stage][1]
    subprocess.run(["git", "add", str(artifact)], cwd=ROOT, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", f"warden({task_id}): {stage} artifact", "--no-verify"],
        cwd=ROOT, capture_output=True,
    )


# ----------------------------------------------------------------------------
# Escalation (Phase 2 fills this out; the call sites exist now)
# ----------------------------------------------------------------------------

def escalate(task_id: str, reason: str) -> None:
    state = load_state(task_id)
    state["status"] = "escalated"
    save_state(task_id, state)
    log_event(task_id, "escalation", {"reason": reason})
    (task_dir(task_id) / "NEEDS_HUMAN").write_text(reason + "\n")
    # TODO(phase2): roll back task branch to last good commit
    # TODO(phase4): hooks/notify.sh -> Telegram
    print(f"[warden] ESCALATED {task_id}: {reason}", file=sys.stderr)


# ----------------------------------------------------------------------------
# Main loop
# ----------------------------------------------------------------------------

def cmd_new(task_id: str, description: str) -> None:
    td = task_dir(task_id)
    if td.exists():
        sys.exit(f"task {task_id} already exists")
    (td / "artifacts").mkdir(parents=True)
    (td / "workdir").mkdir()
    (td / "task.md").write_text(description + "\n")
    state = {
        "task_id": task_id,
        "stage": STAGES[0],
        "status": "pending",
        "stages": {s: {"attempts": 0, "max_attempts": DEFAULT_MAX_ATTEMPTS,
                       "last_diff_hash": None} for s in STAGES},
        "budget": {"max_total_attempts": 15, "max_token_spend": 2_000_000,
                   "total_attempts": 0, "tokens_spent": 0},
    }
    atomic_write_json(td / "state.json", state)
    log_event(task_id, "created", {"description": description})
    print(f"[warden] created {task_id} at stage '{STAGES[0]}'")


def cmd_run(task_id: str, agent_mode: str) -> None:
    state = load_state(task_id)
    if state["status"] == "escalated":
        sys.exit(f"{task_id} is escalated — resolve NEEDS_HUMAN first")
    if state["status"] == "done":
        print(f"[warden] {task_id} already done")
        return

    # Resumability: continue from whatever stage state.json records.
    while state["stage"] != "done":
        stage = state["stage"]
        role = STAGE_CONFIG[stage][0]
        print(f"[warden] {task_id} :: stage '{stage}'")

        state["status"] = "running"
        state["stages"][stage]["attempts"] += 1  # recorded BEFORE work: a crash
        state["budget"]["total_attempts"] += 1   # mid-stage still counts the attempt
        save_state(task_id, state)
        log_event(task_id, "stage_start",
                  {"stage": stage, "attempt": state["stages"][stage]["attempts"]})

        try:
            if role is not None:
                run_agent(task_id, stage, role, agent_mode)
            validate_artifact(task_id, stage)
            run_gate(task_id, stage)
        except StageFailure as e:
            log_event(task_id, "stage_failed", {"stage": stage, "reason": str(e)})
            # TODO(phase2): retry while attempts < max_attempts; diff-hash stuck
            # detection; only then escalate. Phase 1 escalates on first failure.
            escalate(task_id, f"stage '{stage}' failed: {e}")
            return

        commit_artifact(task_id, stage)
        nxt = STAGES[STAGES.index(stage) + 1] if stage != STAGES[-1] else "done"
        state = load_state(task_id)  # reload: agent path may have updated budget
        state["stage"] = nxt
        state["status"] = "done" if nxt == "done" else "pending"
        save_state(task_id, state)   # atomic transition — the crash-safe moment
        log_event(task_id, "transition", {"from": stage, "to": nxt})

    print(f"[warden] {task_id} complete")


def cmd_status(task_id: str) -> None:
    print(json.dumps(load_state(task_id), indent=2))


def main() -> None:
    ap = argparse.ArgumentParser(prog="warden")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_new = sub.add_parser("new", help="create a task")
    p_new.add_argument("task_id")
    p_new.add_argument("description")

    p_run = sub.add_parser("run", help="run/resume a task")
    p_run.add_argument("task_id")
    p_run.add_argument("--agent-mode", choices=["stub", "claude"], default="stub",
                       help="stub = deterministic test artifacts; claude = headless Claude Code")

    p_st = sub.add_parser("status", help="print state.json")
    p_st.add_argument("task_id")

    args = ap.parse_args()
    if args.cmd == "new":
        cmd_new(args.task_id, args.description)
    elif args.cmd == "run":
        cmd_run(args.task_id, args.agent_mode)
    elif args.cmd == "status":
        cmd_status(args.task_id)


if __name__ == "__main__":
    main()
