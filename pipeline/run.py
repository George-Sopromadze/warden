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


# State: atomic, append-only logging, resumable

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


# Agents

def run_agent(task_id: str, stage: str, role: str, mode: str) -> dict:
    """Produce the stage artifact via Claude Code headless or a stub.
    Returns a usage dict (empty for stub) for the caller to add to the budget."""
    artifact_name = STAGE_CONFIG[stage][1]
    artifact_path = task_dir(task_id) / "artifacts" / artifact_name
    artifact_path.parent.mkdir(parents=True, exist_ok=True)

    if mode == "stub":
        _stub_agent(task_id, stage, artifact_path)
        log_event(task_id, "agent_message", {"stage": stage, "agent": "stub"})
        return {}

    rules = (AGENTS_DIR / f"{role}.md").read_text()
    task_desc = (task_dir(task_id) / "task.md").read_text()
    prior = _prior_artifacts_summary(task_id, stage)
    schema_name = STAGE_CONFIG[stage][2]
    schema_text = ""
    if schema_name:
        schema_text = (SCHEMAS_DIR / schema_name).read_text()

    feedback_path = task_dir(task_id) / "feedback.md"
    feedback_block = ""
    if feedback_path.exists():
        feedback_block = (
            f"## Human reviewer feedback — you MUST apply this\n"
            f"A human reviewed the previous attempt and asked for the change "
            f"below. This feedback is now a REQUIRED part of the task — apply it "
            f"to the code even if it extends slightly beyond the original spec. "
            f"Modify the existing declared files (the .py files in the plan) to "
            f"satisfy it. Do NOT create new files not in the plan (no READMEs).\n"
            f"<feedback_content>\n"
            f"{feedback_path.read_text().strip()}\n"
            f"</feedback_content>\n\n"
        )

    prompt = (
        f"WARDEN stage: {stage}\n"
        f"Task folder: {task_dir(task_id)}\n\n"
        f"## Task\n{task_desc}\n\n"
        f"{feedback_block}"
        f"## Prior stage artifacts\n{prior}\n\n"
        f"## Required output schema (your JSON MUST conform exactly to this)\n"
        f"```json\n{schema_text}\n```\n\n"
        f"Do the stage's actual work now (read/edit files in the working "
        f"directory as needed), then return the `{stage}` artifact as a single "
        f"JSON object conforming EXACTLY to the schema above — use those exact "
        f"field names, no extra fields. Return ONLY the JSON object, nothing else."
    )

    # Headless Claude Code. We DON'T ask the model to write the artifact file
    # (non-interactive -p has no write permission by default); instead it
    # returns structured output and the orchestrator writes the file. When a
    # schema exists we pass it via --json-schema so the CLI enforces the shape.
    cmd = ["claude", "-p", prompt,
           "--append-system-prompt", rules,
           "--output-format", "json"]
    model = os.environ.get(
        "WARDEN_WORKER_MODEL" if role == "worker" else "WARDEN_REVIEWER_MODEL")
    if model:
        cmd += ["--model", model]
    # NOTE: --json-schema was tried here but hangs in Claude Code 2.1.x, so we
    # rely on _extract_artifact (parse .result, strip fences) + the orchestrator's
    # own validate_artifact for schema enforcement. Same guarantee, gate-side.
    # Implement stage needs to actually edit files; allow the edit tools and
    # confine work to the task workdir (Phase 6 will tighten this further).
    if stage == "implement":
        cmd += ["--allowedTools", "Read,Write,Edit,Bash,Grep,Glob"]

    log_event(task_id, "agent_start", {"stage": stage, "role": role,
                                       "cmd": " ".join(shlex.quote(c) for c in cmd[:2])})
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              cwd=task_dir(task_id) / "workdir",
                              timeout=int(os.environ.get("WARDEN_AGENT_TIMEOUT", "180")))
    except subprocess.TimeoutExpired:
        raise StageFailure("agent call timed out "
                           f"({os.environ.get('WARDEN_AGENT_TIMEOUT', '180')}s)")
    usage = _extract_usage(proc.stdout)
    log_event(task_id, "agent_message", {
        "stage": stage, "role": role, "exit": proc.returncode,
        "stdout_tail": proc.stdout[-2000:], "stderr_tail": proc.stderr[-1000:],
        "usage": usage,
    })
    if proc.returncode != 0:
        raise StageFailure(f"agent exited {proc.returncode}: {proc.stderr.strip()[:200]}")

    # Extract the artifact and write it ourselves (deterministic).
    if schema_name:
        artifact_obj = _extract_artifact(proc.stdout)
        if artifact_obj is None:
            raise StageFailure("could not extract structured artifact from agent output")
        artifact_obj = _normalize_artifact(stage, artifact_obj)
        atomic_write_json(artifact_path, artifact_obj)
    return usage


def _normalize_artifact(stage, obj):
    """Coerce reasonable agent shorthand into the schema shape before validation.

    Review agents sometimes emit a finding as a bare string sentence rather than
    the required {"severity","description"} object. A valid, approving review
    should not be discarded over that format slip, so we wrap string findings as
    info-level findings. Object findings pass through untouched.
    """
    if stage == "review" and isinstance(obj, dict):
        findings = obj.get("findings")
        if isinstance(findings, list):
            normed = []
            for f in findings:
                if isinstance(f, str):
                    desc = f if len(f) >= 5 else (f + " .")[:max(5, len(f) + 2)]
                    normed.append({"severity": "info", "description": desc})
                else:
                    normed.append(f)
            obj["findings"] = normed
        # Map verdict synonyms to the canonical enum. Reviewers asking for
        # changes sometimes say "reject"/"deny"; the schema only accepts
        # "approve" / "request_changes". Normalize so a substantive rejection
        # is recorded correctly rather than escalating on a word choice.
        v = obj.get("verdict")
        if isinstance(v, str):
            vl = v.strip().lower()
            if vl in ("reject", "rejected", "deny", "denied", "changes_requested",
                      "request_change", "needs_changes", "fail", "failed"):
                obj["verdict"] = "request_changes"
            elif vl in ("approved", "accept", "accepted", "pass", "passed", "lgtm"):
                obj["verdict"] = "approve"
    return obj


def _extract_artifact(stdout: str):
    try:
        env = json.loads(stdout)
    except json.JSONDecodeError:
        return None
    if isinstance(env, dict) and env.get("structured_output") is not None:
        return env["structured_output"]
    text = env.get("result") if isinstance(env, dict) else None
    if not isinstance(text, str):
        return None
    text = text.strip()
    # 0. Prefer a fenced ```json ... ``` block ANYWHERE in the text. Agents often
    #    wrap the JSON in explanatory prose, and that prose can contain stray { }
    #    (e.g. f-strings like {name}) that fool the brace-span fallback below.
    import re as _re
    _m = _re.search(r"```(?:json)?\s*\n(.*?)```", text, _re.S)
    if _m:
        try:
            return json.loads(_m.group(1).strip())
        except json.JSONDecodeError:
            pass
    # 1. Strip a leading ```json / ``` fence and trailing ``` if present.
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
    # 2. Try direct parse.
    try:
        return json.loads(text.strip())
    except json.JSONDecodeError:
        pass
    # 3. Final fallback: parse the widest {...} span. Survives any preamble,
    #    trailing prose, or stray fence the steps above missed.
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            return None
    return None


def _extract_usage(stdout: str) -> dict:
    """Best-effort token/cost capture from `--output-format json` (Phase 5 expands)."""
    try:
        data = json.loads(stdout)
        return {k: data[k] for k in ("usage", "total_cost_usd", "duration_ms")
                if k in data}
    except (json.JSONDecodeError, TypeError):
        return {}


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
        target = "accounts.md" if os.environ.get("WARDEN_STUB_MISALIGN") else "README.md"
        (wd / target).write_text(f"# {task_id}\n\nCreated by WARDEN stub agent.\n")
        stubs["implement"]["files_changed"] = [target]
    if stage == "plan" and os.environ.get("WARDEN_STUB_MISALIGN"):
        # Mis-instructed coder scenario: plans (and builds) the WRONG file.
        # Spec still says README.md — only the Goal Keeper can catch this,
        # because plan and implement are internally consistent with each other.
        stubs["plan"]["tasks"][0]["files"] = ["accounts.md"]
    if stage == "spec":
        # acceptance.md: the written contract (Phase 3). Same criteria as
        # spec.json, in the human-readable form gate_spec now requires.
        lines = ["# Acceptance criteria\n"]
        for c in stubs["spec"]["acceptance_criteria"]:
            lines.append(f"- **{c['id']}** ({c['type']}): {c['check']}")
        (task_dir(task_id) / "artifacts" / "acceptance.md").write_text(
            "\n".join(lines) + "\n")
    if os.environ.get("WARDEN_STUB_BREAK") == stage:
        # Deliberately invalid artifact — for testing schema-gate rejection
        # and the escalation path without an LLM in the loop.
        atomic_write_json(artifact_path, {"deliberately": "broken"})
        return
    atomic_write_json(artifact_path, stubs[stage])


# Validation + gates

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
    run_named_gate(task_id, stage, STAGE_CONFIG[stage][3])


def run_named_gate(task_id: str, stage: str, gate: str) -> None:
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


# Goal Keeper (Phase 3): judges against the written contract, never fixes

GOALKEEPER_STAGES = {"plan", "implement", "test", "review"}  # agent stages after spec


def run_goalkeeper(task_id: str, stage: str, mode: str) -> dict:
    """Run after a stage's gate passes. on_track: false => StageFailure, which
    flows into the normal Phase 2 retry/escalation machinery. Returns usage."""
    out_path = task_dir(task_id) / "artifacts" / f"goalkeeper-{stage}.json"
    usage = {}

    if mode == "stub":
        _stub_goalkeeper(task_id, stage, out_path)
    else:
        rules = (AGENTS_DIR / "goalkeeper.md").read_text()
        task_desc = (task_dir(task_id) / "task.md").read_text()
        spec = (task_dir(task_id) / "artifacts" / "spec.json").read_text()
        latest = (task_dir(task_id) / "artifacts" / STAGE_CONFIG[stage][1]).read_text()
        gk_schema = (SCHEMAS_DIR / "goalkeeper.schema.json").read_text()
        prompt = (
            f"Goal Keeper check after stage: {stage}\n\n"
            f"## Original task\n{task_desc}\n\n"
            f"## Acceptance criteria (from spec.json)\n{spec}\n\n"
            f"## Latest artifact\n{latest}\n\n"
            f"## Required output schema (conform EXACTLY)\n```json\n{gk_schema}\n```\n\n"
            f"Evaluate ONLY judgment criteria and overall goal alignment. "
            f"Return your verdict as a single JSON object conforming to the schema "
            f"above — use those exact field names. Return ONLY the JSON, nothing else."
        )
        cmd = ["claude", "-p", prompt,
               "--append-system-prompt", rules,
               "--output-format", "json"]
        gk_model = os.environ.get("WARDEN_GK_MODEL")  # cheap model per roadmap
        if gk_model:
            cmd += ["--model", gk_model]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  cwd=task_dir(task_id),
                                  timeout=int(os.environ.get("WARDEN_AGENT_TIMEOUT", "180")))
        except subprocess.TimeoutExpired:
            raise StageFailure(f"goal keeper timed out after '{stage}'")
        usage = _extract_usage(proc.stdout)
        if proc.returncode != 0:
            raise StageFailure(f"goal keeper exited {proc.returncode} after '{stage}'")
        verdict_obj = _extract_artifact(proc.stdout)
        if verdict_obj is None:
            raise StageFailure(f"could not extract goal keeper verdict after '{stage}'")
        atomic_write_json(out_path, verdict_obj)

    try:
        v = json.loads(out_path.read_text())
    except json.JSONDecodeError as e:
        raise StageFailure(f"goal keeper verdict is not valid JSON: {e}")
    schema = json.loads((SCHEMAS_DIR / "goalkeeper.schema.json").read_text())
    try:
        if jsonschema is not None:
            jsonschema.validate(v, schema)
        else:
            _mini_validate(v, schema)
    except Exception as e:
        raise StageFailure(f"goal keeper verdict failed schema: {e}")

    log_event(task_id, "goalkeeper", {"stage": stage, **v})
    if not v["on_track"]:
        raise StageFailure(
            f"goal keeper: off track after '{stage}' — violated: "
            f"{', '.join(v['violated_criteria']) or 'unspecified'} | {v['reasoning']}")
    return usage


def _stub_goalkeeper(task_id: str, stage: str, out_path: Path) -> None:
    """Deterministic alignment check: do the files the plan/implement artifacts
    touch stay within the spec's declared scope? A real (cheap-model) Goal
    Keeper replaces this in claude mode; the wiring is identical."""
    spec = json.loads((task_dir(task_id) / "artifacts" / "spec.json").read_text())
    scope = set(spec.get("scope", []))
    touched: set = set()
    if stage == "plan":
        plan = json.loads((task_dir(task_id) / "artifacts" / "plan.json").read_text())
        touched = {f for t in plan["tasks"] for f in t["files"]}
    elif stage == "implement":
        impl = json.loads((task_dir(task_id) / "artifacts" / "implement.json").read_text())
        touched = set(impl["files_changed"])
    out_of_scope = sorted(touched - scope)
    verdict_obj = {
        "on_track": not out_of_scope,
        "violated_criteria": (
            [f"scope: {f} is not in the spec's declared scope {sorted(scope)}"
             for f in out_of_scope]),
        "reasoning": ("all touched files are within the declared scope"
                      if not out_of_scope else
                      "the work has drifted from the spec's declared scope"),
    }
    atomic_write_json(out_path, verdict_obj)




# Notifications + human approval (Phase 4)

REQUIRES_HUMAN = {"approve"}  # the irreversible boundary before merge


def notify(task_id: str, text: str) -> None:
    """Fire-and-forget message via hooks/notify.sh (Telegram when configured,
    local log line otherwise). Never allowed to fail the pipeline."""
    try:
        subprocess.run([str(ROOT / "hooks" / "notify.sh"), text],
                       capture_output=True, timeout=20)
    except Exception:
        pass


def current_diff_hash(task_id: str) -> str:
    return subprocess.run(
        [str(ROOT / "pipeline" / "diff_hash.sh"), str(task_dir(task_id))],
        capture_output=True, text=True).stdout.strip()


def approval_is_valid(task_id: str) -> bool:
    """A usable approval exists: positive and bound to the CURRENT diff."""
    p = task_dir(task_id) / "artifacts" / "approval.json"
    if not p.exists():
        return False
    try:
        rec = json.loads(p.read_text())
    except json.JSONDecodeError:
        return False
    return bool(rec.get("approved")) and rec.get("diff_hash") == current_diff_hash(task_id)


def _load_telegram_cfg() -> tuple:
    secrets_path = Path(os.environ.get("WARDEN_SECRETS",
                                       Path.home() / ".warden" / "secrets.env"))
    cfg = {}
    if secrets_path.exists():
        for line in secrets_path.read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, _, v = line.partition("=")
                cfg[k.strip()] = v.strip().strip('"').strip("'")
    return (cfg.get("TELEGRAM_BOT_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN"),
            cfg.get("TELEGRAM_USER_ID") or os.environ.get("TELEGRAM_USER_ID"))


def _secret(name: str, default: str = "") -> str:
    """Read a config value from the environment, falling back to secrets.env.
    Lets settings like WARDEN_DELIVER_TO live in ~/.warden/secrets.env so every
    WARDEN process honors them regardless of how it was started."""
    val = os.environ.get(name)
    if val:
        return val
    secrets_path = Path(os.environ.get("WARDEN_SECRETS",
                                       Path.home() / ".warden" / "secrets.env"))
    if secrets_path.exists():
        for line in secrets_path.read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, _, v = line.partition("=")
                if k.strip() == name:
                    return v.strip().strip('"').strip("'")
    return default


def _send_approval_buttons(task_id: str, text: str) -> bool:
    """Inline-keyboard approval request via Bot API. False when unconfigured."""
    token, user = _load_telegram_cfg()
    if not token or not user:
        return False
    keyboard = json.dumps({"inline_keyboard": [[
        {"text": "Approve", "callback_data": f"approve:{task_id}"},
        {"text": "Reject", "callback_data": f"reject:{task_id}"},
        {"text": "Show diff", "callback_data": f"diff:{task_id}"},
    ]]})
    proc = subprocess.run(
        ["curl", "-sS", "-m", "10",
         f"https://api.telegram.org/bot{token}/sendMessage",
         "--data-urlencode", f"chat_id={user}",
         "--data-urlencode", f"text={text}",
         "--data-urlencode", f"reply_markup={keyboard}"],
        capture_output=True, text=True)
    try:
        return proc.returncode == 0 and json.loads(proc.stdout).get("ok", False)
    except json.JSONDecodeError:
        return False


def request_approval(task_id: str, state: dict) -> None:
    """Post the approval request, record the hash the approval must bind to,
    park the task as awaiting_approval, and stop. Resumability from Phase 1
    is what makes stopping safe."""
    h = current_diff_hash(task_id)
    state["pending_approval"] = {
        "diff_hash": h,
        "requested_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    state["status"] = "awaiting_approval"
    save_state(task_id, state)
    log_event(task_id, "approval_requested", {"diff_hash": h})

    text = (f"WARDEN approval needed\n"
            f"task: {task_id}\nstage: approve\n"
            f"diff: {h[:12]} (tasks/{task_id}/workdir)\n"
            f"tokens so far: {state['budget']['tokens_spent']}")
    sent = _send_approval_buttons(task_id, text)
    if not sent:
        notify(task_id, text)
        (task_dir(task_id) / "APPROVAL_NEEDED").write_text(
            text + f"\n\nNo Telegram configured. Approve manually with:\n"
                   f"  python3 pipeline/run.py approve {task_id}\n")
    print(f"[warden] {task_id} awaiting approval "
          f"({'Telegram buttons sent' if sent else 'see APPROVAL_NEEDED'})")


def cmd_approve(task_id: str) -> None:
    """Manual approval from the machine itself — the dev/no-Telegram path.
    Binds to the current diff hash exactly like the bot does."""
    state = load_state(task_id)
    if state["status"] != "awaiting_approval":
        sys.exit(f"{task_id} is not awaiting approval (status: {state['status']})")
    record = {"approved": True, "by": "manual-cli",
              "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
              "diff_hash": current_diff_hash(task_id)}
    atomic_write_json(task_dir(task_id) / "artifacts" / "approval.json", record)
    state["status"] = "pending"
    save_state(task_id, state)
    log_event(task_id, "approval", record)
    (task_dir(task_id) / "APPROVAL_NEEDED").unlink(missing_ok=True)
    print(f"[warden] {task_id} approved (diff {record['diff_hash'][:8]}); "
          f"re-run to continue")


def _wgit(task_id: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=task_dir(task_id) / "workdir",
                          capture_output=True, text=True)


def workdir_is_repo(task_id: str) -> bool:
    return (task_dir(task_id) / "workdir" / ".git").exists()


def init_workdir_repo(task_id: str) -> str:
    """Each task workdir is its own git repo, so rollback has a precise target."""
    _wgit(task_id, "init", "-q")
    _wgit(task_id, "config", "user.email", "warden@local")
    _wgit(task_id, "config", "user.name", "warden")
    _wgit(task_id, "commit", "--allow-empty", "-q", "-m", "warden: task created")
    return _wgit(task_id, "rev-parse", "HEAD").stdout.strip()


def checkpoint_workdir(task_id: str, stage: str) -> str | None:
    """Commit the workdir after a passed stage; returns the new last-good commit."""
    if not workdir_is_repo(task_id):
        return None
    _wgit(task_id, "add", "-A")
    _wgit(task_id, "commit", "-q", "--allow-empty", "-m",
          f"warden checkpoint: {stage} passed")
    return _wgit(task_id, "rev-parse", "HEAD").stdout.strip()


def workdir_diff_hash(task_id: str) -> str | None:
    """Hash of all uncommitted workdir changes (vs the last good checkpoint).
    Two failed implement attempts with the same hash = the agent is looping."""
    if not workdir_is_repo(task_id):
        return None
    _wgit(task_id, "add", "-A")  # stage everything so new files count too
    diff = _wgit(task_id, "diff", "--cached").stdout
    return hashlib.sha256(diff.encode()).hexdigest()


def rollback_workdir(task_id: str, commit: str | None) -> bool:
    """Hard-reset the workdir to the last good checkpoint and drop strays."""
    if not workdir_is_repo(task_id) or not commit:
        return False
    _wgit(task_id, "reset", "-q", "--hard", commit)
    _wgit(task_id, "clean", "-fdq")
    return True


# Escalation (Phase 2): halt, roll back, record full failure context

def escalate(task_id: str, reason: str) -> None:
    state = load_state(task_id)
    state["status"] = "escalated"
    save_state(task_id, state)

    rolled_back = rollback_workdir(task_id, state.get("last_good_commit"))

    context = {
        "reason": reason,
        "last_failure": state["stages"].get(state["stage"], {}).get("last_failure"),
        "stage": state["stage"],
        "attempts": state["stages"].get(state["stage"], {}).get("attempts"),
        "budget": state.get("budget"),
        "rolled_back_to": state.get("last_good_commit") if rolled_back else None,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    atomic_write_json(task_dir(task_id) / "escalation.json", context)
    log_event(task_id, "escalation", context)
    (task_dir(task_id) / "NEEDS_HUMAN").write_text(
        f"{reason}\nFull context: escalation.json | event trail: run.jsonl\n")
    notify(task_id, f"WARDEN escalation\ntask: {task_id}\nstage: {state['stage']}\n"
                    f"reason: {reason}\ntokens: {state['budget']['tokens_spent']}")
    print(f"[warden] ESCALATED {task_id}: {reason}"
          + (" (workdir rolled back)" if rolled_back else ""), file=sys.stderr)


# Main loop

def cmd_new(task_id: str, description: str) -> None:
    td = task_dir(task_id)
    if td.exists():
        sys.exit(f"task {task_id} already exists")
    (td / "artifacts").mkdir(parents=True)
    (td / "workdir").mkdir()
    (td / "task.md").write_text(description + "\n")
    initial_commit = init_workdir_repo(task_id)
    state = {
        "task_id": task_id,
        "stage": STAGES[0],
        "status": "pending",
        "last_good_commit": initial_commit,
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
    if state["status"] == "awaiting_approval" and not approval_is_valid(task_id):
        print(f"[warden] {task_id} still awaiting approval")
        return

    # Resumability: continue from whatever stage state.json records.
    while state["stage"] != "done":
        stage = state["stage"]
        role = STAGE_CONFIG[stage][0]
        ss = state["stages"][stage]
        budget = state["budget"]

        # --- Phase 4: human gate. Park here until a diff-bound approval
        # exists; an approval voided by later changes re-parks the task. ---
        if stage in REQUIRES_HUMAN and not approval_is_valid(task_id):
            request_approval(task_id, state)
            return

        # --- Phase 2: budgets and attempt ceilings, checked BEFORE each attempt ---
        if ss["attempts"] >= ss["max_attempts"]:
            escalate(task_id, f"stage '{stage}' exhausted {ss['max_attempts']} attempts")
            return
        if budget["total_attempts"] >= budget["max_total_attempts"]:
            escalate(task_id, f"task budget exhausted: {budget['total_attempts']} total attempts")
            return
        if budget["tokens_spent"] >= budget["max_token_spend"]:
            escalate(task_id, f"token budget exhausted: {budget['tokens_spent']} spent")
            return

        attempt = ss["attempts"] + 1
        print(f"[warden] {task_id} :: stage '{stage}' (attempt {attempt}/{ss['max_attempts']})")

        state["status"] = "running"
        ss["attempts"] = attempt           # recorded BEFORE work: a crash
        budget["total_attempts"] += 1      # mid-stage still counts the attempt
        save_state(task_id, state)
        log_event(task_id, "stage_start", {"stage": stage, "attempt": attempt})

        try:
            if role is not None:
                usage = run_agent(task_id, stage, role, agent_mode)
                u = usage.get("usage", {})
                budget["tokens_spent"] += u.get("input_tokens", 0) + u.get("output_tokens", 0)
                save_state(task_id, state)
            # Multi-model review: Claude and Gemini review independently, GPT judges.
            # Always on. Never breaks the pipeline: on any error, falls back to Claude's verdict.
            if stage == "review" and agent_mode == "claude":
                try:
                    from review_debate import reconcile
                    _rp = task_dir(task_id) / "artifacts" / "review.json"
                    _claude_v = json.loads(_rp.read_text())
                    _task = (task_dir(task_id) / "task.md").read_text()
                    # Read the actual source files the worker produced (the diff is
                    # empty here because WARDEN commits each stage). Show the reviewers the real code.
                    _wd = task_dir(task_id) / "workdir"
                    _parts = []
                    for _f in sorted(_wd.rglob("*.py")):
                        if "__pycache__" in str(_f) or ".pytest_cache" in str(_f):
                            continue
                        try:
                            _parts.append(f"### {_f.relative_to(_wd)}\n{_f.read_text()}")
                        except Exception:
                            pass
                    _diff = "\n\n".join(_parts) if _parts else _wgit(task_id, "diff", "HEAD~1", "HEAD").stdout
                    _deb = reconcile(_task, _task, _diff, _claude_v)
                    atomic_write_json(_rp, _deb["final"])
                    log_event(task_id, "gemini_debate", {
                        "claude": _deb["claude_review"],
                        "gemini": _deb["gemini_review"],
                        "final": _deb["final"],
                    })
                    print("[warden] gemini review debate applied")
                except Exception as _e:
                    log_event(task_id, "gemini_debate_skipped", {"reason": str(_e)})
                    print(f"[warden] gemini debate skipped (fell back to Claude): {_e}")
            validate_artifact(task_id, stage)
            run_gate(task_id, stage)
            if stage == "test":
                # Phase 3: executable acceptance criteria, proved by script.
                run_named_gate(task_id, stage, "gate_acceptance.sh")
            if stage in GOALKEEPER_STAGES:
                # Phase 3: judgment criteria + goal alignment, judged by the
                # Goal Keeper. Never re-checks what scripts above proved.
                gk_usage = run_goalkeeper(task_id, stage, agent_mode)
                u = gk_usage.get("usage", {})
                budget["tokens_spent"] += u.get("input_tokens", 0) + u.get("output_tokens", 0)
                save_state(task_id, state)
        except StageFailure as e:
            ss["last_failure"] = str(e)  # surfaces in escalation context
            log_event(task_id, "stage_failed",
                      {"stage": stage, "attempt": attempt, "reason": str(e)})

            # --- Phase 2: stuck detection on implement ---
            # Identical diff across consecutive failed attempts = agent is
            # looping; escalate now instead of burning remaining attempts.
            if stage == "implement":
                h = workdir_diff_hash(task_id)
                if h is not None and h == ss["last_diff_hash"]:
                    escalate(task_id,
                             f"stuck: implement attempt {attempt} produced an "
                             f"identical diff to the previous attempt")
                    return
                ss["last_diff_hash"] = h

            save_state(task_id, state)
            continue  # retry same stage; ceilings re-checked at loop top

        # --- stage passed ---
        commit_artifact(task_id, stage)
        good = checkpoint_workdir(task_id, stage)
        if good:
            state["last_good_commit"] = good
        nxt = STAGES[STAGES.index(stage) + 1] if stage != STAGES[-1] else "done"
        state["stage"] = nxt
        state["status"] = "done" if nxt == "done" else "pending"
        save_state(task_id, state)   # atomic transition — the crash-safe moment
        log_event(task_id, "transition", {"from": stage, "to": nxt,
                                          "checkpoint": good})
        if stage == "merge":
            deliver_to_target(task_id)
        notify(task_id, f"WARDEN: {task_id}: {stage} passed -> {nxt} "
                        f"(tokens: {budget['tokens_spent']})")

    print(f"[warden] {task_id} complete")


def deliver_to_target(task_id: str) -> None:
    """Deliver an approved task's output to a target repo and commit on green.

    Opt-in via the WARDEN_DELIVER_TO env var (an existing git repo path). If
    unset, delivery is skipped. Copies the task workdir's src/ and tests/ into
    the target, runs pytest there, and commits ONLY if the tests pass — so a
    delivered module is always verified in its destination before it lands in
    history. On test failure the files are left in place (uncommitted) and the
    failure is logged, so nothing broken is silently committed.
    """
    import shutil
    dest = _secret("WARDEN_DELIVER_TO")
    if not dest:
        return
    dest_path = Path(dest).expanduser()
    if not (dest_path / ".git").exists():
        print(f"[warden] deliver: {dest_path} is not a git repo; skipping",
              file=sys.stderr)
        log_event(task_id, "deliver_skipped", {"reason": "target not a git repo",
                                               "dest": str(dest_path)})
        return

    wd = task_dir(task_id) / "workdir"
    copied = []
    skipped = []
    for sub in ("src", "tests"):
        s = wd / sub
        if not s.is_dir():
            continue
        for f in s.rglob("*"):
            if not f.is_file():
                continue
            if "__pycache__" in f.parts or ".pytest_cache" in f.parts:
                continue
            rel = f.relative_to(wd)
            target = dest_path / rel
            # Deliver only files this task CREATED, never seeded dependencies.
            # A dependency seeded in for the task to compile against already
            # exists in the target; re-copying it risks clobbering the canonical
            # version with a stale seed. Skip anything already present.
            if target.exists():
                skipped.append(str(rel))
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, target)
            copied.append(str(rel))

    if not copied:
        print(f"[warden] deliver: nothing to copy from {wd}", file=sys.stderr)
        log_event(task_id, "deliver_skipped", {"reason": "no src/ or tests/ files"})
        return

    # Verify in the destination before committing.
    proc = subprocess.run(["python3", "-m", "pytest", "tests/", "-q"],
                          cwd=dest_path, capture_output=True, text=True, timeout=300)
    if proc.returncode != 0:
        print(f"[warden] deliver: pytest FAILED in {dest_path}; files copied but "
              f"NOT committed. Inspect and commit manually.", file=sys.stderr)
        print(proc.stdout[-1500:], file=sys.stderr)
        log_event(task_id, "deliver_test_failed",
                  {"dest": str(dest_path), "files": copied,
                   "pytest_tail": proc.stdout[-1500:]})
        return

    subprocess.run(["git", "add", *copied], cwd=dest_path, check=True)
    msg = f"WARDEN delivery: {task_id} (tests green, 3-model reviewed)"
    commit = subprocess.run(["git", "commit", "-m", msg], cwd=dest_path,
                            capture_output=True, text=True)
    if commit.returncode == 0:
        print(f"[warden] deliver: {len(copied)} files -> {dest_path}, tests green, committed.")
        log_event(task_id, "delivered", {"dest": str(dest_path), "files": copied})
    else:
        print(f"[warden] deliver: nothing to commit (no changes) in {dest_path}.")
        log_event(task_id, "deliver_nochange", {"dest": str(dest_path)})

    # Optional mirror: after a successful delivery, copy the whole target repo
    # to a second location (e.g. a desktop folder) so it always matches. Runs
    # only on success, so the mirror never receives a broken tree. Set via
    # WARDEN_DELIVER_MIRROR in the environment or ~/.warden/secrets.env.
    mirror = _secret("WARDEN_DELIVER_MIRROR")
    if mirror:
        mirror_path = Path(mirror).expanduser()
        try:
            mirror_path.mkdir(parents=True, exist_ok=True)
            r = subprocess.run(
                ["rsync", "-a", "--delete",
                 "--exclude", "__pycache__", "--exclude", ".pytest_cache",
                 "--exclude", ".git",
                 f"{dest_path}/", f"{mirror_path}/"],
                capture_output=True, text=True, timeout=120)
            if r.returncode == 0:
                print(f"[warden] deliver: mirrored to {mirror_path}.")
                log_event(task_id, "mirrored", {"mirror": str(mirror_path)})
            else:
                print(f"[warden] deliver: mirror failed: {r.stderr[-300:]}",
                      file=sys.stderr)
                log_event(task_id, "mirror_failed", {"mirror": str(mirror_path),
                                                      "stderr": r.stderr[-300:]})
        except Exception as e:
            print(f"[warden] deliver: mirror error: {e}", file=sys.stderr)
            log_event(task_id, "mirror_error", {"mirror": str(mirror_path),
                                                "error": str(e)})


def cmd_status(task_id: str) -> None:
    print(json.dumps(load_state(task_id), indent=2))


CHAINS_DIR = ROOT / "chains"


def _chain_path(name):
    return CHAINS_DIR / f"{name}.json"


def _chain_state_path(name):
    return CHAINS_DIR / f"{name}.state.json"


def load_chain(name):
    p = _chain_path(name)
    if not p.exists():
        sys.exit(f"no chain definition: {p}")
    return json.loads(p.read_text())


def load_chain_state(name):
    p = _chain_state_path(name)
    if p.exists():
        return json.loads(p.read_text())
    return {"chain": name, "step": 0, "status": "pending"}


def save_chain_state(name, st):
    atomic_write_json(_chain_state_path(name), st)


def _seed_workdir(task_id, seeds):
    import shutil
    dest = _secret("WARDEN_DELIVER_TO")
    if not dest:
        print("[warden] chain: WARDEN_DELIVER_TO unset; cannot seed deps", file=sys.stderr)
        return
    base = Path(dest).expanduser()
    wd = task_dir(task_id) / "workdir"
    for rel in seeds:
        s = base / rel
        if not s.is_file():
            print(f"[warden] chain: seed source missing: {s}", file=sys.stderr)
            continue
        t = wd / rel
        t.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(s, t)


def cmd_chain_start(name, agent_mode):
    chain = load_chain(name)
    steps = chain["steps"]
    st = load_chain_state(name)

    while st["step"] < len(steps):
        step = steps[st["step"]]
        task_id = step["task_id"]
        td = task_dir(task_id)

        if not (td / "state.json").exists():
            cmd_new(task_id, f"chain {name} step {st['step']}")
            (td / "task.md").write_text(step["spec"])
            _seed_workdir(task_id, step.get("seed", []))
            print(f"[warden] chain {name}: created step {st['step']} -> {task_id}")

        state = load_state(task_id)

        if state["status"] == "awaiting_approval":
            print(f"[warden] chain {name}: {task_id} awaiting approval. Approve it, "
                  f"then re-run: warden chain {name} --agent-mode {agent_mode}")
            st["status"] = "awaiting_approval"
            save_chain_state(name, st)
            return

        if state["stage"] == "done":
            print(f"[warden] chain {name}: step {st['step']} ({task_id}) done; advancing.")
            st["step"] += 1
            st["status"] = "pending"
            save_chain_state(name, st)
            continue

        st["status"] = "running"
        save_chain_state(name, st)
        cmd_run(task_id, agent_mode)
        state = load_state(task_id)
        if state["status"] == "escalated":
            print(f"[warden] chain {name}: step {st['step']} ({task_id}) ESCALATED; "
                  f"chain paused. Resolve, then re-run the chain.", file=sys.stderr)
            st["status"] = "escalated"
            save_chain_state(name, st)
            return

    print(f"[warden] chain {name}: all {len(steps)} steps complete.")
    st["status"] = "done"
    save_chain_state(name, st)


def cmd_chain_status(name):
    chain = load_chain(name)
    st = load_chain_state(name)
    print(f"chain {name}: step {st['step']}/{len(chain['steps'])} ({st['status']})")
    for i, step in enumerate(chain["steps"]):
        mark = "->" if i == st["step"] else "  "
        tid = step["task_id"]
        tstate = "-"
        if (task_dir(tid) / "state.json").exists():
            tstate = load_state(tid).get("status", "?")
        print(f" {mark} {i}. {tid} [{tstate}]")


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

    p_ap = sub.add_parser("approve", help="manually approve an awaiting task (no-Telegram fallback)")
    p_ap.add_argument("task_id")
    p_ch = sub.add_parser("chain", help="run/resume a chain of dependent tasks")
    p_ch.add_argument("name")
    p_ch.add_argument("--agent-mode", choices=["stub", "claude"], default="stub")
    p_chs = sub.add_parser("chain-status", help="show a chain progress")
    p_chs.add_argument("name")
    args = ap.parse_args()
    if args.cmd == "new":
        cmd_new(args.task_id, args.description)
    elif args.cmd == "run":
        cmd_run(args.task_id, args.agent_mode)
    elif args.cmd == "status":
        cmd_status(args.task_id)
    elif args.cmd == "approve":
        cmd_approve(args.task_id)
    elif args.cmd == "chain":
        cmd_chain_start(args.name, args.agent_mode)
    elif args.cmd == "chain-status":
        cmd_chain_status(args.name)


if __name__ == "__main__":
    main()
