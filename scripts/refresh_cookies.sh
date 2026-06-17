#!/bin/bash
# Re-export logged-in YouTube cookies for the daily digest pipeline.
#
# Run this on the runner Mac (with Chrome logged into YouTube) whenever the
# channels start coming back "link-only" again — that means the stored session
# cookies have rotated/expired. yt-dlp normally refreshes them after each run, so
# this should be rare, but it's the one manual lever the pipeline depends on.
#
#   bash scripts/refresh_cookies.sh
#
# Uses --cookies-from-browser (one-time macOS Keychain prompt -> click "Always
# Allow"), retries to dodge Chrome's cookie-DB write lock, and only overwrites the
# stored file once it actually contains the auth cookies.
set -u
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"

COOK="$HOME/.config/youtube-daily-rss/youtube-cookies.txt"
YTDLP="${YTDLP_BIN:-yt-dlp}"
PROBE="https://www.youtube.com/watch?v=dbRTLhk_PNs"   # any video; we only load the page
TMP="$(mktemp)"
mkdir -p "$(dirname "$COOK")"

command -v "$YTDLP" >/dev/null 2>&1 || { echo "yt-dlp not found (set YTDLP_BIN)"; exit 1; }

best=0
for i in 1 2 3 4 5 6; do
  "$YTDLP" --cookies-from-browser chrome --cookies "$TMP" \
    --skip-download --simulate "$PROBE" >/dev/null 2>&1 || true
  yl=$(grep -c "youtube.com" "$TMP" 2>/dev/null || echo 0)
  auth=$(grep -cE "LOGIN_INFO|SAPISID|__Secure-3PSID" "$TMP" 2>/dev/null || echo 0)
  echo "attempt $i: youtube_lines=$yl auth_cookies=$auth"
  if [ "$auth" -ge 3 ] && [ "$yl" -gt "$best" ]; then cp "$TMP" "$COOK"; best="$yl"; fi
done
rm -f "$TMP"

if [ "$best" -gt 0 ]; then
  chmod 600 "$COOK"
  echo "OK: refreshed $COOK ($(grep -c 'youtube.com' "$COOK") youtube cookie lines)"
else
  echo "FAILED: no logged-in cookies extracted. Is Chrome logged into YouTube? Click 'Always Allow' on the Keychain prompt." >&2
  exit 1
fi
