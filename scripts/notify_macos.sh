#!/usr/bin/env bash
# Send a desktop notification on macOS.
# Usage: TITLE="title" MESSAGE="message" [PLAY_SOUND=true] [DIR="/path/"] ./notify_macos.sh
#
# Environment:
#   TITLE       Notification title   (default: "Notification")
#   MESSAGE     Notification body    (default: "Task complete")
#   PLAY_SOUND  Play alert sound     (default: false)
#   DIR         Open directory after  (default: none)

set -euo pipefail

if [[ "$(uname -s)" != "Darwin" ]]; then
    echo "Skipping notification: not running on macOS"
    exit 0
fi

TITLE="${TITLE:-Notification}"
MESSAGE="${MESSAGE:-Task complete}"
PLAY_SOUND="${PLAY_SOUND:-false}"
DIR="${DIR:-}"

# Sanitise inputs for AppleScript (escape backslashes and double quotes).
sanitise() { printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'; }
SAFE_TITLE=$(sanitise "$TITLE")
SAFE_MESSAGE=$(sanitise "$MESSAGE")

SCRIPT="display notification \"$SAFE_MESSAGE\" with title \"$SAFE_TITLE\""

if [[ "$PLAY_SOUND" == "true" ]]; then
    SCRIPT="$SCRIPT sound name \"default\""
fi

osascript -e "$SCRIPT"

if [[ -n "$DIR" && -d "$DIR" ]]; then
    open "$DIR"
fi

echo "Notification sent: $TITLE - $MESSAGE"
