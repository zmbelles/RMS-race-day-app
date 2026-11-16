#!/usr/bin/env bash
# Pull the latest code from GitHub and restart the app. The database updates itself on start (keeping a backup).
#   sudo bash /opt/rms-race-day/deploy/update.sh
set -euo pipefail
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
[[ $EUID -eq 0 ]] || { echo "Run with sudo." >&2; exit 1; }
git -C "$APP_DIR" pull --ff-only
"$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"
systemctl restart rms-race-day
systemctl --no-pager --lines=5 status rms-race-day
