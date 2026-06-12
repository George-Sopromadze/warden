#!/usr/bin/env python3
"""
WARDEN Telegram approval bot (Phase 4 Step B).

Long-polls getUpdates and handles Approve / Reject / Show diff callbacks for
tasks in awaiting_approval. Run it as a separate always-on process:

    python3 pipeline/approval_bot.py

Security posture (Phase 4/6):
- Every update is checked against the hard-coded NUMERIC user id from
  TELEGRAM_USER_ID. Usernames are never trusted (they can be changed/spoofed).
  Anything from another id is ignored and logged.
- Chat text is never executed; the only accepted inputs are the three
  callback buttons this bot itself created.
- Approvals are bound to the diff hash at request time; gate_approve.sh
  re-verifies the binding, so a stale approval can never pass the gate.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TASKS = ROOT / "tasks"
SECRETS = Path(os.environ.get("WARDEN_SECRETS",
                              Path.home() / ".warden" / "secrets.env"))


def load_secrets() -> dict:
    env = {}
    if SECRETS.exists():
        for line in SECRETS.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env


CFG = load_secrets()
TOKEN = CFG.get("TELEGRAM_BOT_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN")
ALLOWED_ID = CFG.get("TELEGRAM_USER_ID") or os.environ.get("TELEGRAM_USER_ID")
API = f"https://api.telegram.org/bot{TOKEN}"


def tg(method: str, **params):
    """Bot API call via curl (macOS system trust store, no Python SSL)."""
    cmd = ["curl", "-sS", "-m", "70", f"{API}/{method}"]
    for k, v in params.items():
        cmd += ["--data-urlencode", f"{k}={v}"]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"curl failed: {proc.stderr.strip()[:200]}")
    return json.loads(proc.stdout)


def log(msg: str) -> None:
    print(f"[approval-bot] {time.strftime('%H:%M:%S')} {msg}", flush=True)


def diff_hash(task_dir: Path) -> str:
    return subprocess.run(
        [str(ROOT / "pipeline" / "diff_hash.sh"), str(task_dir)],
        capture_output=True, text=True).stdout.strip()


def workdir_diff(task_dir: Path, limit: int = 3500) -> str:
    wd = task_dir / "workdir"
    if not (wd / ".git").exists():
        return "(workdir is not a git repo)"
    root = subprocess.run(["git", "rev-list", "--max-parents=0", "HEAD"],
                          cwd=wd, capture_output=True, text=True).stdout.strip()
    diff = subprocess.run(["git", "diff", f"{root}..HEAD"],
                          cwd=wd, capture_output=True, text=True).stdout
    if len(diff) > limit:
        return diff[:limit] + f"\n... truncated; full diff in {wd}"
    return diff or "(empty diff)"


def write_approval(task_dir: Path, approved: bool, who: str) -> None:
    record = {
        "approved": approved,
        "by": who,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "diff_hash": diff_hash(task_dir),
    }
    out = task_dir / "artifacts" / "approval.json"
    tmp = out.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(record, indent=2) + "\n")
    os.replace(tmp, out)


def set_status(task_dir: Path, status: str) -> None:
    sp = task_dir / "state.json"
    state = json.loads(sp.read_text())
    state["status"] = status
    tmp = sp.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2) + "\n")
    os.replace(tmp, sp)


def resume_pipeline(task_id: str) -> None:
    subprocess.Popen([sys.executable, str(ROOT / "pipeline" / "run.py"),
                      "run", task_id,
                      "--agent-mode", os.environ.get("WARDEN_AGENT_MODE", "stub")])


def handle_callback(cb: dict) -> None:
    from_id = str(cb.get("from", {}).get("id", ""))
    if from_id != str(ALLOWED_ID):
        log(f"IGNORED callback from unauthorized id {from_id}")
        return

    data = cb.get("data", "")
    action, _, task_id = data.partition(":")
    # task ids come only from our own buttons, but never trust them blindly:
    task_dir = (TASKS / task_id).resolve()
    if not str(task_dir).startswith(str(TASKS.resolve())) or not task_dir.is_dir():
        log(f"IGNORED callback for unknown task {task_id!r}")
        return

    tg("answerCallbackQuery", callback_query_id=cb["id"])

    if action == "approve":
        state = json.loads((task_dir / "state.json").read_text())
        pending = state.get("pending_approval") or {}
        current = diff_hash(task_dir)
        if pending.get("diff_hash") and pending["diff_hash"] != current:
            tg("sendMessage", chat_id=ALLOWED_ID, text=(
                f"⚠️ {task_id}: diff changed since approval was requested — "
                f"approval void. Re-run the pipeline to request again."))
            log(f"{task_id}: approval void, hash drift")
            return
        write_approval(task_dir, True, f"telegram:{from_id}")
        set_status(task_dir, "pending")
        resume_pipeline(task_id)
        tg("sendMessage", chat_id=ALLOWED_ID,
           text=f"✅ {task_id} approved (diff {current[:8]}). Pipeline resuming.")
        log(f"{task_id}: approved")

    elif action == "reject":
        write_approval(task_dir, False, f"telegram:{from_id}")
        # Rejection routes through the orchestrator's escalation on next run;
        # mark it directly so it halts even if nothing re-runs it.
        set_status(task_dir, "escalated")
        (task_dir / "NEEDS_HUMAN").write_text(
            f"rejected via Telegram by {from_id}\n")
        tg("sendMessage", chat_id=ALLOWED_ID,
           text=f"⛔ {task_id} rejected and escalated.")
        log(f"{task_id}: rejected")

    elif action == "diff":
        tg("sendMessage", chat_id=ALLOWED_ID,
           text=f"📄 {task_id} diff:\n\n{workdir_diff(task_dir)}")
        log(f"{task_id}: diff sent")


def main() -> None:
    if not TOKEN or not ALLOWED_ID:
        sys.exit(f"TELEGRAM_BOT_TOKEN / TELEGRAM_USER_ID not set "
                 f"(looked in {SECRETS})")
    if not str(ALLOWED_ID).isdigit():
        sys.exit("TELEGRAM_USER_ID must be the NUMERIC id (see @userinfobot), "
                 "never a username")
    log(f"polling as bot, whitelisted user {ALLOWED_ID}")
    offset = 0
    while True:
        try:
            resp = tg("getUpdates", offset=offset, timeout=50)
            if not resp.get("ok", False):
                log(f"getUpdates rejected: {str(resp)[:200]}")
                time.sleep(5)
                continue
            for upd in resp.get("result", []):
                offset = upd["update_id"] + 1
                log(f"received update {upd['update_id']}: {', '.join(k for k in upd if k != 'update_id')}")
                if "callback_query" in upd:
                    handle_callback(upd["callback_query"])
                # All other update types (messages, etc.) are deliberately
                # ignored: chat text is never a command (Phase 6 policy).
        except KeyboardInterrupt:
            raise
        except Exception as e:
            log(f"poll error: {e}; retrying in 5s")
            time.sleep(5)


if __name__ == "__main__":
    main()
