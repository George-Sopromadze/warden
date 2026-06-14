#!/usr/bin/env bash
# gate_implement: lint clean, build succeeds, diff non-empty, diff touches only
# files declared in the plan OR the worker's implement artifact. Lint/build are
# project-specific — set WARDEN_LINT_CMD / WARDEN_BUILD_CMD, or leave unset.
set -euo pipefail
source "$(dirname "$0")/common.sh"
TASK_DIR="${1:?usage: gate_implement.sh <task-dir>}"
IMPL="$TASK_DIR/artifacts/implement.json"
PLAN="$TASK_DIR/artifacts/plan.json"
WORKDIR="$TASK_DIR/workdir"

require_file gate_implement "$IMPL"
require_file gate_implement "$PLAN"

if [ -n "${WARDEN_LINT_CMD:-}" ]; then
  (cd "$WORKDIR" && eval "$WARDEN_LINT_CMD") || verdict gate_implement fail "lint failed"
fi
if [ -n "${WARDEN_BUILD_CMD:-}" ]; then
  (cd "$WORKDIR" && eval "$WARDEN_BUILD_CMD") || verdict gate_implement fail "build failed"
fi

if [ -d "$WORKDIR/.git" ]; then
  (cd "$WORKDIR" && git add -A)
  changed=$(cd "$WORKDIR" && git diff --cached --name-only)
  [ -n "$changed" ] || verdict gate_implement fail "diff is empty"
  planned=$(json_get "$PLAN" '"\n".join(f for t in d["tasks"] for f in t["files"])')
  declared_impl=$(json_get "$IMPL" '"\n".join(d.get("files_changed", []))')
  allowed=$(printf '%s\n%s\n' "$planned" "$declared_impl")
  while IFS= read -r f; do
    [ -z "$f" ] && continue
    case "$f" in
      __pycache__/*|*/__pycache__/*|*.pyc|*.pyo|.pytest_cache/*|*/.pytest_cache/*|node_modules/*|.gitignore) continue ;;
      README.md|*/README.md) continue ;;
    esac
    base=$(basename "$f")
    if echo "$allowed" | grep -qxF "$f" || echo "$allowed" | grep -qF "/$base" || echo "$allowed" | grep -qxF "$base"; then
      continue
    fi
    verdict gate_implement fail "diff touches undeclared file: $f"
  done <<< "$changed"
fi

verdict gate_implement pass "implementation within declared scope"
