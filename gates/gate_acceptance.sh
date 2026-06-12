#!/usr/bin/env bash
# gate_acceptance (Phase 3): executable acceptance criteria are checked by
# THIS script, never by an LLM. Each criterion's `check` runs as a shell
# command with the task dir as cwd; non-zero exit = criterion violated.
#
# NOTE: criteria commands are authored by the planner agent. Until Phase 6
# sandboxing, run the pipeline only on machines/containers you'd let the
# agent touch anyway.
set -euo pipefail
source "$(dirname "$0")/common.sh"
TASK_DIR="${1:?usage: gate_acceptance.sh <task-dir>}"
SPEC="$TASK_DIR/artifacts/spec.json"

require_file gate_acceptance "$SPEC"

n=$(json_get "$SPEC" 'len([c for c in d["acceptance_criteria"] if c["type"]=="executable"])')
i=0
while [ "$i" -lt "$n" ]; do
  id=$(json_get "$SPEC" "[c for c in d['acceptance_criteria'] if c['type']=='executable'][$i]['id']")
  cmd=$(json_get "$SPEC" "[c for c in d['acceptance_criteria'] if c['type']=='executable'][$i]['check']")
  if ! (cd "$TASK_DIR" && eval "$cmd" > /dev/null 2>&1); then
    verdict gate_acceptance fail "executable criterion $id failed: $cmd"
  fi
  i=$((i+1))
done

verdict gate_acceptance pass "$n executable criteria verified"
