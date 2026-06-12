#!/usr/bin/env bash
# Phase 1 STUB: records a merge marker. Phase 8 replaces with the merge queue +
# "rebases cleanly onto integration" check.
set -euo pipefail
source "$(dirname "$0")/common.sh"
TASK_DIR="${1:?usage: gate_merge.sh <task-dir>}"
mkdir -p "$TASK_DIR/artifacts"
printf '{"merged": true, "mode": "phase1-stub"}\n' > "$TASK_DIR/artifacts/merge.json"
verdict gate_merge pass "merge recorded (Phase 1 stub)"
