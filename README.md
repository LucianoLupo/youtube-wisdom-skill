# youtube-wisdom

A [Claude Code](https://docs.anthropic.com/en/docs/claude-code) skill that extracts wisdom, insights, and actionable takeaways from YouTube videos.

Give it a YouTube URL and it downloads the transcript, analyses the content, and produces a structured markdown analysis with key insights, notable quotes, and actionable takeaways.

## Requirements

- [Claude Code](https://docs.anthropic.com/en/docs/claude-code)
- [yt-dlp](https://github.com/yt-dlp/yt-dlp) (`brew install yt-dlp` or `pip install yt-dlp`)
- Python 3

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
- "analyse this YouTube video [URL]"

## Configuration

Set the `YOUTUBE_WISDOM_DIR` environment variable to change where analyses are saved:

```bash
export YOUTUBE_WISDOM_DIR="$HOME/my-custom-wisdom-dir"
```

Default: `~/Documents/Wisdom`

## Output

For each video, the skill creates a directory under `$YOUTUBE_WISDOM_DIR`:

```
YYYY-MM-DD-Video-Topic/
├── metadata.json              # Video metadata from yt-dlp
├── <title>.en.json3           # Raw subtitle data
├── <title> - transcript.txt   # Plain text transcript
└── <title> - analysis.md      # Structured analysis
```

The analysis includes:
- Summary with simplified explanation
- Key takeaways
- Key insights with supporting details
- Notable quotes with context
- Structured breakdown by theme
- Actionable takeaways
- Additional resources mentioned

## Limitations

- Requires English captions (manual or auto-generated). Videos without English subtitles will fail with a clear error message.
- Age-restricted or members-only videos may require [yt-dlp authentication](https://github.com/yt-dlp/yt-dlp#authentication-with-netrc).
- The notification script is macOS-only (automatically skipped on other platforms).

## License

[MIT](LICENSE)
