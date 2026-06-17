#!/usr/bin/env python3
"""
WARDEN observability CLI (Phase 5).

Answer "why did it do that?" for any past run, from files alone.

    python3 pipeline/warden.py report <task-id>          timeline + costs + verdicts
    python3 pipeline/warden.py replay <task-id> <stage>  reconstruct what a stage saw
    python3 pipeline/warden.py costs  <task-id>           cost/token breakdown only
    python3 pipeline/warden.py list                       all tasks + status

Reads only tasks/<id>/run.jsonl, state.json, and artifacts/ — no live state,
so it works on any finished or escalated task.
"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TASKS = ROOT / "tasks"
SCHEMAS = ROOT / "schemas"
AGENTS = ROOT / "agents"

STAGES = ["spec", "plan", "implement", "test", "review", "approve", "merge"]
STAGE_ARTIFACT = {
    "spec": "spec.json", "plan": "plan.json", "implement": "implement.json",
    "test": "test-report.json", "review": "review.json",
    "approve": "approval.json", "merge": "merge.json",
}

# ANSI colors (degrade gracefully if piped)
def _c(code, s):
    return f"\033[{code}m{s}\033[0m" if sys.stdout.isatty() else s
GREEN = lambda s: _c("32", s)
RED = lambda s: _c("31", s)
YEL = lambda s: _c("33", s)
DIM = lambda s: _c("2", s)
BOLD = lambda s: _c("1", s)
CYAN = lambda s: _c("36", s)


def load_events(task_id: str) -> list:
    p = TASKS / task_id / "run.jsonl"
    if not p.exists():
        sys.exit(f"no run log for task '{task_id}' (looked in {p})")
    out = []
    for line in p.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out


def load_state(task_id: str) -> dict:
    p = TASKS / task_id / "state.json"
    return json.loads(p.read_text()) if p.exists() else {}


def _hhmmss(ts: str) -> str:
    # "2026-06-13T19:51:34+0100" -> "19:51:34"
    return ts[11:19] if len(ts) >= 19 else ts


def _usage_tokens(ev: dict) -> int:
    u = ev.get("usage") or {}
    inner = u.get("usage") or u
    return int(inner.get("input_tokens", 0)) + int(inner.get("output_tokens", 0))


def _usage_cost(ev: dict) -> float:
    u = ev.get("usage") or {}
    return float(u.get("total_cost_usd", 0) or 0)


# ----------------------------------------------------------------------------
# report
# ----------------------------------------------------------------------------

def cmd_report(task_id: str) -> None:
    events = load_events(task_id)
    state = load_state(task_id)

    status = state.get("status", "?")
    stage = state.get("stage", "?")
    status_str = {"done": GREEN("done"), "escalated": RED("escalated"),
                  "awaiting_approval": YEL("awaiting approval")}.get(status, status)
    print(BOLD(f"\nWARDEN report — {task_id}"))
    print(f"status: {status_str}   current stage: {stage}")

    desc = next((e["description"] for e in events if e["event"] == "created"), None)
    if desc:
        print(DIM(f"task: {desc}"))
    print()

    # Timeline
    print(BOLD("Timeline"))
    total_tokens = 0
    total_cost = 0.0
    for ev in events:
        t = _hhmmss(ev.get("ts", ""))
        e = ev["event"]
        if e == "stage_start":
            print(f"  {DIM(t)}  {CYAN('▶')} {ev['stage']} "
                  f"{DIM('(attempt ' + str(ev.get('attempt', '?')) + ')')}")
        elif e == "agent_message":
            tok = _usage_tokens(ev)
            cost = _usage_cost(ev)
            total_tokens += tok
            total_cost += cost
            if tok or cost:
                print(f"  {DIM(t)}     {DIM('agent ' + ev.get('role', ev.get('agent','')) + f': {tok} tok, ${cost:.4f}')}")
        elif e == "gate_result":
            mark = GREEN("[OK]") if ev.get("pass") else RED("[X]")
            print(f"  {DIM(t)}     {mark} {ev['gate']}: {ev.get('reason','')}")
        elif e == "goalkeeper":
            mark = GREEN("on track") if ev.get("on_track") else RED("off track")
            extra = "" if ev.get("on_track") else RED(" — " + ", ".join(ev.get("violated_criteria", [])))
            print(f"  {DIM(t)}     {mark} goalkeeper{extra}")
        elif e == "transition":
            print(f"  {DIM(t)}  {GREEN('→')} {ev['from']} → {BOLD(ev['to'])}")
        elif e == "stage_failed":
            print(f"  {DIM(t)}     {RED('FAILED')} {ev['stage']}: {ev.get('reason','')}")
        elif e == "escalation":
            print(f"  {DIM(t)}  {RED('ESCALATED')}: {ev.get('reason','')}")
            if ev.get("rolled_back_to"):
                print(f"  {DIM(t)}     {DIM('rolled back to ' + ev['rolled_back_to'][:10])}")
        elif e == "approval_requested":
            print(f"  {DIM(t)}  {YEL('⏸ approval requested')} (diff {ev.get('diff_hash','')[:8]})")
        elif e == "approval":
            print(f"  {DIM(t)}  {GREEN('approved')} by {ev.get('by','?')}")

    # Costs
    print()
    print(BOLD("Cost"))
    budget = state.get("budget", {})
    bt = budget.get("tokens_spent", total_tokens)
    print(f"  tokens: {bt:,}" + (f"  (~${total_cost:.4f})" if total_cost else ""))
    print(f"  attempts: {budget.get('total_attempts','?')} / {budget.get('max_total_attempts','?')} budget")

    # Per-stage attempt summary
    print()
    print(BOLD("Stages"))
    for s in STAGES:
        st = state.get("stages", {}).get(s)
        if not st:
            continue
        a = st.get("attempts", 0)
        if status == "done":
            mark = GREEN("done")
        elif stage in STAGES and STAGES.index(s) < STAGES.index(stage):
            mark = GREEN("done")
        elif s == stage:
            mark = YEL("current")
        else:
            mark = DIM("pending")
        print(f"  {s:<10} {mark:<20} {DIM(str(a) + ' attempt(s)')}")
    print()


# ----------------------------------------------------------------------------
# replay — reconstruct exactly what a stage saw
# ----------------------------------------------------------------------------

def cmd_replay(task_id: str, stage: str) -> None:
    if stage not in STAGES:
        sys.exit(f"unknown stage '{stage}'. one of: {', '.join(STAGES)}")
    td = TASKS / task_id
    print(BOLD(f"\nWARDEN replay — {task_id} / {stage}\n"))

    # The rules file the agent ran under
    role = "reviewer" if stage == "review" else "worker"
    rules = AGENTS / f"{role}.md"
    print(BOLD(f"── system prompt (agents/{role}.md) ──"))
    print(DIM(rules.read_text().strip() if rules.exists() else "(missing)"))
    print()

    # Task description
    task_md = td / "task.md"
    print(BOLD("── task ──"))
    print(task_md.read_text().strip() if task_md.exists() else "(missing)")
    print()

    # Prior artifacts the stage would have seen
    print(BOLD("── prior stage artifacts ──"))
    any_prior = False
    for s in STAGES[: STAGES.index(stage)]:
        ap = td / "artifacts" / STAGE_ARTIFACT[s]
        if ap.exists():
            any_prior = True
            print(CYAN(f"  [{s}] {STAGE_ARTIFACT[s]}"))
            print(DIM("  " + ap.read_text().strip().replace("\n", "\n  ")))
    if not any_prior:
        print(DIM("  (none)"))
    print()

    # The artifact this stage produced (if any)
    out = td / "artifacts" / STAGE_ARTIFACT[stage]
    print(BOLD(f"── {stage} produced ──"))
    print(out.read_text().strip() if out.exists() else DIM("(no artifact — stage did not complete)"))

    # What the gates said about it
    events = load_events(task_id)
    gate_lines = [e for e in events
                  if e.get("stage") == stage and e["event"] in ("gate_result", "goalkeeper", "stage_failed")]
    if gate_lines:
        print()
        print(BOLD("── verdicts ──"))
        for e in gate_lines:
            if e["event"] == "gate_result":
                mark = GREEN("[OK]") if e.get("pass") else RED("[X]")
                print(f"  {mark} {e['gate']}: {e.get('reason','')}")
            elif e["event"] == "goalkeeper":
                mark = GREEN("[OK]") if e.get("on_track") else RED("[X]")
                print(f"  {mark} goalkeeper: {e.get('reasoning','')}")
            elif e["event"] == "stage_failed":
                print(f"  {RED('FAILED')}: {e.get('reason','')}")
    print()


# ----------------------------------------------------------------------------
# costs / list
# ----------------------------------------------------------------------------

def cmd_costs(task_id: str) -> None:
    events = load_events(task_id)
    print(BOLD(f"\nWARDEN costs — {task_id}\n"))
    per_stage = {}
    for ev in events:
        if ev["event"] == "agent_message":
            s = ev.get("stage", "?")
            per_stage.setdefault(s, {"tok": 0, "cost": 0.0, "calls": 0})
            per_stage[s]["tok"] += _usage_tokens(ev)
            per_stage[s]["cost"] += _usage_cost(ev)
            per_stage[s]["calls"] += 1
    if not any(v["tok"] or v["cost"] for v in per_stage.values()):
        print(DIM("  no token usage recorded (stub-mode run?)\n"))
        return
    print(f"  {'stage':<12}{'calls':>6}{'tokens':>12}{'cost':>12}")
    tt = tc = 0
    for s in STAGES:
        if s in per_stage:
            v = per_stage[s]
            tt += v["tok"]; tc += v["cost"]
            print(f"  {s:<12}{v['calls']:>6}{v['tok']:>12,}{('$%.4f' % v['cost']):>12}")
    print(DIM(f"  {'─'*42}"))
    print(BOLD(f"  {'total':<12}{'':>6}{tt:>12,}{('$%.4f' % tc):>12}\n"))


def cmd_list() -> None:
    if not TASKS.exists():
        print("no tasks yet"); return
    rows = []
    for d in sorted(TASKS.iterdir()):
        sp = d / "state.json"
        if sp.is_file():
            s = json.loads(sp.read_text())
            rows.append((d.name, s.get("stage", "?"), s.get("status", "?"),
                         s.get("budget", {}).get("tokens_spent", 0)))
    if not rows:
        print("no tasks yet"); return
    print(BOLD(f"\n  {'task':<20}{'stage':<12}{'status':<18}{'tokens':>10}"))
    for name, stage, status, tok in rows:
        st = {"done": GREEN, "escalated": RED,
              "awaiting_approval": YEL}.get(status, lambda x: x)(status)
        print(f"  {name:<20}{stage:<12}{st:<18}{tok:>10,}")
    print()


def main() -> None:
    ap = argparse.ArgumentParser(prog="warden")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("report", help="timeline + costs + verdicts for a task")
    r.add_argument("task_id")
    rp = sub.add_parser("replay", help="reconstruct exactly what a stage saw")
    rp.add_argument("task_id"); rp.add_argument("stage")
    c = sub.add_parser("costs", help="cost/token breakdown")
    c.add_argument("task_id")
    sub.add_parser("list", help="all tasks and their status")
    args = ap.parse_args()
    if args.cmd == "report":
        cmd_report(args.task_id)
    elif args.cmd == "replay":
        cmd_replay(args.task_id, args.stage)
    elif args.cmd == "costs":
        cmd_costs(args.task_id)
    elif args.cmd == "list":
        cmd_list()


if __name__ == "__main__":
    main()
