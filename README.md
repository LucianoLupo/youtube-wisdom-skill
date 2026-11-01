# youtube-wisdom

A [Claude Code](https://docs.anthropic.com/en/docs/claude-code) skill that extracts wisdom, insights, and actionable takeaways from YouTube videos.

Give it a YouTube URL and it fetches a timestamped transcript, decides whether the video has anything worth looking at (slides, diagrams, demos, code on screen), and produces a structured markdown analysis. For visual talks it pulls hand-picked frames and embeds them next to the insights they support, so numbers and diagrams that the speaker never says out loud still make it into the analysis.

## Requirements

- [Claude Code](https://docs.anthropic.com/en/docs/claude-code)
- Python 3 with [Pillow](https://pypi.org/project/pillow/) (`python3 -m pip install Pillow`)
- [yt-dlp](https://github.com/yt-dlp/yt-dlp) (`brew install yt-dlp` or `pip install yt-dlp`)
- [ffmpeg](https://ffmpeg.org/) (`brew install ffmpeg`)
- Optional, PDF export only: Google Chrome or Chromium, and poppler (`brew install poppler`)

Check everything with:

```bash
python3 ~/.claude/skills/youtube-wisdom/scripts/video_kit.py doctor
```

## Install

Clone into your Claude Code skills directory:

```bash
git clone https://github.com/LucianoLupo/youtube-wisdom-skill.git ~/.claude/skills/youtube-wisdom
```

Or add as a git submodule if you version-control your skills:

```bash
git submodule add https://github.com/LucianoLupo/youtube-wisdom-skill.git ~/.claude/skills/youtube-wisdom
```

## Usage

In Claude Code, say:

```
extract wisdom from https://www.youtube.com/watch?v=VIDEO_ID
```

Or any variation like:
- "summarise this video: [URL]"
- "what are the key points from [URL]?"
- "pull the slides and timestamps from [URL]"

## Configuration

All optional, via environment variables:

| Variable | Default | Purpose |
|---|---|---|
| `YOUTUBE_WISDOM_DIR` | `<repo>/wisdom/` (gitignored) | where analyses are saved |
| `YOUTUBE_WISDOM_LANG` | the video's own language | preferred subtitle language, e.g. `es` |
| `YOUTUBE_WISDOM_CACHE` | `~/.cache/youtube-wisdom` | downloaded videos and frame dumps; must be outside any git repo |
| `YOUTUBE_WISDOM_VAULT` | unset | Obsidian vault root; when set, the skill also prints wikilink embeds and a command to copy frames into `<vault>/attachments/` |

```bash
export YOUTUBE_WISDOM_DIR="$HOME/my-custom-wisdom-dir"
```

Language is detected automatically: the skill prefers author subtitles in the video's spoken language, then the original auto-generated track, and never picks a machine translation. `YOUTUBE_WISDOM_LANG` only matters when neither exists.

## Output

For each video, the skill creates a directory under `$YOUTUBE_WISDOM_DIR`:

```
YYYY-MM-DD-video-topic/
├── <title> - transcript.txt   # [MM:SS]-timestamped transcript
├── <title> - analysis.md      # structured analysis, frames embedded inline
└── frames/                    # only for visual talks: the picked frames
    └── YYYY-MM-DD-video-topic-000213.jpg
```

Videos and raw frame dumps stay in the cache and never land in the output directory; `video_kit.py prune` reclaims the cache when you are done.

The analysis includes:
- Summary and a one-line simplified explanation
- Key insights with supporting details, including figures read off the slides
- Notable quotes with timestamps
- Actionable takeaways
- Additional resources mentioned
- A structured breakdown by theme for long videos

## Upgrading from the first version

`scripts/download_transcript.sh` is replaced by `scripts/video_kit.py fetch`. The default subtitle language is no longer `en`: the video's own language is detected, so non-English videos no longer need a language code.

## Limitations

- Requires captions (author or auto-generated). Videos without any fail with a clear error; audio transcription is not automated.
- Age-restricted or members-only videos may require [yt-dlp authentication](https://github.com/yt-dlp/yt-dlp#authentication-with-netrc).
- The notification script is macOS-only (automatically skipped on other platforms).

## License

[MIT](LICENSE)
