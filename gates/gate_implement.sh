#!/usr/bin/env bash
# gate_implement: lint clean, build succeeds, diff non-empty, diff touches only
# files declared in the plan. Lint/build commands are project-specific —
# set WARDEN_LINT_CMD / WARDEN_BUILD_CMD per project, or leave unset to skip.
set -euo pipefail
source "$(dirname "$0")/common.sh"
TASK_DIR="${1:?usage: gate_implement.sh <task-dir>}"
IMPL="$TASK_DIR/artifacts/implement.json"
PLAN="$TASK_DIR/artifacts/plan.json"
WORKDIR="$TASK_DIR/workdir"

require_file gate_implement "$IMPL"
require_file gate_implement "$PLAN"

# 1. Lint / build (optional hooks)
if [ -n "${WARDEN_LINT_CMD:-}" ]; then
  (cd "$WORKDIR" && eval "$WARDEN_LINT_CMD") || verdict gate_implement fail "lint failed"
fi
if [ -n "${WARDEN_BUILD_CMD:-}" ]; then
  (cd "$WORKDIR" && eval "$WARDEN_BUILD_CMD") || verdict gate_implement fail "build failed"
fi

# 2. Diff non-empty and confined to planned files (when workdir is a git repo).
# HEAD is the last good checkpoint, so stage everything and diff against it —
# this counts new (previously untracked) files too. Staging is safe: the
# orchestrator checkpoints with `add -A` on pass and hard-resets on escalation.
if [ -d "$WORKDIR/.git" ]; then
  (cd "$WORKDIR" && git add -A)
  changed=$(cd "$WORKDIR" && git diff --cached --name-only)
  [ -n "$changed" ] || verdict gate_implement fail "diff is empty"
  allowed=$(json_get "$PLAN" '"\n".join(f for t in d["tasks"] for f in t["files"])')
  while IFS= read -r f; do
    echo "$allowed" | grep -qxF "$f" || \
      verdict gate_implement fail "diff touches undeclared file: $f"
  done <<< "$changed"
fi

verdict gate_implement pass "implementation within declared scope"
