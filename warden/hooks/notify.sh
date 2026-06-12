#!/usr/bin/env bash
# WARDEN notifier (Phase 4 Step A).
# Usage 1 (orchestrator): notify.sh "message text"
# Usage 2 (Claude Code hook): receives hook JSON on stdin, no args.
# Secrets come from ~/.warden/secrets.env (override path via WARDEN_SECRETS):
#   TELEGRAM_BOT_TOKEN=123456:ABC...
#   TELEGRAM_USER_ID=123456789        # numeric, from @userinfobot
# With no token configured it degrades to appending to a local log, exit 0 —
# the pipeline must never fail because notifications are unconfigured.
set -euo pipefail

SECRETS="${WARDEN_SECRETS:-$HOME/.warden/secrets.env}"
[ -f "$SECRETS" ] && set -a && source "$SECRETS" && set +a

if [ "$#" -ge 1 ]; then
  TEXT="$1"
else
  TEXT="claude-code-hook: $(cat | head -c 1000)"
fi

LOG="${WARDEN_LOG_DIR:-/tmp}/warden-notify.log"
echo "$(date -Is) $TEXT" >> "$LOG"

if [ -n "${TELEGRAM_BOT_TOKEN:-}" ] && [ -n "${TELEGRAM_USER_ID:-}" ]; then
  curl -sS -m 10 "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
    --data-urlencode "chat_id=${TELEGRAM_USER_ID}" \
    --data-urlencode "text=${TEXT}" \
    --data-urlencode "disable_web_page_preview=true" > /dev/null \
    || echo "$(date -Is) SEND-FAILED $TEXT" >> "$LOG"
fi
exit 0
