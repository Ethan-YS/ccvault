#!/bin/bash
# Rebuild the ccvault archive once, then exit. Meant to be run by a scheduler
# (launchd on macOS, cron on Linux). Safe to run anytime — it's incremental and
# append-only, so it never deletes anything you've already archived.
DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON=""
for c in /opt/homebrew/bin/python3 /opt/homebrew/opt/python@3.14/bin/python3.14 /usr/local/bin/python3 /usr/bin/python3; do
  [ -x "$c" ] && PYTHON="$c" && break
done
if [ -z "$PYTHON" ]; then
  echo "[$(date '+%Y-%m-%d %H:%M')] python3 not found"
  exit 1
fi
"$PYTHON" "$DIR/ccvault.py" --update-only
echo "[$(date '+%Y-%m-%d %H:%M')] ccvault auto-update finished"
