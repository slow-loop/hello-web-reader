# hello-web-reader

**English** | [繁體中文](README.zh-TW.md)

Lightweight async web reader — URL in, clean text out. Supports web pages, RSS, Reddit, YouTube, PTT, Google News, Apple Podcasts, and more. Includes a CLI, Python API, and MCP server.

> Python package name: `web-reader` · CLI commands: `web-reader`, `web-reader-mcp`

## Installation

Not on PyPI — install directly from GitHub:

```bash
# Add to a project
uv add "web-reader @ git+https://github.com/w121211/hello-web-reader.git@v0.1.0"

# Or clone and run standalone
git clone https://github.com/w121211/hello-web-reader.git
cd hello-web-reader
uv sync                        # readers only
uv sync --extra transcribe     # + local ASR (funasr / torch, ~2GB)
```

`fetch` needs the `transcribe` extra whenever the watchlist has `rss_podcast`
sources or caption-less YouTube videos — without it those items fail one by one
with `No module named 'funasr'`, the run still exits 3 ("partial"), and the
missing episodes look like a quiet day rather than a broken install.

Copy the env template and fill in your keys:

```bash
cp feeds.example.yaml feeds.yaml   # customise your sources
# Add to .env: YOUTUBE_API_KEY, YAHOO_MAIL_USERNAME, YAHOO_MAIL_PASSWORD
```

## CLI

### Single URL

```bash
uv run web-reader read <URL> [--format=md|json] [--no-cache] [--no-asr] [-v]
```

A YouTube video is archived into the `output/` store on the way through — the
same `output/youtube/<handle>/{subtitles,transcripts}/` that `channel` and
`fetch` write — and a repeat read is served from there without touching the
network. `read`, `channel` and `fetch` therefore never re-fetch each other's
videos. Add `--no-asr` to fail instead of falling back to local audio
transcription, which costs minutes of compute on a caption-less video.

Other source types are printed and kept only in the short-TTL scratch cache;
they have no home in `output/`.

| Source | Example |
|---|---|
| Web page | `uv run web-reader "https://example.com/article"` |
| RSS feed | `uv run web-reader "https://hnrss.org/frontpage"` |
| Reddit | `uv run web-reader "https://reddit.com/r/investing/hot"` |
| YouTube | `uv run web-reader "https://youtube.com/watch?v=xxx"` |
| Substack | `uv run web-reader "https://xxx.substack.com"` |
| PTT | `uv run web-reader "https://www.ptt.cc/bbs/Stock/index.html"` |
| Google News | `uv run web-reader "gnews://NVDA stock"` |

#### Google News (`gnews://` pseudo-URL)

```bash
uv run web-reader "gnews://NVDA stock"
uv run web-reader "gnews://台積電?period=3d&max=5"
uv run web-reader "gnews://Fed interest rate?period=7d&max=10"
```

| Param | Description | Default |
|---|---|---|
| `period` | Time range: `1d` `3d` `7d` `30d` | `7d` |
| `max` | Max results | `10` |

### YouTube channel

List a channel's videos with full metadata via the YouTube Data API. Requires
`YOUTUBE_API_KEY`. The default is a pure API call — ~1300 videos in ~15s, about
55 quota units. Nothing is downloaded until you add a flag. Each run writes two
timestamped files (so re-running never overwrites an earlier snapshot):

- `videos_<timestamp>.json` — the complete API response per video (description,
  tags, statistics, status, all thumbnail sizes); nothing discarded
- `manifest_<timestamp>.csv` — readable subset: date, duration, title, views,
  likes, comments, caption availability, definition, language, tags, url

Thumbnails and subtitles go in `thumbnails/` and `subtitles/`, keyed by video
ID and skipped if already present, so they accumulate across runs.

```bash
# Metadata only (API, no downloads), default 30 most-recent videos
uv run web-reader channel @example --limit 50

# Also download cover images (no video)
uv run web-reader channel @example --limit 50 --thumbnails

# Also fetch subtitle transcripts (the API's caption flag skips the rest)
uv run web-reader channel @example --limit 50 --subtitles --lang zh-Hant,en
```

Snapshot files (`manifest_<stamp>.csv`, `videos_<stamp>.json`, `thumbnails/`) go
to `output/youtube/<handle>/` unless `--out` is given. Transcripts always go
through the store contract — `output/youtube/<handle>/{subtitles,transcripts}/`,
never moved by `--out`. Re-running skips what is already on disk.

> `gnews` returns title + source URL only. To get full text, pipe the URL back through `web-reader`.

### Watchlist fetch (incremental archive)

The recurring acquisition step: read a watchlist (feeds.yaml format), fetch
whatever is new in the window, and archive it under `output/` as markdown with
frontmatter (layout contract in `web_reader.store`). Already-archived items
are never re-fetched, so overlapping windows are free.

- `rss` → article full text → `output/substack/<id>/`
- `rss_podcast` → audio download + local ASR → `output/podcast/<id>/` (audio kept in `audio/`)
- `youtube_channel` → captions, ASR fallback for caption-less videos →
  `output/youtube/<handle>/{subtitles,transcripts}/`

```bash
uv run web-reader fetch path/to/watchlist.yaml   # last 24h
uv run web-reader fetch path/to/watchlist.yaml --since -7d --dry-run
uv run web-reader fetch path/to/watchlist.yaml --source some-id --limit 10
```

The watchlist path is explicit so any repo can point web-reader at its own
list; output always lands in this repo's `output/` archive.

`--limit` caps *new* expensive fetches (ASR / yt-dlp) per source; `--dry-run`
prices the batch (worst-case ASR hours) without downloading anything.

### YAML feed config (batch fetch)

```bash
uv run web-reader feeds.yaml
uv run web-reader feeds.yaml --tags=finance
uv run web-reader feeds.yaml --format=md --no-cache
```

See [`feeds.example.yaml`](feeds.example.yaml) for a full config template.

Supported `reader` values:

| reader | Description |
|---|---|
| `web` | Generic web page |
| `rss` | RSS / Atom feed |
| `reddit` | Subreddit or thread |
| `youtube` | YouTube transcript |
| `substack` | Substack newsletter |
| `gnews` | Google News keyword search |
| `ptt` | PTT board / search / single post |
| `json` | JSON API endpoint |
| `email` | IMAP mailbox |
| `apple_podcast` | Apple Podcasts (Mac only, episodes must be downloaded) |

### Options

| Option | Description |
|---|---|
| `--format=md` | Markdown output (default: `json`) |
| `--no-cache` | Skip cache, force re-fetch |
| `-v` | Debug logging |

## Python API

```python
from web_reader import read_url, read_urls, read_gnews, read_ptt

# Auto-detect source type
result = await read_url("https://www.ptt.cc/bbs/Stock/index.html")
print(result.text)

# Batch
results = await read_urls(["https://...", "https://..."])

# Google News search
results = await read_gnews("NVDA stock", period="7d", max_results=5)
for r in results:
    print(r.title, r.url)

# PTT — board listing, search, or single post
result = await read_ptt("https://www.ptt.cc/bbs/Stock/index.html")
result = await read_ptt("https://www.ptt.cc/bbs/Stock/search?q=台積電")
result = await read_ptt("https://www.ptt.cc/bbs/Stock/M.xxx.html")

# With cache (shared across projects by default: ~/Library/Caches/web-reader/cache.db)
from web_reader import ReadCache
cache = ReadCache()
result = await read_url("https://example.com", cache=cache, cache_ttl=3600)
```

## Scripts

Standalone scripts in [`scripts/`](scripts/), not wired into the `web-reader` CLI:

| Script | Description |
|---|---|
| `transcribe_local_audio.py` | Batch-transcribe local audio files via the ASR + LLM polish pipeline |
| `split_audio_on_silence.py` | Split an audio file into chunks near silence boundaries (used internally by `transcribe_local_audio.py` for long/large files) |
| `export_claude_conversations.py` | Export local Claude Code / Claude Desktop Cowork conversation history to plain files (macOS only) |
| `rednote_liked.js` | Index and fetch the signed-in rednote.com account's Like tab (needs OpenCLI + a logged-in controlled Chrome) |

Indexing and fetching are separate on purpose: building the index costs zero
per-note requests, so it can cover everything and run often; fetching a note
costs one page load, so it runs only on the notes actually picked for a video.
Pool lands in `output/rednote/liked/<note-id>/`. Full notes in the script header.

```bash
node scripts/rednote_liked.js                          # print the index (default, offline)
node scripts/rednote_liked.js --index                  # refresh it (3 scrolls, ~40 notes)
node scripts/rednote_liked.js --index --scrolls all    # full sweep
node scripts/rednote_liked.js --fetch <id> [<id>...]   # already-fetched ids are skipped
```

```bash
.venv/bin/python scripts/transcribe_local_audio.py output/example/audio --out output/example/transcripts --skip-existing

uv run scripts/split_audio_on_silence.py input.m4a -o ./output/example/chunks

uv run scripts/export_claude_conversations.py --project hello-trader-skill --days 7 --dry-run
```

## MCP Server

A generic `stdio` MCP server — no domain-specific config baked in.

```bash
uv run web-reader-mcp
```

### Tools

| Tool | Description |
|---|---|
| `read_url` | Read a single URL or pseudo-URL (e.g. `gnews://台積電?period=3d&max=5`) |
| `read_config` | Run a YAML feed config, with optional tag filtering |
| `web_reader_server_info` | Return server metadata |

Usage: `read_url(url, output_format="md", no_cache=False)` · `read_config(config_path, tags=None, output_format="md")`

If you have a domain-specific workflow (e.g. a trading agent), wrap this server with a fixed config/tags in your own repo rather than putting domain logic inside `hello-web-reader`.

## License

MIT — see [LICENSE](LICENSE).
