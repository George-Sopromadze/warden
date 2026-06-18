#!/usr/bin/env python3
"""
WARDEN secret scanner (Phase 6).

Audits a task's logs and artifacts (or any path) for leaked secret patterns.
Roadmap done-when: "a grep of all logs finds zero secrets."

    python3 hooks/scan_secrets.py tasks/<task-id>      scan one task
    python3 hooks/scan_secrets.py tasks                 scan all tasks
Exit 0 = clean, exit 1 = secrets found (so it can gate CI later).
"""
import re
import sys
from pathlib import Path

PATTERNS = {
    "telegram_token": re.compile(r"\b\d{8,12}:[A-Za-z0-9_-]{30,}\b"),
    "openai_key": re.compile(r"\bsk-[A-Za-z0-9-]{20,}\b"),
    "anthropic_key": re.compile(r"\bsk-ant-[A-Za-z0-9-]{20,}\b"),
    "aws_key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "github_pat": re.compile(r"\bghp_[A-Za-z0-9]{36}\b"),
    "private_key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
}

def scan(root: Path) -> int:
    hits = 0
    for p in root.rglob("*"):
        if not p.is_file() or ".git" in p.parts:
            continue
        try:
            text = p.read_text(errors="ignore")
        except Exception:
            continue
        for name, pat in PATTERNS.items():
            for m in pat.finditer(text):
                hits += 1
                print(f"LEAK [{name}] in {p}: ...{m.group()[:6]}... (redacted)")
    return hits

if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("tasks")
    n = scan(target)
    if n == 0:
        print(f"clean: no secrets found in {target}")
        sys.exit(0)
    print(f"FAILED: {n} secret-like string(s) found")
    sys.exit(1)
