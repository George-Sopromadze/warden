#!/usr/bin/env bash
# Phase 4 will replace the log line with a Telegram Bot API curl.
# Receives hook JSON on stdin per Claude Code hook contract.
set -euo pipefail
payload=$(cat)
echo "$(date -Is) NOTIFY $payload" >> "${WARDEN_LOG_DIR:-/tmp}/warden-notify.log"
exit 0
