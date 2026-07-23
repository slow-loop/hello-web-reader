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
uv sync
```

Copy the env template and fill in your keys:

```bash
cp feeds.example.yaml feeds.yaml   # customise your sources
# Add to .env: YOUTUBE_API_KEY, YAHOO_MAIL_USERNAME, YAHOO_MAIL_PASSWORD
```

## CLI

### Single URL

```bash
uv run web-reader <URL> [--format=md|json] [--no-cache] [-v]
```

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

> `gnews` returns title + source URL only. To get full text, pipe the URL back through `web-reader`.

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

# With cache (shared across projects by default: ~/Library/Caches/web-reader/store.db)
from web_reader import ReadStore
store = ReadStore()
result = await read_url("https://example.com", store=store, cache_ttl=3600)
```

## Scripts

Standalone scripts in [`scripts/`](scripts/), not wired into the `web-reader` CLI:

| Script | Description |
|---|---|
| `fetch_youtube_channel.py` | Fetch the N most recent videos from a channel (handle or URL) + transcripts |
| `transcribe_local_audio.py` | Batch-transcribe local audio files via the ASR + LLM polish pipeline |
| `split_audio_on_silence.py` | Split an audio file into chunks near silence boundaries (used internally by `transcribe_local_audio.py` for long/large files) |
| `export_claude_conversations.py` | Export local Claude Code / Claude Desktop Cowork conversation history to plain files (macOS only) |

```bash
uv run scripts/fetch_youtube_channel.py --handle @example --limit 5 --out ./output/example

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
