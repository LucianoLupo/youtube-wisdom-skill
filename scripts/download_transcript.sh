#!/usr/bin/env bash
# Download YouTube transcript and metadata (no video file).
# Usage: ./download_transcript.sh <youtube-url> [lang-code]
#
# Environment:
#   YOUTUBE_WISDOM_DIR   Output directory (default: <repo>/wisdom)
#   YOUTUBE_WISDOM_LANG  Subtitle language code (default: en). CLI arg wins if both set.

set -euo pipefail

if [[ -z "${1:-}" ]]; then
    echo "Usage: $0 <youtube-url> [lang-code]"
    exit 1
fi

URL="$1"
LANG_CODE="${2:-${YOUTUBE_WISDOM_LANG:-en}}"
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
REPO_ROOT="$( cd "${SCRIPT_DIR}/.." && pwd )"
OUTPUT_DIR="${YOUTUBE_WISDOM_DIR:-${REPO_ROOT}/wisdom}"

# Extract video ID — handles standard, short, and embed URLs.
VIDEO_ID=$(python3 -c "
import re, sys
url = sys.argv[1]
patterns = [
    r'(?:v=|/v/|youtu\.be/|/embed/)([a-zA-Z0-9_-]{11})',
    r'^([a-zA-Z0-9_-]{11})$',
]
for p in patterns:
    m = re.search(p, url)
    if m:
        print(m.group(1))
        sys.exit(0)
sys.exit(1)
" "$URL" 2>/dev/null)

if [[ -z "$VIDEO_ID" ]]; then
    echo "Error: Could not extract video ID from URL"
    exit 1
fi

VIDEO_DIR="${OUTPUT_DIR}/${VIDEO_ID}"
mkdir -p "$VIDEO_DIR"

echo "Downloading transcript for video: $VIDEO_ID"
echo "Output directory: $VIDEO_DIR"

# Download subtitles only (no video) in JSON3 format.
if ! yt-dlp \
    --skip-download \
    --write-subs \
    --write-auto-subs \
    --sub-langs "${LANG_CODE}.*,${LANG_CODE}" \
    --sub-format "json3" \
    --restrict-filenames \
    --output "${VIDEO_DIR}/%(title)s" \
    "$URL"; then
    echo "Warning: yt-dlp subtitle download had errors (may still have partial results)"
fi

# Save video metadata.
if ! yt-dlp \
    --skip-download \
    --print-json \
    --restrict-filenames \
    "$URL" > "${VIDEO_DIR}/metadata.json"; then
    echo "Warning: Failed to download video metadata"
fi

# Find the JSON3 subtitle file. Prefer manual subs over auto-generated.
JSON3_FILE=$(find "$VIDEO_DIR" -name "*.json3" ! -name "*-orig*" 2>/dev/null | head -1)
if [[ -z "$JSON3_FILE" ]]; then
    JSON3_FILE=$(find "$VIDEO_DIR" -name "*.json3" 2>/dev/null | head -1)
fi

if [[ -z "$JSON3_FILE" || ! -f "$JSON3_FILE" ]]; then
    echo "Error: No subtitle file found. The video may not have '${LANG_CODE}' captions available."
    echo "Video directory: $VIDEO_DIR"
    exit 1
fi

# Derive transcript filename: strip any language tag and .json3 extension.
BASE_NAME=$(basename "$JSON3_FILE" | sed -E 's/\.[a-zA-Z-]+\.json3$//' | sed 's/\.json3$//')
TRANSCRIPT_FILE="${VIDEO_DIR}/${BASE_NAME} - transcript.txt"

# Parse JSON3 subtitle format and extract plain text.
python3 - "$JSON3_FILE" <<'PYEOF' > "$TRANSCRIPT_FILE"
import json, sys

with open(sys.argv[1], "r") as f:
    data = json.load(f)

parts = []
for event in data.get("events", []):
    for seg in event.get("segs", []):
        text = seg.get("utf8", "").strip()
        if text and text != "\n":
            parts.append(text)

print(" ".join(" ".join(parts).split()))
PYEOF

echo ""
echo "Transcript saved to: $TRANSCRIPT_FILE"
echo "Video directory: $VIDEO_DIR"
