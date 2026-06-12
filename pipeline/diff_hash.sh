#!/usr/bin/env bash
# Canonical "what would be merged" hash for a task: full workdir diff from the
# task's initial commit to HEAD. One definition, used everywhere.
set -euo pipefail
TASK_DIR="${1:?usage: diff_hash.sh <task-dir>}"
WD="$TASK_DIR/workdir"
if [ ! -d "$WD/.git" ]; then echo "no-git"; exit 0; fi
root=$(cd "$WD" && git rev-list --max-parents=0 HEAD)
(cd "$WD" && git diff "$root"..HEAD) | shasum -a 256 | cut -d' ' -f1
