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

`YOUTUBE_WISDOM_DIR` — change where analyses are saved (default: `<repo>/wisdom/`, gitignored):

```bash
export YOUTUBE_WISDOM_DIR="$HOME/my-custom-wisdom-dir"
```

`YOUTUBE_WISDOM_LANG` — subtitle language code to fetch (default: `en`). For Spanish videos, ask Claude to "extract wisdom from [URL] in Spanish" or set:

```bash
export YOUTUBE_WISDOM_LANG="es"
```

Common codes: `en`, `es`, `pt`, `fr`, `de`, `it`, `ja`, `ko`, `zh`. Regional variants like `es-419` or `en-US` are matched automatically.

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

- Requires captions in the requested language (manual or auto-generated). Defaults to English; override per-video with a CLI arg or globally with `YOUTUBE_WISDOM_LANG`. Videos without captions in the requested language fail with a clear error message.
- Age-restricted or members-only videos may require [yt-dlp authentication](https://github.com/yt-dlp/yt-dlp#authentication-with-netrc).
- The notification script is macOS-only (automatically skipped on other platforms).

## License

[MIT](LICENSE)
