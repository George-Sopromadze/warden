#!/usr/bin/env python3
"""
WARDEN PreToolUse hook (Phase 6 — security hardening).

Claude Code calls this before every tool use, passing a JSON event on stdin.
We allow or deny by exit code and a JSON decision on stdout:
  - allow: exit 0
  - deny:  exit 0 with {"decision":"block","reason":"..."} (Claude Code reads it),
           and we ALSO exit non-zero as a belt-and-braces signal.

This enforces three of your roadmap's Phase 6 controls:
  1. Filesystem blast radius: file tools may only touch paths inside the task
     worktree (WARDEN_WORKDIR). Anything outside is rejected.
  2. Secret exfiltration: tool arguments containing known secret patterns are
     blocked (a coding agent never needs to handle a bot token or API key).
  3. Network egress: Bash/web tool calls reaching out to hosts not on an
     allowlist are blocked.

Config via env (set by the orchestrator):
  WARDEN_WORKDIR        absolute path of the task worktree (required for path checks)
  WARDEN_ALLOWED_HOSTS  comma-separated host allowlist for egress (default: none)

Fail-closed: if the event can't be parsed or WARDEN_WORKDIR is unset, deny.
"""

import json
import os
import re
import sys
from pathlib import Path

# --- patterns that should never appear in a coding agent's tool arguments ---
SECRET_PATTERNS = [
    re.compile(r"\b\d{8,12}:[A-Za-z0-9_-]{30,}\b"),       # Telegram bot token
    re.compile(r"\bsk-[A-Za-z0-9-]{20,}\b"),               # OpenAI-style key
    re.compile(r"\bsk-ant-[A-Za-z0-9-]{20,}\b"),           # Anthropic key
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),                   # AWS access key id
    re.compile(r"\bghp_[A-Za-z0-9]{36}\b"),                # GitHub PAT
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),     # private key block
    re.compile(r"\bTELEGRAM_BOT_TOKEN\b"),                 # env var name itself
]

# --- crude URL/host extraction for egress checks ---
URL_RE = re.compile(r"https?://([A-Za-z0-9.\-]+)")


def deny(reason: str):
    print(json.dumps({"decision": "block", "reason": f"WARDEN hook: {reason}"}))
    sys.exit(2)


def allow():
    sys.exit(0)


def main():
    raw = sys.stdin.read()
    try:
        event = json.loads(raw)
    except json.JSONDecodeError:
        deny("could not parse tool event (fail-closed)")

    tool = event.get("tool_name", "")
    tin = event.get("tool_input", {}) or {}
    # Flatten all string values of the tool input for scanning.
    blob = json.dumps(tin)

    # 1. Secret patterns anywhere in the arguments.
    for pat in SECRET_PATTERNS:
        if pat.search(blob):
            deny("tool arguments contain a secret-like pattern")

    # 2. Filesystem confinement for file-touching tools.
    workdir = os.environ.get("WARDEN_WORKDIR")
    file_tools = {"Write", "Edit", "Read", "NotebookEdit", "MultiEdit"}
    if tool in file_tools:
        if not workdir:
            deny("WARDEN_WORKDIR unset; cannot verify path confinement (fail-closed)")
        wd = Path(workdir).resolve()
        # Candidate path fields across tool variants.
        path_val = tin.get("file_path") or tin.get("path") or tin.get("notebook_path")
        if path_val:
            try:
                target = Path(path_val).resolve()
            except Exception:
                deny(f"unresolvable path: {path_val}")
            if not str(target).startswith(str(wd)):
                deny(f"path escapes task worktree: {target} not under {wd}")

    # 3. Network egress: Bash and web tools must target allowed hosts only.
    allowed_hosts = {h.strip() for h in
                     os.environ.get("WARDEN_ALLOWED_HOSTS", "").split(",") if h.strip()}
    if tool in {"Bash", "WebFetch", "WebSearch"}:
        for host in URL_RE.findall(blob):
            if host not in allowed_hosts:
                deny(f"network egress to non-allowlisted host: {host}")

    allow()


if __name__ == "__main__":
    main()
