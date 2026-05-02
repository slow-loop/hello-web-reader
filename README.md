# web-reader

Lightweight async web reader — URL in, clean text out.

## Installation

```bash
# Basic
uv add web-reader

# With Google News support
uv add "web-reader[gnews]"

# With YouTube transcript support
uv add "web-reader[youtube]"

# With email support
uv add "web-reader[email]"

# With MCP server support
uv add "web-reader[mcp]"
```

---

## CLI

### 直接讀取 URL

```bash
uv run web-reader <URL> [--format=md|json] [--no-cache] [-v]
```

#### 支援的來源（自動偵測）

| 來源 | 範例 |
|------|------|
| 一般網頁 | `uv run web-reader "https://example.com/article"` |
| RSS Feed | `uv run web-reader "https://hnrss.org/frontpage"` |
| Reddit | `uv run web-reader "https://reddit.com/r/investing/hot"` |
| YouTube | `uv run web-reader "https://youtube.com/watch?v=xxx"` |
| Substack | `uv run web-reader "https://xxx.substack.com"` |
| PTT | `uv run web-reader "https://www.ptt.cc/bbs/Stock/index.html"` |
| Google News | `uv run web-reader "gnews://NVDA stock"` |

#### Google News 搜尋（`gnews://` pseudo-URL）

```bash
# 基本搜尋
uv run web-reader "gnews://NVDA stock"

# 加參數：period（時間範圍）、max（筆數）
uv run web-reader "gnews://台積電?period=3d&max=5"
uv run web-reader "gnews://Fed interest rate?period=7d&max=10"
```

| 參數 | 說明 | 預設值 |
|------|------|--------|
| `period` | 時間範圍：`1d` `3d` `7d` `30d` | `7d` |
| `max` | 最多回傳幾筆 | `10` |

> gnews 回傳的是**標題 + 來源 URL**，不含全文。要拿全文請把 URL 再丟給 `read_web()`。

#### PTT 版面與討論

```bash
# 股票版最新列表
uv run web-reader "https://www.ptt.cc/bbs/Stock/index.html"

# 搜尋
uv run web-reader "https://www.ptt.cc/bbs/Stock/search?q=台積電"

# 讀取單篇討論（含推文）
uv run web-reader "https://www.ptt.cc/bbs/Stock/M.1774581562.A.27A.html"
```

### 選項

| 選項 | 說明 |
|------|------|
| `--format=md` | Markdown 輸出（預設 `json`）|
| `--no-cache` | 不用 cache，強制重新抓取 |
| `-v` | 顯示 debug log |

---

### YAML Config（批次抓取）

```bash
uv run web-reader feeds.yaml
uv run web-reader feeds.yaml --tags=stock
uv run web-reader feeds.yaml --format=md --no-cache
```

#### feeds.yaml 範例

```yaml
sources:
  - name: HN frontpage
    reader: rss
    url: https://hnrss.org/frontpage
    tags: [tech]

  - name: r/investing hot
    reader: reddit
    params:
      subreddit: investing
      sort: hot
      limit: 20
    tags: [stock]

  - name: NVDA news
    reader: gnews
    params:
      query: "NVDA stock"
      period: 3d
      max_results: 10
    tags: [stock]

  - name: PTT 股票版
    reader: ptt
    url: https://www.ptt.cc/bbs/Stock/index.html
    tags: [stock, tw]

  - name: Bloomberg
    reader: rss
    url: https://feeds.bloomberg.com/markets/news.rss
    cache:
      ttl: 1800
    tags: [stock]
```

#### 支援的 reader 值

| reader | 說明 |
|--------|------|
| `web` | 一般網頁 |
| `rss` | RSS / Atom feed |
| `reddit` | Reddit subreddit 或 thread |
| `youtube` | YouTube 字幕 |
| `substack` | Substack newsletter |
| `gnews` | Google News 關鍵字搜尋 |
| `ptt` | PTT 版面 / 搜尋 / 單篇 |
| `json` | JSON API endpoint |
| `email` | IMAP 信箱 |

---

## Python API

```python
from web_reader import read_url, read_urls, read_gnews, read_ptt

# 自動偵測來源
result = await read_url("https://www.ptt.cc/bbs/Stock/index.html")
print(result.text)

# 批次
results = await read_urls(["https://...", "https://..."])

# GNews 搜尋
results = await read_gnews("NVDA stock", period="7d", max_results=5)
for r in results:
    print(r.title, r.url)

# PTT
result = await read_ptt("https://www.ptt.cc/bbs/Stock/index.html")
result = await read_ptt("https://www.ptt.cc/bbs/Stock/search?q=台積電")
result = await read_ptt("https://www.ptt.cc/bbs/Stock/M.xxx.html")  # 單篇

# 加 cache（預設用使用者層的共享路徑，例如 ~/Library/Caches/web-reader/store.db）
# 跨專案共享同一個 cache；想隔離可設 WEB_READER_DB_PATH 或傳路徑給 ReadStore(...)
from web_reader import ReadStore
store = ReadStore()
result = await read_url("https://example.com", store=store, cache_ttl=3600)
```

---

## MCP Server

`web-reader` 也提供通用 `stdio` MCP server，不綁任何特定 domain config。

### 啟動

```bash
uv run web-reader-mcp
```

如果你是以 extra 安裝：

```bash
uv add "web-reader[mcp]"
```

### 提供的 tools

| tool | 說明 |
|------|------|
| `read_url` | 讀單一 URL 或 pseudo-URL，例如 `gnews://台積電?period=3d&max=5`、`substack://search?q=AI&page=1` |
| `read_config` | 執行 YAML config，支援 `tags` 過濾 |
| `web_reader_server_info` | 回傳 server metadata |

### MCP 使用建議

- `read_url(url, output_format="md", no_cache=False)`
- `read_config(config_path, tags=None, output_format="md", no_cache=False)`

如果你有 trader、research 或別的 domain-specific workflow，建議在外層 repo 再包一層固定 config / tags，而不是把 domain logic 寫進 `web-reader` 本體。
