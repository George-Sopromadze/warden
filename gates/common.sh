#!/usr/bin/env bash
# Shared helpers for WARDEN gates.
# Contract: a gate exits 0 = pass, non-zero = fail, and prints exactly one
# JSON verdict object to stdout: {"gate": "...", "pass": bool, "reason": "..."}
set -euo pipefail

verdict() { # verdict <gate-name> <pass|fail> <reason>
  local gate="$1" result="$2" reason="$3"
  if [ "$result" = "pass" ]; then
    printf '{"gate": "%s", "pass": true, "reason": "%s"}\n' "$gate" "$reason"
    exit 0
  else
    printf '{"gate": "%s", "pass": false, "reason": "%s"}\n' "$gate" "$reason"
    exit 1
  fi
}

require_file() { # require_file <gate-name> <path>
  [ -f "$2" ] || verdict "$1" fail "missing required file: $2"
}

json_get() { # json_get <file> <python-expr on obj `d`>
  python3 - "$1" "$2" << 'PY'
import json, sys
d = json.load(open(sys.argv[1]))
print(eval(sys.argv[2], {"d": d}))
PY
}
