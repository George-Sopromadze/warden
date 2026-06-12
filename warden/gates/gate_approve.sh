#!/usr/bin/env bash
# gate_approve (Phase 4): verifies a HUMAN approval record exists, is positive,
# and is bound to the CURRENT diff hash. If the workdir changed after approval,
# the hash no longer matches and the approval is void — gate fails, the
# orchestrator re-requests approval. The script, not the bot, is the authority.
set -euo pipefail
source "$(dirname "$0")/common.sh"
TASK_DIR="${1:?usage: gate_approve.sh <task-dir>}"
APPROVAL="$TASK_DIR/artifacts/approval.json"

require_file gate_approve "$APPROVAL"

approved=$(json_get "$APPROVAL" 'str(d["approved"]).lower()')
[ "$approved" = "true" ] || verdict gate_approve fail "approval record says rejected"

recorded=$(json_get "$APPROVAL" 'd["diff_hash"]')
current=$("$(dirname "$0")/../pipeline/diff_hash.sh" "$TASK_DIR")
[ "$recorded" = "$current" ] || \
  verdict gate_approve fail "approval void: diff changed after approval (recorded ${recorded:0:8}, current ${current:0:8})"

by=$(json_get "$APPROVAL" 'd["by"]')
verdict gate_approve pass "approved by $by, diff hash bound"
