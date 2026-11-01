---
name: youtube-wisdom
description: Extract wisdom, insights, and actionable takeaways from YouTube videos, with timestamped transcripts and - for talks with slides, demos or diagrams - hand-picked frames embedded in the analysis. Use when the user says "extract wisdom from [URL]", or asks to analyse, summarise, break down, or extract key learnings from YouTube content, or asks for the slides/frames/timestamps from a video. Optionally creates a link note in the user's Obsidian vault.
---

# YouTube Wisdom Extraction

All work goes through one tool. Shell variables do not survive between tool calls, so substitute
the real paths rather than relying on `$DEST` from an earlier block.

```bash
python3 ~/.claude/skills/youtube-wisdom/scripts/video_kit.py doctor
```

`doctor` reports the binaries, the cache, and the output dir. The mp4 and the raw frame dump stay
in the cache (`~/.cache/youtube-wisdom/`); only `install` writes into the output dir. Do not copy
video or scene dumps there by hand.

**Requirements:** `yt-dlp`, `ffmpeg`, Pillow (`python3 -m pip install Pillow`). PDF escape hatch
only: Google Chrome or Chromium, and `pdftoppm` (poppler). `doctor` exits non-zero only if a
required one is missing.

## Configuration

All optional, via environment variables:

| Variable | Default | Purpose |
|---|---|---|
| `YOUTUBE_WISDOM_DIR` | `<skill dir>/wisdom/` (gitignored) | where analyses are installed |
| `YOUTUBE_WISDOM_LANG` | the video's own language | preferred subtitle language, e.g. `es` |
| `YOUTUBE_WISDOM_CACHE` | `~/.cache/youtube-wisdom` | videos and frame dumps (must be outside any git repo) |
| `YOUTUBE_WISDOM_VAULT` | unset | Obsidian vault root; when set, `install` prints a frame-mirror command |

Below, `<output dir>` means the resolved `YOUTUBE_WISDOM_DIR`; `doctor` prints it.

## Step 1: Fetch the transcript (not the video)

```bash
python3 ~/.claude/skills/youtube-wisdom/scripts/video_kit.py fetch "<youtube-url>"
```

Capture the final `WORKDIR=<path>` line; every later command needs it. Handles any language
automatically - it prefers author subtitles in the spoken language, then the original ASR track
(never a machine translation).

- If it prints `!! ALREADY EXTRACTED`, stop and ask the user whether to redo it or open the
  existing analysis.
- If it exits non-zero saying no usable subtitle track, read the `manual=` / `auto-orig=` lists it
  printed. If a track is listed, re-run with `--sub-langs <tag>` to force it. If both are empty,
  stop and tell the user - audio transcription is not automated here; do not download audio or
  video to transcribe by hand.

Match the analysis language to the source unless the user asks otherwise.

## Step 2: Read the transcript

Read `<workdir>/transcript.md`. It is `[MM:SS]` blocks of roughly 20 seconds, switching to
`[H:MM:SS]` past the one-hour mark - `[1:05:30]` is 65 minutes in, not 1 minute. Read it in full
for anything under ~20 minutes; read in ranges for very long videos. Every quote and insight you
write later must carry the timestamp of the block it came from.

## Step 3: Decide - is there anything to look at?

Decide from the transcript, before downloading anything. State the verdict and the reason in one
line.

**Visual branch** - talks with slides, technical walkthroughs, screen shares, live coding,
whiteboard explanations, product demos, anything with charts or architecture diagrams. Tells in the
transcript: "as you can see here", "on the left", "this diagram", "let me show you", "if I run
this", references to figures or numbers on screen, gaps in the caption stream during a demo.

Those same tells are the signal that the transcript is *lossy*: the speaker is pointing at
information they never say out loud. Frames are how that information is recovered (Step 7), so the
more the transcript leans on unspoken visuals, the more the visual branch is worth its download.

**Audio-only branch** - podcasts, interviews, panels without slides, monologues, fireside chats.
Nothing to show, so do not download an mp4. Skip to Step 6.

When in doubt, default to audio-only or ask in one line. Never guess-and-download: a 720p slide
talk is roughly 15 MB per 10 minutes, so even a two-hour talk lands near 200 MB, well inside the
600 MB `--max-mb` ceiling; high-motion footage can be 5x that, which is why `fetch --video` prints
the estimate before it downloads.

## Step 4 (visual branch only): Video, frames, contact sheet

```bash
python3 ~/.claude/skills/youtube-wisdom/scripts/video_kit.py fetch "<youtube-url>" --video   # same workdir, adds the video only
python3 ~/.claude/skills/youtube-wisdom/scripts/video_kit.py frames --workdir <workdir>
python3 ~/.claude/skills/youtube-wisdom/scripts/video_kit.py sheet  --workdir <workdir>
```

`fetch --video` prints an estimated size and refuses above 600 MB; raise `--max-mb` or lower
`--height` deliberately if a long talk is worth it.

**Read the ONE contact sheet, not the individual frames.** Use `Read` on `<workdir>/sheet.jpg`
(or `sheet-01.jpg`, ...). Every cell is labelled `frames/scene_007.jpg  02:13` - the path and the
timestamp, the only two strings you need. If there are more than three sheets, re-run
`frames --threshold 0.45` and `sheet` instead of reading them all.

## Step 5 (visual branch only): Second targeted pass

Scene detection fires **on** the cut, so many scene frames are talking heads, black transitions,
or a diagram caught mid-animation. For each moment you actually want, pick a timestamp 2-4 seconds
after the cut and re-extract:

```bash
python3 ~/.claude/skills/youtube-wisdom/scripts/video_kit.py frames --workdir <workdir> --no-scenes --at 1:37,4:12,11:05
python3 ~/.claude/skills/youtube-wisdom/scripts/video_kit.py sheet  --workdir <workdir>
```

`--no-scenes` keeps the existing scene dump and the threshold you tuned in Step 4; without it this
re-runs the full scene pass at the default `--threshold 0.35`. Add `--clear-picks` if you are
replacing earlier picks rather than adding to them.

Read the regenerated sheet once. The `pick/t*.jpg` cells appear alongside the scene cells, sorted
by timestamp. Iterate at most twice; if a frame is still unusable, drop it - a missing figure beats
an illegible one.

There is no cap on how many frames you install. Keep every frame that shows something the prose
cannot say as compactly - a chart with real numbers, an architecture diagram, a table, code on
screen. A frame you install and never embed costs a few hundred KB; a frame you skipped costs
re-downloading the video to recover. When in doubt, install it.

## Step 6: Install the durable artifacts

```bash
python3 ~/.claude/skills/youtube-wisdom/scripts/video_kit.py install --workdir <workdir> \
  --dest "<output dir>/$(date +%Y-%m-%d)-<concise-description>" \
  --frame pick/t97.jpg --frame frames/scene_014.jpg --frame pick/t665.jpg
```

The description is 1-6 words, hyphens only. Audio-only branch: the same command with no `--frame`
arguments.

`install` copies the transcript, re-encodes the picked frames into `<dest>/frames/`, and prints
`DEST=<path>` plus the exact markdown embed lines (and, with `YOUTUBE_WISDOM_VAULT` set, the
wikilink lines and the vault mirror command). Copy those lines; never hand-write a frame path. It
also prints the cache size and the `prune` command - mention that to the user rather than pruning
unasked.

## Step 7: Mine the frames, then write the analysis

**Visual branch: `Read` every picked frame before writing a word.** The frames are evidence, not
decoration. In a diagram-heavy talk the transcript degenerates into "as you can see here", "this
number", "on the left" - the actual content exists only on screen.

From each frame, extract what the words never said: exact figures and axis labels, the boxes and
arrows of an architecture diagram, values in a table, commands or code on screen, the units and
source under a chart. Key Insights and the Structured Breakdown should carry those figures, each
with its timestamp. Write every caption from what you actually see in the frame.

Save to `<dest>/<title> - analysis.md`. Adapt sections to the content - omit any that would be empty
or forced.

```markdown
---
title: "[Video Title]"
url: "[URL]"
speaker: "[Channel/Speaker]"
date: [YYYY-MM-DD]
tags:
  - wisdom
  - [topic]
---

# [Title]

## Summary
[2-3 sentence overview]

### ELI10
[1-2 sentences a child could understand]

## Key Insights
- [Insight 1]
  - [Supporting detail]

## Notable Quotes
> "[Quote]" - [12:34]

## Actionable Takeaways
1. [Specific action]

## Additional Resources
[Tools, links, references mentioned. Omit if none.]
```

**Guidelines:**
- High signal-to-noise ratio - no fluff or filler
- Quotes carry the timestamp of their transcript block
- Embed frames where they support the point, never as a gallery at the bottom, using the exact
  lines `install` printed with a real caption substituted:
  `![02:13](frames/2026-08-10-agent-harness-000213.jpg "the three-tier agent loop")`
- For long videos, add a "Structured Breakdown" section with thematic subsections
- If tools or resources are mentioned, task a sub-agent to look them up
- No em-dashes or smart quotes; bold only where truly needed

## Step 8: Self-review

Accuracy to source, conciseness, no empty sections, plausible timestamps, and every embedded frame
path resolves (`ls <dest>/frames/`).

## Step 9: Vault note (optional, only with `YOUTUBE_WISDOM_VAULT` set)

Offer it. If the vault has its own conventions file (e.g. a `CLAUDE.md` at its root), read it first.
Otherwise default to a short literature note: TL;DR, top 3-4 insights, action items as `- [ ]`
tasks, `[[wikilink]]` connections, and the absolute path to the full analysis. If there are frames,
run the `mkdir -p ... && cp ...` mirror command `install` printed verbatim, then embed them as
`![[filename.jpg]]` with an italic caption underneath.

## Step 10: Notify (macOS only; skipped elsewhere)

```bash
TITLE="Wisdom Extracted" MESSAGE="<3-5 word description>" PLAY_SOUND=true DIR="<dest>" \
  bash ~/.claude/skills/youtube-wisdom/scripts/notify_macos.sh
```

## Variations

- **Multiple videos:** process sequentially, write one comparative summary file highlighting common
  themes, notify once at the end.
- **Topic-specific focus:** grep the transcript for the keywords and extract only those blocks.
- **Shareable PDF (optional):** `pdf <analysis.md> <out.pdf>` renders a shareable dark-theme PDF and
  `check <out.pdf>` rasterises it so you can verify the layout. Only when the user asks for it -
  markdown is the deliverable. Restyle by editing the `:root` tokens in `video_kit.py`. Captions
  must not contain double quotes (the figure parser reads `"` as the caption delimiter). Write the
  `.pdf` outside the output dir; `pdf` refuses to write there unless you pass `--allow-durable`.
