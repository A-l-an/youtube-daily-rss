#!/bin/bash
# Reliable daily trigger for the YouTube RSS digest.
#
# Fired by ~/Library/LaunchAgents/com.alan.youtube-daily-rss.plist at 10:00 local
# time. GitHub's own scheduled-cron dispatch is chronically late/unreliable (runs
# have landed 4-5h late, or not at all), so instead we kick the workflow ourselves
# via workflow_dispatch, which GitHub processes within seconds and routes to the
# self-hosted runner on this same Mac. The full pipeline (discovery -> transcript/
# ASR -> summary -> commit -> Pages deploy) is unchanged; only the trigger moves
# off GitHub cron. The GitHub `schedule:` trigger stays in place as a backup.
set -u
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"

REPO="A-l-an/youtube-daily-rss"
LOG_DIR="$HOME/.config/youtube-daily-rss"
LOG="$LOG_DIR/launchd-trigger.log"
mkdir -p "$LOG_DIR"
ts() { date '+%Y-%m-%d %H:%M:%S %Z'; }

echo "[$(ts)] triggering $REPO daily.yml ..." >> "$LOG"
if gh workflow run daily.yml -R "$REPO" >> "$LOG" 2>&1; then
  echo "[$(ts)] dispatch OK" >> "$LOG"
else
  rc=$?
  echo "[$(ts)] dispatch FAILED (exit $rc) -- is gh authenticated & keychain unlocked?" >> "$LOG"
  exit "$rc"
fi
