#!/usr/bin/env bash
# Shared helpers for WARDEN gates.
# Contract: a gate exits 0 = pass, non-zero = fail, and prints exactly one
# JSON verdict object to stdout: {"gate": "...", "pass": bool, "reason": "..."}
set -euo pipefail

verdict() { # verdict <gate-name> <pass|fail> <reason>
  local gate="$1" result="$2" reason="$3" passbool
  if [ "$result" = "pass" ]; then passbool=true; else passbool=false; fi
  # Build JSON via python json.dumps so reasons containing quotes, newlines,
  # or backslashes are correctly escaped and always parse as valid JSON.
  GATE="$gate" PASSBOOL="$passbool" REASON="$reason" python3 -c '
import json, os
print(json.dumps({
    "gate": os.environ["GATE"],
    "pass": os.environ["PASSBOOL"] == "true",
    "reason": os.environ["REASON"],
}))
'
  if [ "$result" = "pass" ]; then exit 0; else exit 1; fi
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
