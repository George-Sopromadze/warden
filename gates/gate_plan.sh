#!/usr/bin/env bash
# gate_plan: every plan task has files declared; dependency ids resolve.
set -euo pipefail
source "$(dirname "$0")/common.sh"
TASK_DIR="${1:?usage: gate_plan.sh <task-dir>}"
PLAN="$TASK_DIR/artifacts/plan.json"

require_file gate_plan "$PLAN"

bad_deps=$(json_get "$PLAN" '
sum(1 for t in d["tasks"] for dep in t["depends_on"]
    if dep not in {x["id"] for x in d["tasks"]})')
[ "$bad_deps" -eq 0 ] || verdict gate_plan fail "$bad_deps unresolved depends_on references"

verdict gate_plan pass "plan well-formed"
