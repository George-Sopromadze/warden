#!/usr/bin/env bash
# gate_review: review artifact valid (orchestrator-checked) and no blocking findings.
set -euo pipefail
source "$(dirname "$0")/common.sh"
TASK_DIR="${1:?usage: gate_review.sh <task-dir>}"
REVIEW="$TASK_DIR/artifacts/review.json"

require_file gate_review "$REVIEW"

blocking=$(json_get "$REVIEW" 'str(d["blocking"]).lower()')
verdict_field=$(json_get "$REVIEW" 'd["verdict"]')

[ "$blocking" = "false" ] || verdict gate_review fail "review has blocking findings"
[ "$verdict_field" = "approve" ] || verdict gate_review fail "reviewer requested changes"

verdict gate_review pass "review approved, no blocking findings"
