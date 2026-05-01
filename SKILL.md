---
name: youtube-wisdom
description: Extract wisdom, insights, and actionable takeaways from YouTube videos. Use when asked to analyse, summarise, or extract key learnings from YouTube content. Downloads transcripts via yt-dlp and produces structured analysis. Triggers on "extract wisdom from [URL]", "summarise this video", "what are the key points", or any request to analyse YouTube content.
---

# YouTube Wisdom Extraction

Extract meaningful insights from YouTube videos by downloading transcripts and performing structured analysis.

## Prerequisites

- `yt-dlp` installed (`brew install yt-dlp` / `pip install yt-dlp`)
- `python3` available on PATH

## Configuration

- `YOUTUBE_WISDOM_DIR` — output directory (default: `~/Documents/Wisdom`).
- `YOUTUBE_WISDOM_LANG` — subtitle language code (default: `en`). Also accepted as a 2nd CLI arg, which takes precedence.

## Workflow

### Step 1: Download Transcript

```bash
bash ~/.claude/skills/youtube-wisdom/scripts/download_transcript.sh <youtube-url> [lang-code]
```

Creates: `$YOUTUBE_WISDOM_DIR/<video-id>/` with transcript and metadata.

If the user mentions the video is in a non-English language (e.g. "this Spanish video"), pass the matching code (`es`, `pt`, `fr`, etc.) as the 2nd argument. Match the analysis output language to the source unless the user asks otherwise.

### Step 2: Read Transcript

Read the transcript file from the output directory shown by the script.

### Step 3: Rename Directory

Replace the video ID with a dated, descriptive name:

```bash
mv "$YOUTUBE_WISDOM_DIR/<video-id>" "$YOUTUBE_WISDOM_DIR/$(date +%Y-%m-%d)-<Concise-Description>"
```

Keep descriptions to 1-6 hyphenated words, no spaces or special characters.

### Step 4: Analyse and Extract

Maintain high signal-to-noise ratio. Extract:

**Key Insights**: Main ideas, core arguments, expert recommendations, counterintuitive findings.

**Notable Quotes**: Memorable statements with context. Preserve exact wording.

**Structured Summary**: Hierarchical breakdown by themes/sections.

**Actionable Takeaways**: Specific, executable steps. Prioritise practical over theoretical. Include tools/resources mentioned.

### Step 5: Write Analysis

Save to: `$YOUTUBE_WISDOM_DIR/<date>-<description>/<title> - analysis.md`

```markdown
# Video Analysis: [Title]

**Video URL:** [URL]
**Channel:** [Channel name]
**Analysis Date:** [YYYY-MM-DD]

## Summary
[2-3 sentence overview]

### Simplified Explanation
[Explain like I'm 10: 1-2 sentences]

### Key Takeaways
- [Takeaway 1]
- [Takeaway 2]
- [Takeaway 3]

## Key Insights
- [Insight 1]
  - [Supporting detail]
- [Insight 2]
  - [Supporting detail]

## Notable Quotes
> "[Quote]"

Context: [If needed]

## Structured Breakdown
### [Section Title]
[Content]

## Actionable Takeaways
1. [Action 1]
2. [Action 2]

## Additional Resources
[Tools, links, references mentioned]
```

### Step 6: Self-Review

Verify: accuracy to source, concise writing, logical structure, proper markdown, no smart quotes or em-dashes.

### Step 7: Notify (macOS only)

```bash
TITLE="Wisdom Extracted" MESSAGE="<3-5 word description>" PLAY_SOUND=true DIR="<output-dir>" bash ~/.claude/skills/youtube-wisdom/scripts/notify_macos.sh
```

Skip on non-macOS systems.

## Multiple Videos

Process sequentially. Create comparative analysis highlighting common themes in a separate summary file. Notify only once at the end.

## Topic-Specific Focus

When requested, search transcript for keywords and extract only relevant content.

## Tips

- If a tool or resource is mentioned in the video, task a sub-agent to look it up for the Additional Resources section.
- Keep analysis concise. Avoid marketing speak and filler.
