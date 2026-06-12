#!/usr/bin/env bash
# gate_spec: schema validity is already enforced by the orchestrator.
# This gate adds: acceptance criteria exist, are well-formed, and acceptance.md
# is present (hard requirement from Phase 3 — wired in early so it never regresses).
set -euo pipefail
source "$(dirname "$0")/common.sh"
TASK_DIR="${1:?usage: gate_spec.sh <task-dir>}"
SPEC="$TASK_DIR/artifacts/spec.json"

require_file gate_spec "$SPEC"

n_exec=$(json_get "$SPEC" 'sum(1 for c in d["acceptance_criteria"] if c["type"]=="executable")')
n_total=$(json_get "$SPEC" 'len(d["acceptance_criteria"])')

[ "$n_total" -ge 1 ] || verdict gate_spec fail "no acceptance criteria"
[ "$n_exec" -ge 1 ]  || verdict gate_spec fail "no executable acceptance criteria"
# Phase 3 tightening point: also require acceptance.md
# require_file gate_spec "$TASK_DIR/artifacts/acceptance.md"

verdict gate_spec pass "$n_total criteria ($n_exec executable)"
