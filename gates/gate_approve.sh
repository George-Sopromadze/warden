#!/usr/bin/env bash
# Phase 1 STUB: auto-approves so a toy task can flow end to end.
# Phase 4 replaces this with: requires_human → Telegram inline buttons →
# approval record bound to the diff hash.
set -euo pipefail
source "$(dirname "$0")/common.sh"
TASK_DIR="${1:?usage: gate_approve.sh <task-dir>}"
mkdir -p "$TASK_DIR/artifacts"
printf '{"approved": true, "by": "auto-phase1-stub", "diff_hash": null}\n' \
  > "$TASK_DIR/artifacts/approval.json"
verdict gate_approve pass "auto-approved (Phase 1 stub — replace in Phase 4)"
