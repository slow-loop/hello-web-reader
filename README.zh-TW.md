# hello-web-reader

[English](README.md) | **繁體中文**

輕量非同步網頁讀取器——丟 URL 進去，拿乾淨文字出來。支援一般網頁、RSS、Reddit、YouTube、PTT、Google News、Apple Podcast 等。提供 CLI、Python API 與 MCP server。

> Python 套件名稱：`web-reader` · CLI 指令：`web-reader`、`web-reader-mcp`

## 安裝

尚未發布到 PyPI，直接從 GitHub 安裝：

```bash
# 加入專案
uv add "web-reader @ git+https://github.com/w121211/hello-web-reader.git@v0.1.0"

# 或 clone 獨立執行
git clone https://github.com/w121211/hello-web-reader.git
cd hello-web-reader
uv sync
```

複製設定範本：

```bash
cp feeds.example.yaml feeds.yaml   # 自訂來源
# .env 填入：GROQ_API_KEY、YOUTUBE_API_KEY、YAHOO_MAIL_USERNAME、YAHOO_MAIL_PASSWORD
```

## CLI

### 單一 URL

```bash
uv run web-reader <URL> [--format=md|json] [--no-cache] [-v]
```

| 來源 | 範例 |
|---|---|
| 一般網頁 | `uv run web-reader "https://example.com/article"` |
| RSS feed | `uv run web-reader "https://hnrss.org/frontpage"` |
| Reddit | `uv run web-reader "https://reddit.com/r/investing/hot"` |
| YouTube | `uv run web-reader "https://youtube.com/watch?v=xxx"` |
| Substack | `uv run web-reader "https://xxx.substack.com"` |
| PTT | `uv run web-reader "https://www.ptt.cc/bbs/Stock/index.html"` |
| Google News | `uv run web-reader "gnews://台積電"` |

#### Google News（`gnews://` pseudo-URL）

```bash
uv run web-reader "gnews://台積電"
uv run web-reader "gnews://台積電?period=3d&max=5"
uv run web-reader "gnews://Fed interest rate?period=7d&max=10"
```

| 參數 | 說明 | 預設值 |
|---|---|---|
| `period` | 時間範圍：`1d` `3d` `7d` `30d` | `7d` |
| `max` | 最多回傳幾筆 | `10` |

> `gnews` 只回傳標題 + 來源 URL，不含全文。要拿全文請把 URL 再丟給 `web-reader`。

### YAML 設定檔（批次抓取）

```bash
uv run web-reader feeds.yaml
uv run web-reader feeds.yaml --tags=finance
uv run web-reader feeds.yaml --format=md --no-cache
```

完整設定範本請參考 [`feeds.example.yaml`](feeds.example.yaml)。

支援的 `reader` 值：

| reader | 說明 |
|---|---|
| `web` | 一般網頁 |
| `rss` | RSS / Atom feed |
| `reddit` | Reddit 版面或討論串 |
| `youtube` | YouTube 字幕 |
| `substack` | Substack newsletter |
| `gnews` | Google News 關鍵字搜尋 |
| `ptt` | PTT 版面 / 搜尋 / 單篇 |
| `json` | JSON API endpoint |
| `email` | IMAP 信箱 |
| `apple_podcast` | Apple Podcast（Mac 限定，需本機已下載集數） |

### 選項

| 選項 | 說明 |
|---|---|
| `--format=md` | Markdown 輸出（預設 `json`）|
| `--no-cache` | 不用 cache，強制重新抓取 |
| `-v` | 顯示 debug log |

## Python API

```python
from web_reader import read_url, read_urls, read_gnews, read_ptt

# 自動偵測來源
result = await read_url("https://www.ptt.cc/bbs/Stock/index.html")
print(result.text)

# 批次
results = await read_urls(["https://...", "https://..."])

# Google News 搜尋
results = await read_gnews("台積電", period="7d", max_results=5)
for r in results:
    print(r.title, r.url)

# PTT — 版面列表、搜尋或單篇
result = await read_ptt("https://www.ptt.cc/bbs/Stock/index.html")
result = await read_ptt("https://www.ptt.cc/bbs/Stock/search?q=台積電")
result = await read_ptt("https://www.ptt.cc/bbs/Stock/M.xxx.html")

# 加 cache（預設跨專案共用：~/Library/Caches/web-reader/store.db）
from web_reader import ReadStore
store = ReadStore()
result = await read_url("https://example.com", store=store, cache_ttl=3600)
```

## MCP Server

通用 `stdio` MCP server，不內建任何 domain 特定設定。

```bash
uv run web-reader-mcp
```

### 提供的 tools

| tool | 說明 |
|---|---|
| `read_url` | 讀單一 URL 或 pseudo-URL（如 `gnews://台積電?period=3d&max=5`）|
| `read_config` | 執行 YAML 設定檔，支援 `tags` 過濾 |
| `web_reader_server_info` | 回傳 server metadata |

用法：`read_url(url, output_format="md", no_cache=False)` · `read_config(config_path, tags=None, output_format="md")`

如果有 domain 特定的工作流程（例如交易 agent），建議在外層 repo 包一層固定的 config/tags，不要把 domain logic 寫進 `hello-web-reader` 本體。

## 授權

MIT — 詳見 [LICENSE](LICENSE)。
