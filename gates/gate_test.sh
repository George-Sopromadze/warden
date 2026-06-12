#!/usr/bin/env bash
# gate_test: test report shows zero failures and at least one suite ran.
# Optionally re-runs the real suite via WARDEN_TEST_CMD (trust scripts, not reports).
set -euo pipefail
source "$(dirname "$0")/common.sh"
TASK_DIR="${1:?usage: gate_test.sh <task-dir>}"
REPORT="$TASK_DIR/artifacts/test-report.json"

require_file gate_test "$REPORT"

if [ -n "${WARDEN_TEST_CMD:-}" ]; then
  (cd "$TASK_DIR/workdir" && eval "$WARDEN_TEST_CMD") || verdict gate_test fail "test suite failed on re-run"
fi

failed=$(json_get "$REPORT" 'd["failed"]')
suites=$(json_get "$REPORT" 'd["suites_run"]')
[ "$suites" -ge 1 ] || verdict gate_test fail "no test suites ran"
[ "$failed" -eq 0 ] || verdict gate_test fail "$failed test(s) failed"

verdict gate_test pass "$suites suite(s), 0 failures"
