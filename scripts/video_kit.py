#!/usr/bin/env python3
"""video_kit - YouTube video -> timestamped transcript (+ optional frames) -> Wisdom artifact.

The subtitle, frame and PDF mechanics were first prototyped in a Spanish, PDF-first
`resumir-video` skill; the prose, the defaults and the safety model here are new.

Subcommands, in the order you normally run them:

  fetch   <url>                    subtitles -> transcript.md (+ meta.json, + video.mp4 with --video)
  frames  --workdir DIR            scene-change frames (+ targeted frames with --at)
  sheet   --workdir DIR            one labelled contact sheet, so you pick frames from 1 image
  install --workdir DIR --dest D   the ONLY path from cache into the output dir
  doctor                           check binaries, Pillow, cache location and size
  prune                            report (and, with --yes, reclaim) cache disk usage

  pdf     <source.md> <out.pdf>    optional escape hatch: a shareable PDF (not the default flow)
  check   <out.pdf>                rasterise that PDF to a contact sheet to verify the layout

The cache/durable split is enforced by code, not by convention:
  * fetch/frames/sheet REFUSE to write inside the output dir, ~/.claude, or any git work tree.
  * install REFUSES to write outside the output dir, and refuses to copy video files.
    Frame count is uncapped by default - pass --max-frames N to cap it deliberately.
So an mp4 cannot land in your notes, but frames are your call: prefer
installing a frame you may not use over re-downloading the video to recover it later.

Usage:
  python3 ~/.claude/skills/youtube-wisdom/scripts/video_kit.py ...

Configuration (environment variables, all optional):
  YOUTUBE_WISDOM_DIR    output dir for analyses (default: <skill dir>/wisdom/, gitignored)
  YOUTUBE_WISDOM_LANG   preferred subtitle language, e.g. es (default: the video's own)
  YOUTUBE_WISDOM_CACHE  cache for videos and frame dumps (default: ~/.cache/youtube-wisdom)
  YOUTUBE_WISDOM_VAULT  Obsidian vault root; when set, install prints a frame-mirror command

Requires: yt-dlp, ffmpeg, Pillow. PDF extras only: Google Chrome, poppler (pdftoppm).
"""
import argparse
import base64
import glob
import hashlib
import html as html_mod
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata
import urllib.parse
import urllib.request
from collections import deque
from typing import NoReturn

# ------------------------------------------------------------------ constants

# Fallback priority ONLY. The real selection is: the video's own spoken language first,
# then the single `*-orig` automatic track. See pick_sub_lang().
SUB_LANGS_FALLBACK = "en-orig,en,es-orig,es,pt-orig,pt"
_PREF_LANG = (os.environ.get("YOUTUBE_WISDOM_LANG") or "").strip()
if _PREF_LANG:
    SUB_LANGS_FALLBACK = f"{_PREF_LANG}-orig,{_PREF_LANG}," + SUB_LANGS_FALLBACK

# Keys YouTube advertises inside `subtitles` that are not transcripts. `live_chat` is chat-replay
# JSON; yt-dlp writes no .vtt for it, so picking it turns a clean abort into a confusing one.
NON_TRANSCRIPT_SUB_TAGS = {"live_chat"}

CACHE_ROOT = os.path.abspath(os.path.expanduser(
    os.environ.get("YOUTUBE_WISDOM_CACHE", "~/.cache/youtube-wisdom")))
SKILL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WISDOM_ROOT = os.path.realpath(os.path.expanduser(
    os.environ.get("YOUTUBE_WISDOM_DIR") or os.path.join(SKILL_ROOT, "wisdom")))
CLAUDE_ROOT = os.path.abspath(os.path.expanduser("~/.claude"))
_VAULT = os.environ.get("YOUTUBE_WISDOM_VAULT")
VAULT_ROOT = os.path.abspath(os.path.expanduser(_VAULT)) if _VAULT else None

IMAGE_EXTS = (".jpg", ".jpeg", ".png")

CHROME_CANDIDATES = [
    os.environ.get("CHROME_BIN"),
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
]


# ------------------------------------------------------------------ utilities

def die(msg) -> NoReturn:
    """NoReturn is load-bearing: it tells a type checker that code after a die() guard is
    unreachable, so `x = f() or die(...)` narrowing works instead of flagging x as unbound."""
    sys.exit(f"video_kit: {msg}")


def warn(msg):
    print(f"video_kit: warning: {msg}", file=sys.stderr)


def first_existing(*candidates):
    for c in candidates:
        if c and os.path.exists(c):
            return c
    return None


def resolve_bin(name, env_var, absolute, hint):
    """Absolute path to an external binary. Env override wins, then the brew path, then PATH."""
    found = os.environ.get(env_var) or first_existing(absolute) or shutil.which(name)
    if not found:
        die(f"`{name}` not found - install it with: {hint}  (or set {env_var}=/path/to/{name})")
    return found


def chrome_print_pdf(chrome, file_url, out_pdf, log_path, timeout=180):
    """Render a local HTML file to PDF with headless Chrome.

    Chrome 151 on macOS writes the PDF and then never exits (both --headless=new and
    --headless=old, fresh profile or reused), so a plain subprocess.run() hangs until its
    timeout. Launch it in its own session, wait for the output file to appear and stop
    growing, then kill the whole process group.
    """
    if os.path.exists(out_pdf):
        os.remove(out_pdf)
    cmd = [chrome, "--headless=new", "--disable-gpu",
           "--no-first-run", "--no-default-browser-check",
           "--disable-component-update", "--disable-background-networking",
           "--no-pdf-header-footer", f"--print-to-pdf={out_pdf}", file_url]
    with open(log_path, "w") as log:
        proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT,
                                start_new_session=True)
        deadline = time.time() + timeout
        last_size, stable = -1, 0
        try:
            while time.time() < deadline:
                if proc.poll() is not None:
                    break
                if os.path.exists(out_pdf):
                    size = os.path.getsize(out_pdf)
                    stable = stable + 1 if size == last_size and size > 1024 else 0
                    last_size = size
                    if stable >= 3:                     # ~0.75s with no growth: done
                        break
                time.sleep(0.25)
        finally:
            if proc.poll() is None:
                try:
                    os.killpg(os.getpgid(proc.pid), 15)
                except OSError:
                    proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(os.getpgid(proc.pid), 9)
                    except OSError:
                        proc.kill()
    if not os.path.exists(out_pdf) or os.path.getsize(out_pdf) < 1024:
        with open(log_path, encoding="utf-8", errors="replace") as f:
            tail = f.read()[-1200:]
        die(f"headless Chrome produced no usable PDF at {out_pdf}.\n{tail}")


def find_chrome(required=True):
    """required=False returns None instead of dying, so `doctor` can REPORT a missing Chrome
    rather than aborting the whole check run on it."""
    for candidate in CHROME_CANDIDATES:
        if not candidate:
            continue
        if os.path.sep in candidate:
            if os.path.exists(candidate):
                return candidate
        else:
            found = shutil.which(candidate)
            if found:
                return found
    if not required:
        return None
    die("no Chrome/Chromium found - install it or export CHROME_BIN=/path/to/binary")


def run_checked(cmd, what, timeout=None):
    """Run a subprocess and fail with the captured stderr, not a bare traceback."""
    try:
        proc = subprocess.run(cmd, text=True, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        die(f"{what} timed out after {timeout}s")
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip()[-1200:]
        die(f"{what} failed (exit {proc.returncode}):\n{tail}")
    return proc


def hms(seconds):
    """MM:SS below one hour, H:MM:SS at or above, so 90 minutes renders as 1:30:00, not 90:00."""
    total = int(round(float(seconds)))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def compact_ts(seconds):
    """HHMMSS, for filenames."""
    total = int(round(float(seconds)))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}{m:02d}{s:02d}"


TS_RE = re.compile(r"^\d+(?::\d{1,2}){0,2}(?:\.\d+)?$")


def parse_ts(value):
    """Accept `95`, `1:35`, `01:35`, `1:02:03` and floats. Returns seconds."""
    text = str(value).strip()
    if not TS_RE.match(text):
        die(f"cannot parse timestamp {value!r} - use SS, M:SS or H:MM:SS")
    total = 0.0
    for part in text.split(":"):
        total = total * 60 + float(part)
    return total


def human_bytes(n):
    # The `or unit == "TB"` clamp is deliberate: it makes the last iteration always return, so
    # 1024**6 renders as "1048576.0 TB" rather than falling out of the loop. That also makes any
    # post-loop return unreachable — there was one here, and it was dead code, not a fallback.
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024.0


def dir_size(path):
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            fp = os.path.join(root, f)
            try:
                total += os.path.getsize(fp)
            except OSError:
                pass
    return total


def restrict_filename(name):
    """Reproduce yt-dlp's --restrict-filenames, so install can rebuild the corpus filename."""
    s = unicodedata.normalize("NFKD", name or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.encode("ascii", "ignore").decode("ascii")
    s = re.sub(r"[^A-Za-z0-9_.\-]", "_", s)
    s = re.sub(r"_+", "_", s).strip("_.")
    return s or "video"


# ------------------------------------------------------- workdir + hard guards

VIDEO_ID_RE = re.compile(
    r"(?:v=|youtu\.be/|/shorts/|/embed/|/live/)([A-Za-z0-9_-]{6,})")


def video_id(url):
    m = VIDEO_ID_RE.search(url or "")
    return m.group(1) if m else None


def resolve_workdir(url=None, explicit=None):
    if explicit:
        return os.path.abspath(os.path.expanduser(explicit))
    if url:
        vid = video_id(url) or ("u" + hashlib.sha1(url.encode()).hexdigest()[:12])
        return os.path.join(CACHE_ROOT, vid)
    die("need --workdir (or a url to derive it from)")


def _nearest_existing_dir(path):
    p = path
    while not os.path.isdir(p):
        parent = os.path.dirname(p)
        if parent == p:
            return p
        p = parent
    return p


def git_toplevel(path):
    git = shutil.which("git")
    if not git:
        return None
    base = _nearest_existing_dir(path)
    proc = subprocess.run([git, "-C", base, "rev-parse", "--show-toplevel"],
                          text=True, capture_output=True)
    return proc.stdout.strip() if proc.returncode == 0 else None


def _is_within(child, parent):
    child = os.path.realpath(child)
    parent = os.path.realpath(parent)
    return child == parent or child.startswith(parent + os.sep)


def assert_cache_workdir(wd):
    """Cache artifacts (mp4, scene dumps, sheets) may never touch a repo. Run before any mkdir."""
    real = os.path.realpath(wd)
    if _is_within(real, WISDOM_ROOT):
        die(f"refusing to use {real} as a work dir - {WISDOM_ROOT} is the durable output "
            f"dir. Cache artifacts live in {CACHE_ROOT}; only `install` may write into it.")
    if _is_within(real, CLAUDE_ROOT):
        die(f"refusing to use {real} as a work dir - ~/.claude holds agent configuration; "
            f"keep cache artifacts out of it.")
    top = git_toplevel(real)
    if top:
        die(f"refusing to write cache artifacts inside a git work tree: {top}\n"
            f"       cache root is currently {CACHE_ROOT}"
            f"{' (from YOUTUBE_WISDOM_CACHE)' if os.environ.get('YOUTUBE_WISDOM_CACHE') else ''}"
            f"; point it and any --workdir outside every repo.")
    return real


def assert_durable_dest(dest):
    """install is the only cache -> durable crossing, and it only crosses into the output dir."""
    real = os.path.realpath(os.path.abspath(os.path.expanduser(dest)))
    if not _is_within(real, WISDOM_ROOT):
        die(f"--dest must be inside {WISDOM_ROOT} (got {real})")
    if real == WISDOM_ROOT:
        die(f"--dest must be a per-video directory inside {WISDOM_ROOT}, not the root itself")
    base = os.path.basename(real.rstrip(os.sep))
    if not re.fullmatch(r"[A-Za-z0-9._-]+", base):
        die(f"--dest directory name {base!r} must be [A-Za-z0-9._-] only. It becomes the frame "
            f"filename prefix and is interpolated into the printed vault-mirror command.")
    return real


def _head_bytes(path, n):
    try:
        with open(path, "rb") as f:
            return f.read(n)
    except OSError:
        return b""


def assert_safe_output(path, ext, magic, allow_durable, what):
    """Guard for the pdf/check escape hatch, whose outputs are DELETED before being rewritten.

    Three ways an unvalidated --out destroys data: a typo'd `.md` target is overwritten by a PDF;
    a multi-MB PDF or verification sheet lands in the output dir, which may be a git repo that
    ignores neither *.pdf nor *.png; a pre-existing file of another type is clobbered.
    """
    real = os.path.abspath(os.path.expanduser(path))
    if not real.lower().endswith(ext):
        die(f"{what} must end in {ext} (got {real}). It is overwritten in place, so a wrong "
            f"extension silently destroys whatever is there.")
    if os.path.exists(real):
        if not os.path.isfile(real):
            die(f"{what} {real} exists and is not a regular file")
        head = _head_bytes(real, len(magic))
        if head != magic:
            die(f"refusing to overwrite {real}: it exists but is not a {ext} file "
                f"(starts with {head!r}). Pick another output path.")
    if _is_within(real, WISDOM_ROOT) and not allow_durable:
        die(f"refusing to write {what} into {WISDOM_ROOT} - it is the durable output dir "
            f"and binaries there tend to get committed by accident. Write it "
            f"elsewhere (the markdown is the durable deliverable), or pass --allow-durable if "
            f"you really mean to commit a binary.")
    return real


def load_meta(wd):
    path = os.path.join(wd, "meta.json")
    if not os.path.exists(path):
        die(f"{path} not found - run `fetch` first")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def find_video(wd):
    """The merged file is not always .mp4 (a VP9/AV1 video-only stream lands as .webm)."""
    cands = [p for p in sorted(glob.glob(os.path.join(wd, "video.*")))
             if not p.endswith((".part", ".ytdl", ".json"))]
    return cands[0] if cands else None


# --------------------------------------------------------------------- fetch

def already_extracted(vid):
    """Best-effort duplicate warning. Recursive, so analyses filed in subfolders are found too."""
    if not vid or not os.path.isdir(WISDOM_ROOT):
        return []
    hits = []
    for root, dirs, files in os.walk(WISDOM_ROOT):
        dirs[:] = [d for d in dirs if d not in (".git", ".obsidian")]
        for name in files:
            if not name.endswith(".md"):
                continue
            path = os.path.join(root, name)
            try:
                with open(path, encoding="utf-8", errors="ignore") as f:
                    if vid in f.read():
                        hits.append(path)
            except OSError:
                pass
    return hits


def _lang_base(tag):
    return (tag or "").split("-")[0].lower()


def pick_sub_lang(info, fallback_priority):
    """Choose exactly ONE subtitle language, and make sure it is the ORIGINAL one.

    Asking yt-dlp for several languages makes it fetch machine translations and earns an
    HTTP 429 fast, so exactly one language is requested - chosen here, not by yt-dlp.

    YouTube's `automatic_captions` enumerates ~157 *translation targets*, not available
    originals, so a static priority list starting with `en` picks a machine-translated
    English ASR track for every non-English video. Order that actually works:

      1. an author-provided (manual) track in the video's own spoken language
      2. the single `*-orig` automatic key - always the original ASR, never a translation
      3. any manual track from the fallback list
      4. any automatic track from the fallback list
      5. ANY remaining manual track - a real author track always beats aborting, and unlike
         `automatic_captions` the manual set is never padded with translation targets. There is
         deliberately no equivalent last resort over `auto`, which would grab a translation.

    Returns (lang_tag, is_auto, reason) or (None, None, None).
    """
    manual = list((info.get("subtitles") or {}).keys())
    auto = list((info.get("automatic_captions") or {}).keys())
    spoken = _lang_base(info.get("language"))
    if not spoken:
        # yt-dlp often leaves `language` null, but the `*-orig` auto key still names the spoken
        # language. Without this, step 1 is skipped and step 2 returns the auto track even when
        # an author track in that same language exists.
        orig_bases = {_lang_base(t) for t in auto if t.endswith("-orig")}
        if len(orig_bases) == 1:
            spoken = orig_bases.pop()

    if spoken:
        for tag in manual:
            if tag.lower() == (info.get("language") or "").lower():
                return tag, False, "author subtitles in the spoken language"
        for tag in sorted(manual):
            if _lang_base(tag) == spoken:
                return tag, False, "author subtitles in the spoken language"

    origs = sorted(t for t in auto if t.endswith("-orig"))
    if origs:
        preferred = [t for t in origs if _lang_base(t) == spoken] or origs
        return preferred[0], True, "original auto-captions (not a translation)"

    for tag in fallback_priority:
        if tag in manual and tag not in NON_TRANSCRIPT_SUB_TAGS:
            return tag, False, "author subtitles (fallback list)"
    for tag in fallback_priority:
        if tag in auto:
            return tag, True, "auto-captions (fallback list)"

    # Last resort: a real author track exists, it is just not in the fallback list (`de`, `ja`,
    # or a regional variant like `pt-BR` / `es-419` / `en-GB` on a video whose `language` field
    # yt-dlp left null - which is the common shape, not an edge case).
    # `live_chat` is chat-replay JSON that YouTube advertises inside `subtitles`; it is not a
    # transcript and yt-dlp writes no .vtt for it, so it must never be picked.
    cands = sorted(t for t in manual if t not in NON_TRANSCRIPT_SUB_TAGS)
    if cands:
        tag = cands[0]
        return tag, False, f"author subtitles ({tag}; only track offered)"
    return None, None, None


def _trim_rolling_overlap(prev, cur, min_words=4):
    """YouTube rolling captions re-emit the tail of the previous cue as leading context."""
    pw, cw = prev.split(), cur.split()
    upper = min(len(pw), len(cw))
    for k in range(upper, min_words - 1, -1):
        if pw[-k:] == cw[:k]:
            return " ".join(cw[k:])
    return cur


CUE_RE = re.compile(r"^(?:(\d+):)?(\d{2}):(\d{2})\.(\d{3})\s+-->")


def vtt_to_transcript(vtt_path, out_path, block_seconds, window=6):
    """Deduplicated, timestamped blocks. Returns (n_lines, n_blocks, tmp_path).

    Writes to `out_path + '.tmp'`, NOT to out_path: the caller promotes it with os.replace only
    once it knows the parse produced blocks. Truncating out_path here would destroy a good
    existing transcript on a `--force` re-run whose new .vtt turns out to be empty.
    """
    cues, current = [], None
    with open(vtt_path, encoding="utf-8", errors="replace") as f:
        raw = f.read()
    for line in raw.splitlines():
        stripped = line.strip()
        m = CUE_RE.match(stripped)
        if m:
            h = int(m.group(1) or 0)
            current = h * 3600 + int(m.group(2)) * 60 + int(m.group(3)) + int(m.group(4)) / 1000
            continue
        # A blank line terminates the cue payload. Without this reset, `current` latches forever
        # and every following non-empty line is swallowed as caption text - which captures the
        # optional cue IDENTIFIER line that precedes a timing line in spec-valid WebVTT (and any
        # mid-file NOTE block) as a bare token in the transcript.
        # Only a truly empty line ends a cue. YouTube auto-caption cues open with a line holding a
        # single space; treating that as a terminator drops the new words that follow it.
        if not line:
            current = None
            continue
        if current is None:
            continue
        text = re.sub(r"<[^>]+>", "", line).strip()
        if text:
            cues.append((current, text))

    # Adjacent-window dedup: a global set would permanently delete any line repeated
    # anywhere in the video ("right", "okay", a repeated chorus) along with its timestamp.
    recent = deque(maxlen=window)
    clean = []
    for t, text in cues:
        if text in recent:
            continue
        recent.append(text)
        if clean:
            trimmed = _trim_rolling_overlap(clean[-1][1], text)
            if not trimmed.strip():
                continue
            text = trimmed
        clean.append((t, text))

    # Block label = the first cue's time. The block is closed BEFORE the cue that crosses the
    # boundary, so every cue in a block satisfies `t - start < block_seconds` and a quote can
    # always be cited to its own block. Appending the crossing cue first (the obvious loop) files
    # it under the earlier label - harmless with dense captions, but a caption gap (silence, a
    # live demo, applause) is absorbed whole and the label is then wrong by the entire gap.
    blocks, buf, start = [], [], None
    for t, text in clean:
        if start is not None and t - start >= block_seconds:
            blocks.append((start, " ".join(buf)))
            buf, start = [], None
        if start is None:
            start = t
        buf.append(text)
    if buf:
        blocks.append((start, " ".join(buf)))

    tmp_path = out_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        for t, text in blocks:
            f.write(f"[{hms(t)}] {text}\n\n")
    return len(clean), len(blocks), tmp_path


def estimate_video_mb(info, height):
    """Best guess at the video-only download size, from the format list yt-dlp already gave us."""
    best, best_h = None, -1
    duration = info.get("duration") or 0
    for fmt in info.get("formats") or []:
        if fmt.get("vcodec") in (None, "none"):
            continue
        h = fmt.get("height") or 0
        if h > height:
            continue
        if h > best_h:
            best, best_h = fmt, h
    if not best:
        return None
    size = best.get("filesize") or best.get("filesize_approx")
    if size:
        return size / 1e6
    tbr = best.get("vbr") or best.get("tbr")
    if tbr and duration:
        return tbr * 1000 * duration / 8 / 1e6
    return None


def cmd_fetch(args):
    ytdlp = resolve_bin("yt-dlp", "YT_DLP_BIN", "/opt/homebrew/bin/yt-dlp", "brew install yt-dlp")
    wd = assert_cache_workdir(resolve_workdir(args.url, args.workdir))
    os.makedirs(wd, exist_ok=True)

    transcript_path = os.path.join(wd, "transcript.md")
    have_transcript = os.path.exists(transcript_path) and os.path.getsize(transcript_path) > 0
    have_video = find_video(wd) is not None

    need_transcript = args.force or not have_transcript
    need_video = args.video and (args.force or not have_video)

    if not need_transcript and not need_video:
        print(f"· nothing to do - transcript.md present"
              f"{' and video present' if have_video else ''} (use --force to redo)")
        print(f"· transcript: {transcript_path}")
        print(f"WORKDIR={wd}")
        return

    info = json.loads(run_checked(
        [ytdlp, "--no-playlist", "--dump-single-json", "--skip-download", args.url],
        "yt-dlp metadata").stdout)

    title = info.get("title") or "video"
    meta = {
        "id": info.get("id"),
        "title": title,
        "title_restricted": restrict_filename(title),
        "channel": info.get("channel") or info.get("uploader"),
        "duration_s": info.get("duration"),
        "duration": hms(info["duration"]) if info.get("duration") else None,
        "published": (lambda d: f"{d[:4]}-{d[4:6]}-{d[6:]}" if d else None)(info.get("upload_date")),
        "url": info.get("webpage_url") or args.url,
        "language": info.get("language"),
    }
    existing = {}
    if os.path.exists(os.path.join(wd, "meta.json")):
        try:
            existing = load_meta(wd)
        except SystemExit:
            existing = {}
    existing.update({k: v for k, v in meta.items() if v is not None})
    meta = existing
    with open(os.path.join(wd, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    print(f"· {meta.get('title')} - {meta.get('channel')} · {meta.get('duration')} "
          f"· {meta.get('published')}")

    dupes = already_extracted(meta.get("id"))
    if dupes:
        print("\n!! ALREADY EXTRACTED - this video id appears in:")
        for p in dupes[:5]:
            print(f"   {p}")
        print("   (best-effort: it only matches analyses that contain the video id, so a "
              "clean result is not proof)\n")

    # ---- subtitles
    if need_transcript:
        for stale in glob.glob(os.path.join(wd, "subs.*.vtt")):
            os.remove(stale)
        fallback = [x.strip() for x in args.sub_langs.split(",") if x.strip()]
        lang, is_auto, reason = pick_sub_lang(info, fallback)
        if not lang:
            saw_manual = sorted(t for t in (info.get("subtitles") or {})
                                if t not in NON_TRANSCRIPT_SUB_TAGS)
            saw_origs = sorted(t for t in (info.get("automatic_captions") or {}) if t.endswith("-orig"))
            die(f"no usable subtitle track. manual={saw_manual or 'none'} "
                f"auto-orig={saw_origs or 'none'}. If a track is listed above, force it with "
                f"`--sub-langs <tag>`. Otherwise this skill does not automate audio "
                f"transcription: stop and tell the user; do not download audio or video to "
                f"transcribe by hand.")
        run_checked(
            [ytdlp, "--no-playlist", "--skip-download",
             "--write-auto-subs" if is_auto else "--write-subs",
             "--sub-langs", lang, "--sub-format", "vtt",
             "-o", os.path.join(wd, "subs.%(ext)s"), args.url],
            f"yt-dlp subtitles ({lang})")

        picked = os.path.join(wd, f"subs.{lang}.vtt")
        if not os.path.exists(picked):
            found = sorted(glob.glob(os.path.join(wd, "subs.*.vtt")))
            if not found:
                die(f"yt-dlp reported success but wrote no .vtt for `{lang}` - "
                    f"the track is unavailable. Audio transcription is not automated here.")
            warn(f"requested `{lang}` but yt-dlp wrote {os.path.basename(found[0])}")
            picked = found[0]
        actual = os.path.basename(picked)[len("subs."):-len(".vtt")]
        print(f"· subtitles: {actual} ({'auto' if is_auto else 'author'}) - {reason}")

        n_lines, n_blocks, tmp_transcript = vtt_to_transcript(picked, transcript_path, args.block)
        if n_blocks == 0:
            os.remove(tmp_transcript)
            die(f"{actual} subtitles parsed to 0 blocks - the .vtt at {picked} is empty or in an "
                f"unexpected format. Refusing to leave an empty transcript "
                f"(any existing one is untouched).")
        os.replace(tmp_transcript, transcript_path)
        meta["sub_lang"] = actual
        meta["sub_auto"] = bool(is_auto)
        with open(os.path.join(wd, "meta.json"), "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
        print(f"· transcript.md: {n_lines} lines -> {n_blocks} blocks, each starting within "
              f"{args.block}s of its label ({os.path.getsize(transcript_path) / 1024:.0f} KB)")
    else:
        print("· transcript.md: already present, skipped")

    # ---- video (opt-in, per-artifact skip)
    if args.video and not need_video:
        existing = find_video(wd)
        if not existing:
            die("the cached video disappeared between the skip check and this print - "
                "re-run with --force to re-download it")
        print(f"· {os.path.basename(existing)}: already present, skipped")
    elif need_video:
        est = estimate_video_mb(info, args.height)
        if est is not None:
            print(f"· estimated video-only download: ~{est:.0f} MB at <={args.height}p")
            if est > args.max_mb:
                die(f"estimated {est:.0f} MB exceeds --max-mb {args.max_mb}. Long talks get big; "
                    f"raise the ceiling explicitly (--max-mb {int(est) + 100}) or lower --height.")
        # Stage into a dot-dir and swap only once yt-dlp has produced a file. Deleting the cached
        # video first meant a `--force` re-run that hit a 429, a dropped connection or a format
        # that is no longer offered left the work dir with nothing and ~200 MB to fetch again.
        # vtt_to_transcript refuses to truncate a good transcript for exactly this reason.
        # A dot-dir: prune skips those (it filters `d.startswith(".")`), and it stays outside the
        # `video.*` namespace that find_video and prune both glob.
        stage = os.path.join(wd, ".video-staging")
        shutil.rmtree(stage, ignore_errors=True)     # leftovers from a run that died mid-download
        os.makedirs(stage, exist_ok=True)
        # Video-only stream on purpose: the frames branch never needs audio.
        fmt = (f"bv*[height<={args.height}][ext=mp4]/bv*[height<={args.height}]"
               f"/b[height<={args.height}]")
        run_checked([ytdlp, "--no-playlist", "-f", fmt, "--merge-output-format", "mp4",
                     "-o", os.path.join(stage, "video.%(ext)s"), args.url], "yt-dlp video")
        staged = find_video(stage)
        if not staged:
            shutil.rmtree(stage, ignore_errors=True)
            die("yt-dlp exited 0 but produced no video file in the work dir")
        for stale in glob.glob(os.path.join(wd, "video.*")):
            os.remove(stale)
        vid = os.path.join(wd, os.path.basename(staged))
        os.replace(staged, vid)                      # same filesystem: atomic
        shutil.rmtree(stage, ignore_errors=True)
        meta["video_file"] = os.path.basename(vid)
        with open(os.path.join(wd, "meta.json"), "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
        print(f"· {os.path.basename(vid)}: {os.path.getsize(vid) / 1e6:.1f} MB")

    print(f"· work dir total: {human_bytes(dir_size(wd))}")
    print(f"· transcript: {transcript_path}")
    print(f"WORKDIR={wd}")


# --------------------------------------------------------------------- frames

def cmd_frames(args):
    ffmpeg = resolve_bin("ffmpeg", "FFMPEG_BIN", "/opt/homebrew/bin/ffmpeg", "brew install ffmpeg")
    wd = assert_cache_workdir(resolve_workdir(None, args.workdir))
    video = find_video(wd)
    if not video:
        die(f"no video file in {wd} - run `fetch <url> --video` first")

    scenes = os.path.join(wd, "frames")
    picks = os.path.join(wd, "pick")
    os.makedirs(scenes, exist_ok=True)

    if args.clear_picks and os.path.isdir(picks):
        for old in glob.glob(os.path.join(picks, "t*.jpg")):
            os.remove(old)
        print("· cleared previous --at picks")

    if not args.no_scenes:
        for old in glob.glob(os.path.join(scenes, "scene_*.jpg")):
            os.remove(old)
        # Filter order is load-bearing: select first, showinfo AFTER it so only KEPT frames are
        # logged, scale last. That is what makes ordinal pts_time <-> scene_NNN pairing possible.
        vf = f"select='gt(scene,{args.threshold})',showinfo,scale={args.width}:-2"
        cmd = [ffmpeg, "-hide_banner", "-i", video, "-vf", vf,
               "-fps_mode", "vfr", "-q:v", str(args.quality),
               os.path.join(scenes, "scene_%03d.jpg")]
        proc = subprocess.run(cmd, text=True, capture_output=True)
        if proc.returncode != 0 and "fps_mode" in proc.stderr:
            warn("this ffmpeg predates -fps_mode (5.1); retrying with -vsync vfr")
            cmd[cmd.index("-fps_mode")] = "-vsync"
            proc = subprocess.run(cmd, text=True, capture_output=True)
        if proc.returncode != 0:
            die(f"ffmpeg scene pass failed (exit {proc.returncode}):\n{proc.stderr[-1200:]}")

        times = []
        for tok in proc.stderr.split():
            if tok.startswith("pts_time:"):
                val = tok.split(":", 1)[1]
                try:
                    times.append(float(val))
                except ValueError:
                    warn(f"unparsable pts_time token {tok!r}, ignored")
        written = sorted(glob.glob(os.path.join(scenes, "scene_*.jpg")))
        if len(times) != len(written):
            die(f"ffmpeg wrote {len(written)} frames but showinfo logged {len(times)} timestamps "
                f"- the ordinal pairing would be silently wrong. Re-run; if it persists, lower "
                f"--threshold or report the ffmpeg version.")
        with open(os.path.join(scenes, "map.txt"), "w", encoding="utf-8") as f:
            for i, t in enumerate(times, 1):
                f.write(f"scene_{i:03d}.jpg {hms(t)}\n")
        print(f"· {len(times)} scene-change frames (threshold {args.threshold}, "
              f"{args.width}px) -> {scenes}/")

    if args.at:
        specs = [x.strip() for x in args.at.split(",") if x.strip()]
        seen = {}
        for raw in specs:
            secs = int(round(parse_ts(raw)))
            if secs in seen:
                die(f"--at collision: {seen[secs]!r} and {raw!r} both round to {secs}s and would "
                    f"write the same pick/t{secs}.jpg. Drop one or move it a second.")
            seen[secs] = raw
        os.makedirs(picks, exist_ok=True)
        for secs, raw in sorted(seen.items()):
            out = os.path.join(picks, f"t{secs}.jpg")
            # -ss before -i: fast input seek, accurate on modern ffmpeg.
            run_checked([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-ss", str(secs),
                         "-i", video, "-frames:v", "1",
                         "-vf", f"scale={args.pick_width}:-2", "-q:v", str(args.quality), out],
                        f"ffmpeg frame at {raw}")
            if not os.path.exists(out):
                die(f"ffmpeg exited 0 but wrote no frame for {raw} - is it past the end?")
        print(f"· {len(seen)} targeted frames ({args.pick_width}px) -> {picks}/")
    elif os.path.isdir(picks):
        kept = glob.glob(os.path.join(picks, "t*.jpg"))
        if kept:
            print(f"· {len(kept)} previously picked frame(s) retained in {picks}/ "
                  f"(--clear-picks to drop them)")

    print(f"· next: sheet --workdir {wd}")


# ---------------------------------------------------------------------- sheet

def collect_frames(wd):
    """[(seconds, workdir-relative path, label)] sorted by timestamp."""
    items = []
    map_path = os.path.join(wd, "frames", "map.txt")
    if os.path.exists(map_path):
        with open(map_path, encoding="utf-8") as f:
            for lineno, line in enumerate(f, 1):
                parts = line.split()
                if len(parts) != 2:
                    if line.strip():
                        warn(f"map.txt:{lineno}: cannot parse {line.strip()!r}, skipped")
                    continue
                name, ts = parts
                if not os.path.exists(os.path.join(wd, "frames", name)):
                    warn(f"map.txt lists {name} but the file is gone, skipped")
                    continue
                items.append((parse_ts(ts), f"frames/{name}", ts))
    for path in sorted(glob.glob(os.path.join(wd, "pick", "t*.jpg"))):
        base = os.path.basename(path)
        m = re.match(r"^t(\d+)\.jpg$", base)
        if not m:
            warn(f"pick/{base}: unexpected name, skipped")
            continue
        secs = float(m.group(1))
        items.append((secs, f"pick/{base}", hms(secs)))
    items.sort()
    return items


def cmd_sheet(args):
    try:
        from PIL import Image, ImageDraw, ImageOps
    except ModuleNotFoundError:
        die("Pillow is missing - install it with: python3 -m pip install Pillow")

    wd = assert_cache_workdir(resolve_workdir(None, args.workdir))
    items = collect_frames(wd)
    if not items:
        die(f"no frames in {wd} - run `frames --workdir {wd}` first")

    for stale in glob.glob(os.path.join(wd, "sheet*.jpg")):
        os.remove(stale)

    per_sheet, tw = args.per_sheet, args.thumb
    label_h = 18
    pages = [items[i:i + per_sheet] for i in range(0, len(items), per_sheet)]
    outs = []
    for pi, page in enumerate(pages, 1):
        # Shrink to the content: a 4-frame sheet on a 6-col canvas is 2 columns of dead white.
        cols = min(max(1, args.cols), len(page))
        # Letterbox instead of forcing 16:9, so a non-16:9 source is not squashed.
        with Image.open(os.path.join(wd, page[0][1])) as probe:
            th = max(1, int(round(tw * probe.height / probe.width)))
        rows = (len(page) + cols - 1) // cols
        sheet = Image.new("RGB", (cols * tw, rows * (th + label_h)), "white")
        draw = ImageDraw.Draw(sheet)
        for i, (_secs, rel, ts) in enumerate(page):
            with Image.open(os.path.join(wd, rel)) as img:
                thumb = ImageOps.contain(img.convert("RGB"), (tw, th))
                x = (i % cols) * tw
                y = (i // cols) * (th + label_h)
                sheet.paste(thumb, (x + (tw - thumb.width) // 2, y + (th - thumb.height) // 2))
            # The label is the whole point: it prints exactly the path + timestamp you paste next.
            label = f"{rel}  {ts}"
            while label and draw.textlength(label) > tw - 8:
                label = label[:-1]
            draw.text((x + 4, y + th + 3), label, fill="black")
        name = "sheet.jpg" if len(pages) == 1 else f"sheet-{pi:02d}.jpg"
        out = os.path.join(wd, name)
        sheet.save(out, quality=82)
        outs.append(out)
    print(f"· {len(items)} frames on {len(outs)} sheet(s):")
    for o in outs:
        print(f"  {o}")
    print("· read the SHEET, not the individual frames - every cell is labelled "
          "`frames/scene_007.jpg  02:13`")
    if len(outs) > 3:
        print(f"· {len(outs)} sheets is a lot: re-run `frames --threshold 0.45` and `sheet` "
              f"instead of reading them all")


# -------------------------------------------------------------------- install

def cmd_install(args):
    ffmpeg = resolve_bin("ffmpeg", "FFMPEG_BIN", "/opt/homebrew/bin/ffmpeg", "brew install ffmpeg")
    wd = os.path.abspath(os.path.expanduser(args.workdir))
    if not os.path.isdir(wd):
        die(f"work dir {wd} does not exist")
    dest = assert_durable_dest(args.dest)
    slug = os.path.basename(dest.rstrip(os.sep))

    meta = load_meta(wd)
    transcript = os.path.join(wd, "transcript.md")
    if not os.path.exists(transcript):
        die(f"{transcript} not found - run `fetch` first")

    frames_specs = args.frame or []
    if args.max_frames > 0 and len(frames_specs) > args.max_frames:
        die(f"{len(frames_specs)} --frame arguments exceeds --max-frames {args.max_frames}. "
            f"Drop some, or raise --max-frames deliberately.")

    items = collect_frames(wd)
    by_rel = {rel: secs for secs, rel, _ts in items}
    by_secs = {}
    for secs, rel, _ts in items:
        key = int(round(secs))
        # Scene frames fire ON the cut; picks are taken after it on purpose, so a pick wins.
        if key not in by_secs or rel.startswith("pick/"):
            by_secs[key] = rel

    resolved = []  # (seconds, absolute source path)
    for spec in frames_specs:
        spec = spec.strip()
        if TS_RE.match(spec):
            secs = int(round(parse_ts(spec)))
            rel = by_secs.get(secs)
            if not rel:
                near = ", ".join(f"{hms(s)}" for s in sorted(by_secs)[:12])
                die(f"--frame {spec}: no extracted frame at {hms(secs)}. Available (first 12): "
                    f"{near or 'none'}. Run `frames --at {spec}` first, or pass a path.")
            src = os.path.join(wd, rel)
        else:
            src = os.path.abspath(os.path.join(wd, spec))
            if not _is_within(src, wd):
                die(f"--frame {spec}: resolves outside the work dir ({src})")
            base = os.path.basename(src)
            if base.startswith("video."):
                die(f"--frame {spec}: refusing to install a video file into {WISDOM_ROOT}")
            if not base.lower().endswith(IMAGE_EXTS):
                die(f"--frame {spec}: only {', '.join(IMAGE_EXTS)} may be installed")
            if not os.path.exists(src):
                die(f"--frame {spec}: {src} does not exist")
            rel = os.path.relpath(src, wd)
            if rel in by_rel:
                secs = int(round(by_rel[rel]))
            else:
                m = re.match(r"^t(\d+)\.jpg$", base)
                if not m:
                    die(f"--frame {spec}: cannot determine its timestamp (not in frames/map.txt "
                        f"and not a pick/t<seconds>.jpg). Re-run `frames` to rebuild map.txt.")
                secs = int(m.group(1))
        resolved.append((secs, src))

    resolved.sort()
    seen_ts = {}
    for secs, src in resolved:
        if secs in seen_ts:
            die(f"two --frame arguments resolve to {hms(secs)} ({seen_ts[secs]} and {src}); "
                f"they would write the same file. Drop one.")
        seen_ts[secs] = src

    os.makedirs(dest, exist_ok=True)

    # Re-sanitise rather than trust: meta.json is just a file in the cache dir, and a corrupted or
    # hand-edited `title_restricted` containing `../..` would os.path.join its way straight back
    # out of the --dest that assert_durable_dest just validated. restrict_filename is idempotent
    # on its own output, so this is free for well-formed metadata.
    title_r = restrict_filename(meta.get("title_restricted") or meta.get("title") or "video")
    transcript_dest = os.path.join(dest, f"{title_r} - transcript.txt")
    if not _is_within(transcript_dest, dest):
        die(f"internal guard: {transcript_dest} escapes {dest}")
    shutil.copyfile(transcript, transcript_dest)
    print(f"· transcript -> {transcript_dest}")

    if args.keep_subs:
        for vtt in sorted(glob.glob(os.path.join(wd, "subs.*.vtt"))):
            tag = os.path.basename(vtt)[len("subs."):]
            out = os.path.join(dest, f"{title_r}.{tag}")
            if not _is_within(out, dest):
                die(f"internal guard: {out} escapes {dest}")
            shutil.copyfile(vtt, out)
            print(f"· subtitles -> {out}")

    installed = []
    if resolved:
        fdir = os.path.join(dest, "frames")
        os.makedirs(fdir, exist_ok=True)
        for secs, src in resolved:
            # Name is derived from the timestamp only, never a position in the argument list,
            # so re-running install with a different --frame set cannot renumber anything.
            name = f"{slug}-{compact_ts(secs)}.jpg"
            out = os.path.join(fdir, name)
            run_checked([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", src,
                         "-vf", f"scale={args.frame_width}:-2", "-q:v", str(args.frame_quality),
                         out], f"ffmpeg re-encode of {os.path.basename(src)}")
            if not os.path.exists(out):
                die(f"ffmpeg exited 0 but wrote nothing for {src}")
            installed.append((secs, name, os.path.getsize(out)))
        total = sum(s for _t, _n, s in installed)
        print(f"· {len(installed)} frame(s) -> {fdir}/ ({human_bytes(total)}, "
              f"{args.frame_width}px)")
        current = {n for _t, n, _s in installed}
        stale = sorted(f for f in os.listdir(fdir)
                       if f.lower().endswith(IMAGE_EXTS) and f not in current)
        if stale:
            warn(f"{len(stale)} frame(s) in {fdir} come from an earlier install and were not "
                 f"re-selected (left in place; delete by hand if unwanted): {', '.join(stale)}")

    print()
    print(f"DEST={dest}")
    if installed:
        print("\n--- paste into analysis.md (replace the quoted caption) ---")
        for secs, name, _size in installed:
            print(f'![{hms(secs)}](frames/{name} "what this frame shows")')
    if installed and VAULT_ROOT:
        print("\n--- paste into the Obsidian note (attachments copied separately) ---")
        for secs, name, _size in installed:
            print(f"![[{name}]]")
            print(f"*{hms(secs)} - what this frame shows*")
        # Printed for an agent to run verbatim, so: the real vault path, never an undefined
        # `$VAULT` (shell variables do not survive between tool calls either), and every path
        # shlex-quoted so a character in a path can never break out of the quoting.
        vault_dir = os.path.join(VAULT_ROOT, "attachments", slug)
        print("\n--- to mirror the frames into the vault ---")
        # Name each installed file: a `frames/*.jpg` glob would also copy stale frames.
        srcs = " ".join(shlex.quote(os.path.join(dest, "frames", n)) for _t, n, _s in installed)
        print(f"mkdir -p {shlex.quote(vault_dir)} && cp {srcs} {shlex.quote(vault_dir + os.sep)}")

    size = dir_size(wd)
    print(f"\n· cache work dir is now {human_bytes(size)} at {wd}")
    print(f"· reclaim it when you are done (your call, nothing auto-deletes):")
    print(f"    {shlex.quote(sys.executable)} {shlex.quote(os.path.abspath(__file__))} "
          f"prune --workdir {shlex.quote(wd)} --yes")


# ---------------------------------------------------------------------- prune

def cmd_prune(args):
    # CACHE_ROOT is env-overridable (YOUTUBE_WISDOM_CACHE), so it gets the same guard every other
    # command applies to its work dir. Without this, `prune --all --yes` against a cache root
    # pointed at the output dir (or any repo) would rmtree the repo, .git first.
    root = os.path.realpath(assert_cache_workdir(CACHE_ROOT))
    if args.workdir:
        targets = [assert_cache_workdir(resolve_workdir(None, args.workdir))]
        for t in targets:
            if not _is_within(t, root):
                die(f"prune only operates inside {root} (got {t})")
            # One guard, two crash sites. A missing --workdir used to traceback out of
            # os.path.getmtime() in the --older-than age check AND out of os.path.getsize()
            # in the --all size sum; guarding only the first leaves --all broken. This is
            # reachable by an ordinary user: cmd_install prints `prune --workdir <wd> --yes`
            # as a next step, so re-running a stale printed command hits it. die(), not a
            # silent skip, matching cmd_install's own precedent.
            if not os.path.isdir(t):
                die(f"work dir {t} does not exist")
    else:
        if not os.path.isdir(root):
            print(f"· cache {root} does not exist - nothing to prune")
            return
        # Hidden entries are never work dirs; `fetch` names them after the video id.
        targets = [os.path.join(root, d) for d in sorted(os.listdir(root))
                   if not d.startswith(".") and os.path.isdir(os.path.join(root, d))]

    now = time.time()
    plan = []
    for wd in targets:
        if args.older_than is not None:
            age_days = (now - os.path.getmtime(wd)) / 86400.0
            if age_days < args.older_than:
                continue
        if args.all:
            victims = [wd]
        else:
            victims = sorted(glob.glob(os.path.join(wd, "video.*"))
                             + glob.glob(os.path.join(wd, "frames", "scene_*.jpg"))
                             + glob.glob(os.path.join(wd, "sheet*.jpg")))
        if not victims:
            continue
        size = sum(dir_size(v) if os.path.isdir(v) else os.path.getsize(v) for v in victims)
        plan.append((wd, victims, size))

    if not plan:
        print(f"· nothing to reclaim in {root} "
              f"({human_bytes(dir_size(root)) if os.path.isdir(root) else '0 B'} total)")
        return

    total = sum(s for _wd, _v, s in plan)
    for wd, victims, size in plan:
        print(f"· {wd}: {len(victims)} item(s), {human_bytes(size)}")
    mode = "the ENTIRE work dir" if args.all else "video + scene dump + contact sheets"
    print(f"\n· {human_bytes(total)} reclaimable across {len(plan)} work dir(s) [{mode}]")
    if not args.yes:
        print("· dry run - nothing deleted. Re-run with --yes to actually reclaim.")
        return

    freed = 0
    for wd, victims, size in plan:
        for v in victims:
            if not _is_within(v, root):
                die(f"internal guard: {v} is outside {root}")
            if os.path.isdir(v):
                shutil.rmtree(v)
            else:
                os.remove(v)
        freed += size
        print(f"· pruned {wd}")
    print(f"· freed {human_bytes(freed)}")


# --------------------------------------------------------------------- doctor

def cmd_doctor(_args):
    ok = True
    print(f"· python:  {sys.executable}")

    for name, env_var, absolute, hint, required in (
        ("yt-dlp", "YT_DLP_BIN", "/opt/homebrew/bin/yt-dlp", "brew install yt-dlp", True),
        ("ffmpeg", "FFMPEG_BIN", "/opt/homebrew/bin/ffmpeg", "brew install ffmpeg", True),
        ("pdftoppm", "PDFTOPPM_BIN", "/opt/homebrew/bin/pdftoppm", "brew install poppler", False),
    ):
        found = os.environ.get(env_var) or first_existing(absolute) or shutil.which(name)
        if not found:
            print(f"· {name:9s} MISSING{'' if required else ' (PDF only)'} - {hint}")
            ok = ok and not required
            continue
        ver = "?"
        for flag in ("-v" if name == "pdftoppm" else "--version", "-version"):
            proc = subprocess.run([found, flag], text=True, capture_output=True)
            text = (proc.stdout or proc.stderr).strip()
            if text and "Error" not in text.splitlines()[0]:
                ver = text.splitlines()[0]
                break
        print(f"· {name:9s} {found}  ({ver.strip()[:60]})")

    try:
        import PIL
        print(f"· Pillow    {PIL.__version__}")
    except ModuleNotFoundError:
        print("· Pillow    MISSING - python3 -m pip install Pillow")
        ok = False

    # Ask the same resolver `pdf` uses, rather than a parallel expression. The two DISAGREED:
    # this one tried every path-shaped candidate before any bare command name, so a CHROME_BIN
    # holding a bare name lost to /Applications/Google Chrome here while find_chrome (which
    # honours CHROME_CANDIDATES order, CHROME_BIN first) picked the one on PATH. Doctor reported
    # a binary that `pdf` would not have used — the opposite of a doctor command's job.
    chrome = find_chrome(required=False)
    print(f"· chrome    {chrome or 'MISSING (PDF only)'}")

    print(f"\n· cache root: {CACHE_ROOT}")
    try:
        assert_cache_workdir(CACHE_ROOT)
        print("· cache root passes the no-repo / no-output-dir guard")
    except SystemExit as e:
        print(f"· cache root REJECTED: {e}")
        ok = False
    if os.path.isdir(CACHE_ROOT):
        entries = [(dir_size(os.path.join(CACHE_ROOT, d)), d)
                   for d in os.listdir(CACHE_ROOT)
                   if os.path.isdir(os.path.join(CACHE_ROOT, d))]
        entries.sort(reverse=True)
        print(f"· cache size: {human_bytes(sum(s for s, _ in entries))} "
              f"across {len(entries)} work dir(s)")
        for size, name in entries[:3]:
            print(f"    {human_bytes(size):>10}  {name}")
    else:
        print("· cache size: not created yet")
    usage = shutil.disk_usage(_nearest_existing_dir(CACHE_ROOT))
    print(f"· free space: {human_bytes(usage.free)}")
    print(f"\n· durable root: {WISDOM_ROOT} (install --dest must be inside it; "
          f"set YOUTUBE_WISDOM_DIR to change it)")
    print(f"· vault mirror: {VAULT_ROOT or 'off (set YOUTUBE_WISDOM_VAULT to enable)'}")
    sys.exit(0 if ok else 1)


# ------------------------------------------------------------------------ pdf

# Neutral dark theme. Edit the :root tokens to restyle the PDF.
# Inter / JetBrains Mono are optional; without them the stacks fall back to system faces.
# Install for exact fidelity (macOS):
#   brew install --cask font-inter font-jetbrains-mono
CSS = """
:root {
  --bg: #0c0c0c; --bg-elevated: #181818; --bg-surface: #1f1f1f;
  --accent: #7aa2f7; --accent-muted: #5d7fc9; --accent-contrast: #0c0c0c;
  --text: #f1f1f8; --text-muted: #9f9fa5; --text-subtle: #575760;
  --border: #2a2a2d; --border-strong: #3d3d42;
  --sans: Inter, -apple-system, "SF Pro Text", "Helvetica Neue", Arial, sans-serif;
  --mono: "JetBrains Mono", "SF Mono", Menlo, Consolas, monospace;
}
/* Full-bleed dark needs margin:0 - Chrome does NOT paint the canvas background into the
   @page margin box (verified: html-background-only still leaves white gutters), so the page
   margins have to become body padding instead. */
@page { size: A4; margin: 0; }
* { box-sizing: border-box; }
/* print-color-adjust is load-bearing: without it Chrome drops every dark fill and the
   page renders as black text on white. */
html { background: var(--bg); -webkit-print-color-adjust: exact; print-color-adjust: exact; }
body { background: var(--bg); color: var(--text); font-family: var(--sans);
       font-size: 10.5pt; line-height: 1.55; margin: 0; padding: 16mm 14mm 15mm;
       -webkit-print-color-adjust: exact; print-color-adjust: exact; }
/* Body padding only guards the first and last page. Chrome drops leading MARGINS at a page
   break but keeps PADDING, so a few mm of padding-top on the blocks that can land at the top
   of a page guarantees a gutter there. On a dark full-bleed page it is invisible - the
   bottom margins below are trimmed to keep the net rhythm unchanged. */
p, ul, ol, figure, blockquote, table, h2, h3, h4 { padding-top: 2mm; }
a { color: var(--accent); text-decoration: none; }
strong { color: var(--accent); font-weight: 600; }
.cover { border-top: 3px solid var(--accent); padding-top: 14px; margin-bottom: 22px; }
.kicker { font-family: var(--mono); font-size: 8pt; letter-spacing: .1em; text-transform: uppercase;
          color: var(--accent); font-weight: 500; }
h1 { font-size: 22pt; line-height: 1.1; letter-spacing: -0.02em; font-weight: 600;
     margin: 10px 0 12px; color: var(--accent); }
.meta { font-family: var(--mono); font-size: 8pt; color: var(--text-muted);
        border-top: 1px solid var(--border); border-bottom: 1px solid var(--border); padding: 8px 0; }
.meta span { margin-right: 16px; }
.meta strong { color: var(--text-subtle); font-weight: 500; }
.meta a { color: var(--accent); }
.tldr { background: var(--bg-elevated); border-left: 3px solid var(--accent); padding: 14px 16px;
        margin: 18px 0 8px; border-radius: 0 4px 4px 0; page-break-inside: avoid; }
.tldr h2 { font-family: var(--mono); font-size: 8.5pt; margin: 0 0 8px; color: var(--accent);
           letter-spacing: .1em; text-transform: uppercase; font-weight: 500;
           border: 0; padding: 0; }
.tldr ul { margin: 0; padding-left: 18px; }
.tldr li { margin-bottom: 4px; }
section { margin-top: 11px; }
h2 { font-size: 13.5pt; color: var(--accent); font-weight: 600; letter-spacing: -0.01em;
     border-bottom: 1px solid var(--border); padding-bottom: 6px;
     margin: 0 0 10px; page-break-after: avoid; break-after: avoid; }
h2 .num { display: inline-block; font-family: var(--mono); background: var(--accent);
          color: var(--accent-contrast); font-size: 8.5pt; font-weight: 600; width: 19px;
          height: 19px; line-height: 19px; text-align: center; border-radius: 2px;
          margin-right: 9px; vertical-align: 2px; }
h2 .range { float: right; font-family: var(--mono); font-size: 8pt; color: var(--text-subtle);
            font-weight: 400; padding-top: 5px; letter-spacing: 0; }
h3 { font-size: 11pt; color: var(--text); font-weight: 600; margin: 14px 0 6px;
     page-break-after: avoid; }
h4 { font-size: 10pt; color: var(--text-muted); font-weight: 600; margin: 10px 0 4px;
     page-break-after: avoid; }
p { margin: 0 0 2px; }
ul, ol { margin: 0 0 2px; padding-left: 20px; }
li { margin-bottom: 3px; }
li::marker { color: var(--text-subtle); }
blockquote { margin: 10px 0; padding: 10px 14px; background: var(--bg-elevated);
             border-left: 3px solid var(--accent); border-radius: 0 4px 4px 0;
             font-size: 9.5pt; color: var(--text-muted); page-break-inside: avoid; }
blockquote p:last-child { margin: 0; }
table { border-collapse: collapse; width: 100%; margin: 10px 0 12px; font-size: 9pt;
        page-break-inside: avoid; }
th { background: var(--bg-elevated); color: var(--accent); font-family: var(--mono);
     font-size: 7.5pt; font-weight: 500; letter-spacing: .06em; text-transform: uppercase;
     text-align: left; padding: 7px 8px; border-bottom: 1px solid var(--accent); }
td { border-bottom: 1px solid var(--border); padding: 6px 8px; vertical-align: top;
     color: var(--text); }
tbody tr:nth-child(even) td { background: rgba(255, 255, 255, 0.022); }
code { font-family: var(--mono); font-size: 8.5pt; background: var(--bg-surface);
       color: var(--accent); padding: 1px 5px; border-radius: 2px;
       border: 1px solid var(--border); }
blockquote code, td code, th code { border-color: transparent; }
figure { margin: 12px auto 6px; page-break-inside: avoid; width: 88%; }
figure img { width: 100%; border: 1px solid var(--border); border-radius: 4px; display: block; }
figcaption { font-size: 8.5pt; color: var(--text-muted); padding-top: 6px; line-height: 1.4; }
figcaption .ts { display: inline-block; font-family: var(--mono); background: var(--accent);
                 color: var(--accent-contrast); font-weight: 600; font-size: 7.5pt;
                 letter-spacing: .04em; padding: 1px 6px; border-radius: 2px; margin-right: 7px; }
footer { margin-top: 26px; border-top: 1px solid var(--border); padding-top: 10px;
         font-family: var(--mono); font-size: 7.5pt; color: var(--text-subtle); line-height: 1.5; }
"""

FIGURE_RE = re.compile(r'^!\[([^\]]*)\]\(([^)\s]+)(?:\s+"([^"]*)")?\)$')
HEADING_RE = re.compile(r"^##\s+(.*?)\s*(?:\{([^}]*)\})?\s*$")
TLDR_TITLES = {"tl;dr", "tldr", "summary", "in short"}


def inline(text):
    """Minimal, predictable inline markdown: `code`, **bold**, *it*, [txt](url).

    Code spans are stashed behind sentinels BEFORE html.escape, so escaping is correct and
    XSS-safety comes for free. Do not reorder these four substitutions.
    """
    spans = []

    def stash(html_fragment):
        spans.append(html_fragment)
        return f"\x00{len(spans) - 1}\x00"

    text = re.sub(r"`([^`]+)`", lambda m: stash(f"<code>{html_mod.escape(m.group(1))}</code>"), text)
    text = html_mod.escape(text)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<em>\1</em>", text)
    return re.sub(r"\x00(\d+)\x00", lambda m: spans[int(m.group(1))], text)


def render_blocks(lines, base_dir, embed):
    """Markdown lines -> HTML. Returns (html, n_figures)."""
    html_parts, figures, i = [], 0, 0
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
            continue

        if line.strip().startswith("!["):
            fig = FIGURE_RE.match(line.strip())
            if not fig:
                # Malformed figure lines are an error, never silently dropped - otherwise the PDF
                # quietly misses a figure. A path with a space or trailing text is the usual cause.
                die(f'malformed figure line (expected `![ts](path "caption")`):\n  {line.strip()}')
            ts, path, caption = fig.group(1), fig.group(2), fig.group(3) or ""
            html_parts.append(embed(os.path.join(base_dir, path), ts, caption))
            figures += 1
            i += 1
            continue

        # h4 before h3 is not required (the h3 test carries a trailing space) but keeps the
        # deepest-first reading obvious. Without an h4 arm, `#### ` lines hit the paragraph
        # guard's "###" prefix test below and were dropped silently.
        if line.startswith("#### "):
            html_parts.append(f"<h4>{inline(line[5:].strip())}</h4>")
            i += 1
            continue

        if line.startswith("### "):
            html_parts.append(f"<h3>{inline(line[4:].strip())}</h3>")
            i += 1
            continue

        if line.lstrip().startswith(("- ", "* ")) or re.match(r"^\d+\.\s", line.lstrip()):
            ordered = bool(re.match(r"^\d+\.\s", line.lstrip()))
            items = []
            # Stopping only at a blank line swallowed any block that followed a bullet without
            # one: a figure line became an "indented continuation" of the last <li> and the image
            # vanished, and the die() above never fired because the line never reached that arm.
            # Same guard the paragraph loop below already carries.
            while i < len(lines) and lines[i].strip() \
                    and not lines[i].strip().startswith(("![", "|", ">", "###")):
                item = re.sub(r"^\s*(?:[-*]|\d+\.)\s+", "", lines[i])
                if re.match(r"^\s*(?:[-*]|\d+\.)\s", lines[i]) or not items:
                    items.append(item.strip())
                else:                                    # indented continuation
                    items[-1] += " " + lines[i].strip()
                i += 1
            tag = "ol" if ordered else "ul"
            body = "".join(f"<li>{inline(x)}</li>" for x in items)
            html_parts.append(f"<{tag}>{body}</{tag}>")
            continue

        if line.strip().startswith("|"):
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
                i += 1
            head, body = rows[0], rows[1:]
            if body and all(re.fullmatch(r":?-{2,}:?", c) for c in body[0]):
                body = body[1:]
            thead = "".join(f"<th>{inline(c)}</th>" for c in head)
            tbody = "".join(
                "<tr>" + "".join(f"<td>{inline(c)}</td>" for c in row) + "</tr>" for row in body)
            html_parts.append(f"<table><thead><tr>{thead}</tr></thead><tbody>{tbody}</tbody></table>")
            continue

        if line.strip().startswith(">"):
            quote = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                quote.append(lines[i].strip().lstrip(">").strip())
                i += 1
            html_parts.append(f"<blockquote><p>{inline(' '.join(quote))}</p></blockquote>")
            continue

        para = []
        while i < len(lines) and lines[i].strip() \
                and not lines[i].strip().startswith(("|", ">", "###", "![")) \
                and not lines[i].lstrip().startswith(("- ", "* ")) \
                and not re.match(r"^\d+\.\s", lines[i].lstrip()):
            para.append(lines[i].strip())
            i += 1
        if para:
            html_parts.append(f"<p>{inline(' '.join(para))}</p>")
        else:
            i += 1
    return "\n".join(html_parts), figures


def parse_front_matter(text):
    if not text.startswith("---"):
        die("the source markdown must start with a `---` front matter block "
            "(title, channel, url, ...)")
    # split("---", 2) yields fewer than 3 parts when the header block is never closed, and the
    # bare unpack raised a naked "ValueError: not enough values to unpack" traceback at the user.
    parts = text.split("---", 2)
    if len(parts) < 3:
        die("front matter is not terminated - close the header block with a second "
            "`---` line before the body")
    _, fm, body = parts
    meta = {}
    for line in fm.strip().splitlines():
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        v = v.strip()
        if v.startswith(('"', "'")):
            quote = v[0]
            end = v.find(quote, 1)
            v = v[1:end] if end > 0 else v[1:]
        else:
            v = v.split(" #", 1)[0].strip()          # trailing comment, outside quoted values
        meta[k.strip()] = v
    return meta, body


def cmd_pdf(args):
    chrome = find_chrome()
    src = os.path.abspath(args.source)
    if not os.path.exists(src):
        die(f"{src} not found")
    base_dir = os.path.dirname(src)
    with open(src, encoding="utf-8") as f:
        meta, body = parse_front_matter(f.read())

    if args.meta:
        meta_path = os.path.abspath(os.path.expanduser(args.meta))
        if os.path.isdir(meta_path):
            meta_path = os.path.join(meta_path, "meta.json")
        if not os.path.exists(meta_path):
            die(f"--meta {meta_path} not found")
        with open(meta_path, encoding="utf-8") as f:
            defaults = json.load(f)
        for key in ("title", "channel", "duration", "published", "url"):
            if not meta.get(key) and defaults.get(key):
                meta[key] = str(defaults[key])          # front matter always wins

    total_bytes = [0]

    def embed(path, ts, caption):
        if not os.path.exists(path):
            die(f"figure {path} does not exist (figure paths are relative to {base_dir})")
        with open(path, "rb") as f:
            raw = f.read()
        total_bytes[0] += len(raw)
        ext = "png" if path.lower().endswith(".png") else "jpeg"
        b64 = base64.b64encode(raw).decode()
        ts_html = f'<span class="ts">{html_mod.escape(ts)}</span>' if ts else ""
        return (f'<figure><img src="data:image/{ext};base64,{b64}" alt="">'
                f"<figcaption>{ts_html}{inline(caption)}</figcaption></figure>")

    # split on level-2 headings; everything before the first `##` is ignored on purpose,
    # so the markdown can carry author scratch notes that never reach the PDF.
    chunks, current = [], None
    for line in body.splitlines():
        m = HEADING_RE.match(line)
        if m:
            current = {"title": m.group(1).strip(), "range": (m.group(2) or "").strip(), "lines": []}
            chunks.append(current)
        elif current is not None:
            current["lines"].append(line)
    if not chunks:
        die("no `## ` sections found after the front matter - nothing to render")

    tldr_html, sections_html, n_figs = "", [], 0
    for chunk in chunks:
        inner, figs = render_blocks(chunk["lines"], base_dir, embed)
        n_figs += figs
        title = chunk["title"]
        if title.lower().rstrip(":") in TLDR_TITLES and not tldr_html:
            tldr_html = f'<div class="tldr"><h2>{html_mod.escape(title)}</h2>{inner}</div>'
            continue
        num_m = re.match(r"^(\d+)[.)]?\s+(.*)$", title)
        num = f'<span class="num">{num_m.group(1)}</span>' if num_m else ""
        label = num_m.group(2) if num_m else title
        rng = f'<span class="range">{html_mod.escape(chunk["range"])}</span>' if chunk["range"] else ""
        sections_html.append(
            f"<section><h2>{num}{html_mod.escape(label)}{rng}</h2>\n{inner}</section>")

    channel = meta.get("channel", "the rights holder")
    footer = meta.get("footer") or (
        "Original summary written from the video transcript; the screenshots are frames of the "
        "source, referenced by timestamp so they can be verified against it. The video rights "
        f"belong to {channel}.")
    meta_bits = []
    for label, key in (("Channel", "channel"), ("Duration", "duration"), ("Published", "published")):
        if meta.get(key):
            meta_bits.append(f"<span><strong>{label}:</strong> {html_mod.escape(meta[key])}</span>")
    if meta.get("url"):
        href = html_mod.escape(meta["url"], quote=True)
        meta_bits.append(f'<br><span><strong>Source:</strong> <a href="{href}">'
                         f'{html_mod.escape(meta["url"])}</a></span>')

    doc = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>{html_mod.escape(meta.get('title', 'Video summary'))}</title><style>{CSS}</style></head><body>
<div class="cover">
  <div class="kicker">{html_mod.escape(meta.get('kicker', 'Video summary'))}</div>
  <h1>{html_mod.escape(meta.get('title', ''))}</h1>
  <div class="meta">{''.join(meta_bits)}</div>
</div>
{tldr_html}
{''.join(sections_html)}
<footer>{inline(footer)}</footer>
</body></html>"""

    out_pdf = assert_safe_output(args.out, ".pdf", b"%PDF-", args.allow_durable, "the output PDF")
    if args.keep_html:
        assert_safe_output(os.path.splitext(out_pdf)[0] + ".html", ".html", b"<",
                           args.allow_durable, "the --keep-html sibling")
    os.makedirs(os.path.dirname(out_pdf) or ".", exist_ok=True)
    tmp = tempfile.mkdtemp(prefix="video_kit_")
    try:
        html_path = os.path.join(tmp, "render.html")
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(doc)
        file_url = urllib.parse.urljoin("file:", urllib.request.pathname2url(html_path))
        chrome_print_pdf(chrome, file_url, out_pdf, os.path.join(tmp, "chrome.log"))
        if args.keep_html:
            kept = os.path.splitext(out_pdf)[0] + ".html"
            shutil.copyfile(html_path, kept)
            print(f"· intermediate HTML: {kept}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"· {len(sections_html)} sections · {n_figs} figures "
          f"({total_bytes[0] / 1e6:.1f} MB of images)")
    print(f"· PDF: {out_pdf} ({os.path.getsize(out_pdf) / 1e6:.2f} MB)")
    print(f"· verify the layout with:  {sys.executable} {os.path.abspath(__file__)} "
          f"check {out_pdf}")


# ---------------------------------------------------------------------- check

def cmd_check(args):
    pdftoppm = resolve_bin("pdftoppm", "PDFTOPPM_BIN", "/opt/homebrew/bin/pdftoppm",
                           "brew install poppler")
    try:
        from PIL import Image
    except ModuleNotFoundError:
        die("Pillow is missing - install it with: python3 -m pip install Pillow")

    pdf = os.path.abspath(args.pdf)
    if not os.path.exists(pdf):
        die(f"{pdf} not found")
    # Default the sheet into the temp dir, not next to the PDF: it is a verification artifact, and
    # a PDF that legitimately lives in the output dir must not drag a tracked .png in beside it.
    out = assert_safe_output(
        args.out or os.path.join(tempfile.gettempdir(),
                                 os.path.basename(os.path.splitext(pdf)[0]) + ".pages.png"),
        ".png", b"\x89PNG", args.allow_durable, "the verification sheet")
    tmp = tempfile.mkdtemp(prefix="video_kit_pages_")
    try:
        run_checked([pdftoppm, "-png", "-r", "80", pdf, os.path.join(tmp, "p")],
                    "pdftoppm rasterise")
        pages = glob.glob(os.path.join(tmp, "p-*.png"))
        if not pages:
            die("pdftoppm produced no pages")
        def _page_no(path):
            m = re.search(r"p-(\d+)\.png$", path)
            return int(m.group(1)) if m else 0

        pages.sort(key=_page_no)

        with Image.open(pages[0]) as probe:
            ratio = probe.height / probe.width
        tw = args.thumb
        th = max(1, int(round(tw * ratio)))
        cols = min(max(1, args.cols), len(pages))
        rows = (len(pages) + cols - 1) // cols
        sheet = Image.new("RGB", (cols * tw, rows * th), "#999999")
        for i, p in enumerate(pages):
            with Image.open(p) as im:
                sheet.paste(im.convert("RGB").resize((tw, th)), ((i % cols) * tw, (i // cols) * th))
        sheet.save(out)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"· {len(pages)} pages -> {out}")
    print("· look at the sheet: half-empty pages mean `page-break-inside: avoid` is pushing "
          "content; compact it or narrow the figures.")


# ----------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(
        prog="video_kit.py", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("fetch", help="subtitles -> timestamped transcript.md (+ optional video)")
    p.add_argument("url")
    p.add_argument("--workdir", help=f"default: {CACHE_ROOT}/<video id>")
    p.add_argument("--sub-langs", default=SUB_LANGS_FALLBACK,
                   help=f"FALLBACK language priority; the spoken language and the *-orig auto "
                        f"track win first (default: {SUB_LANGS_FALLBACK})")
    p.add_argument("--block", type=int, default=20,
                   help="seconds per transcript block; every cue in a block starts within this "
                        "many seconds of the block label (default: 20)")
    p.add_argument("--video", action="store_true",
                   help="also download the video (opt-in; only needed for the frames branch)")
    p.add_argument("--height", type=int, default=720, help="max video height (default: 720)")
    p.add_argument("--max-mb", type=float, default=600.0,
                   help="refuse a video download estimated above this (default: 600)")
    p.add_argument("--force", action="store_true", help="redo work even if artifacts exist")
    p.set_defaults(func=cmd_fetch)

    p = sub.add_parser("frames", help="scene-change frames and/or targeted --at frames")
    p.add_argument("--workdir", required=True)
    p.add_argument("--threshold", type=float, default=0.35,
                   help="ffmpeg scene metric (default: 0.35; raise to get fewer frames)")
    p.add_argument("--at", help="targeted timestamps, comma separated: 40,1:35,206,1:02:03")
    p.add_argument("--width", type=int, default=960, help="scene-dump width (default: 960)")
    p.add_argument("--pick-width", type=int, default=1280,
                   help="--at frame width; these are the artifact candidates (default: 1280)")
    p.add_argument("--quality", type=int, default=4, help="ffmpeg -q:v (default: 4)")
    p.add_argument("--clear-picks", action="store_true", help="delete previous --at picks first")
    p.add_argument("--no-scenes", action="store_true",
                   help="skip the scene pass; only extract the --at frames")
    p.set_defaults(func=cmd_frames)

    p = sub.add_parser("sheet", help="one labelled contact sheet of every extracted frame")
    p.add_argument("--workdir", required=True)
    p.add_argument("--cols", type=int, default=6)
    p.add_argument("--thumb", type=int, default=330)
    p.add_argument("--per-sheet", type=int, default=42)
    p.set_defaults(func=cmd_sheet)

    p = sub.add_parser("install",
                       help="copy the durable artifacts from the cache into the output dir")
    p.add_argument("--workdir", required=True)
    p.add_argument("--dest", required=True,
                   help=f"{WISDOM_ROOT}/<YYYY-MM-DD>-<slug> (must be inside the output dir)")
    p.add_argument("--frame", action="append", metavar="SPEC",
                   help="repeatable: frames/scene_007.jpg, pick/t95.jpg, or a bare timestamp 2:13")
    p.add_argument("--max-frames", type=int, default=0,
                   help="hard stop on installed frames (default: 0 = no limit; set a number to cap)")
    p.add_argument("--frame-width", type=int, default=1280, help="installed frame width")
    p.add_argument("--frame-quality", type=int, default=4, help="installed frame ffmpeg -q:v")
    p.add_argument("--keep-subs", action="store_true", help="also copy the raw .vtt")
    p.set_defaults(func=cmd_install)

    p = sub.add_parser("doctor", help="check binaries, Pillow, cache location and size")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("prune", help="report (and with --yes reclaim) cache disk usage")
    p.add_argument("--workdir", help="a single cache work dir; default: every one")
    p.add_argument("--older-than", type=float, metavar="DAYS",
                   help="only work dirs untouched for this many days")
    p.add_argument("--all", action="store_true",
                   help="remove the whole work dir, not just video + scene dump + sheets")
    p.add_argument("--yes", action="store_true", help="actually delete (default is a dry run)")
    p.set_defaults(func=cmd_prune)

    p = sub.add_parser("pdf", help="OPTIONAL: render a markdown summary to a shareable PDF")
    p.add_argument("source", help="markdown with front matter + `## ` sections")
    p.add_argument("out", help="output .pdf")
    p.add_argument("--meta", help="meta.json (or a work dir) to default the front matter from")
    p.add_argument("--keep-html", action="store_true", help="keep the intermediate HTML next to it")
    p.add_argument("--allow-durable", action="store_true",
                   help=f"permit writing into {WISDOM_ROOT} (the durable output dir); off by default")
    p.set_defaults(func=cmd_pdf)

    p = sub.add_parser("check", help="OPTIONAL: rasterise a PDF to verify its layout")
    p.add_argument("pdf")
    p.add_argument("--out", help=f"output .png (default: a sheet in {tempfile.gettempdir()})")
    p.add_argument("--cols", type=int, default=6)
    p.add_argument("--thumb", type=int, default=330)
    p.add_argument("--allow-durable", action="store_true",
                   help=f"permit writing the sheet into {WISDOM_ROOT}; off by default")
    p.set_defaults(func=cmd_check)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
