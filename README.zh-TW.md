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
cp watchlist.example.yaml watchlist.yaml   # 自訂來源
# .env 填入：YOUTUBE_API_KEY、YAHOO_MAIL_USERNAME、YAHOO_MAIL_PASSWORD
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

### YouTube 頻道快照

用 YouTube Data API 列出頻道所有影片與完整 metadata（需要 `YOUTUBE_API_KEY`）。
預設純 API 呼叫、不下載任何東西；每次執行寫出帶時間戳的
`videos_<stamp>.json` 與 `manifest_<stamp>.csv`，重跑不會覆蓋舊快照。

```bash
# 只抓 metadata（預設列整個頻道；--since/--until 選日期窗口，--limit 取最新 N 部）
uv run web-reader channel @example --limit 50

# 加抓封面圖 / 字幕（已存在的自動跳過，中斷可續跑）
uv run web-reader channel @example --limit 50 --thumbnails
uv run web-reader channel @example --limit 50 --subtitles --lang zh-Hant,en

# 對沒有字幕的影片跑本機 ASR 轉錄（慢）
uv run web-reader channel @example --subtitles --transcribe
```

輸出在 `./output/youtube/<handle>/`（字幕進 `subtitles/`、ASR 逐字稿進 `transcripts/`）。

### Watchlist 增量抓取（fetch）

常態性的採集入口：讀一份 watchlist 設定檔，把窗口內的新內容
抓進 `output/` archive，存成帶 frontmatter 的 markdown（佈局契約在
`web_reader.store`）。已入檔的項目永不重抓，窗口重疊是免費的。

- `rss` → 文章全文 → `output/substack/<source_id>/`
- `substack_notes` → Substack Notes（RSS feed 不帶，要另外抓）→ `output/substack/<source_id>/`
- `rss_podcast` → 下載音檔 + 本機 ASR → `output/podcast/<id>/`（音檔留在 `audio/`）
- `youtube_channel` → 抓字幕，無字幕自動 ASR fallback →
  `output/youtube/<handle>/{subtitles,transcripts}/`

```bash
uv run web-reader fetch path/to/watchlist.yaml   # 預設抓最近 24h
uv run web-reader fetch path/to/watchlist.yaml --since -7d --dry-run
uv run web-reader fetch path/to/watchlist.yaml --source some-id --limit 10
```

watchlist 路徑由呼叫端明確傳入，任何 repo 都能指定自己的清單；輸出一律
落在本 repo 的 `output/`。`--limit` 限制每個來源的「新」昂貴抓取次數
（ASR / yt-dlp）；`--dry-run` 先估算這批的成本（worst-case ASR 時數）再決定。

### YAML 設定檔（批次抓取）

```bash
uv run web-reader watchlist.yaml
uv run web-reader watchlist.yaml --tags=finance
uv run web-reader watchlist.yaml --format=md --no-cache
```

完整設定範本請參考 [`watchlist.example.yaml`](watchlist.example.yaml)。

支援的 `reader` 值：

| reader | 說明 |
|---|---|
| `web` | 一般網頁 |
| `rss` | RSS / Atom feed |
| `reddit` | Reddit 版面或討論串 |
| `youtube` | YouTube 字幕 |
| `substack` | Substack newsletter |
| `substack_notes` | 某個 publication 的 Substack Notes（跟長文是兩條不同的流）|
| `gnews` | Google News 關鍵字搜尋 |
| `ptt` | PTT 版面 / 搜尋 / 單篇 |
| `json` | JSON API endpoint |
| `email` | IMAP 信箱 |
| `apple_podcast` | Apple Podcast（Mac 限定，需本機已下載集數） |
| `rss_podcast` | Podcast RSS feed——下載音檔、本機 SenseVoice ASR 轉錄 |

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

# 加 cache（預設跨專案共用：~/Library/Caches/web-reader/cache.db）
from web_reader import ReadCache
cache = ReadCache()
result = await read_url("https://example.com", store=cache, cache_ttl=3600)
```

## OpenCLI 腳本

### Twitter／X 帳號貼文

`scripts/opencli_twitter.js` 重用本機 OpenCLI 的 BrowserBridge 與 Twitter 原生
分頁／媒體解析工具。多帳號逐一抓取，共用一個 **headed、可見的 Chrome** adapter
session（`site:twitter`，`foreground`、`persistent`、閒置保留 24 小時），結束或失敗
都不關閉。切換帳號時記錄並檢查分頁 ID；通用 `browser` 指令是不同 surface，
不能只靠相同 session 名稱判定分頁相同。

```bash
node scripts/opencli_twitter.js --index --accounts dotey,godofprompt --limit 20
node scripts/opencli_twitter.js --list                         # 離線看清單
node scripts/opencli_twitter.js --fetch <推文ID> [<推文ID>...]  # 原文＋圖片
node scripts/opencli_twitter.js --fetch <推文ID> --video        # 加下載影片／GIF
node --test tests/test_opencli_twitter.cjs
```

- `--limit` 是每帳號原始動態的上限：預設 20、最多 100，最多讀三頁；不捲動、
  不為湊足篇數補抓。排除純轉推、回覆與置頂，保留原創及引用貼文。
- 索引與 API 原始回應存 `output/twitter/_index/`；選中的貼文存
  `output/twitter/<帳號>/<推文ID>/post.md`，附圖放同目錄 `assets/`。
  原文不改寫，引用原文／圖片標示來源，不抓留言區。
- 圖片逐張下載；影片／GIF 預設保留預覽圖與來源網址，`--video` 才下載
  OpenCLI 解析出的 MP4，不保證最高畫質。
- 保存外連與展開網址，必要時以 HEAD 解析 t.co；不自動抓外部文章全文。
  全文另用 `uv run web-reader read <目的網址>`，X Article 另用原生 `twitter article`。
- 分頁間隨機停 5–8 秒；帳號／貼文存檔間停 30–90 秒，每五次停 3–5 分鐘；
  圖片與短網址請求間停 1–3 秒。冷卻時間跨次執行保留，腳本鎖避免重複啟動。
  其他 OpenCLI 工作仍需自行避免同時操作同一瀏覽器。
- 登入／驗證／API 錯誤立即停整批；媒體 401／403／429 也停止。
  其他附件／連結失敗記錄為部分完成（exit 3），下次 `--fetch` 補抓。
  文字先落檔，完整存過的略過。OpenCLI 升級後需重新做小批實測。

### 其他共用慣例

[`scripts/`](scripts/) 裡的 `opencli_*.js`（`opencli_ptt.js`、`opencli_ndltd.js`、`opencli_rednote_liked.js`）不屬於 `web-reader` CLI，
用 OpenCLI 驅動真實瀏覽器，共同約定：

- Agent 只負責執行腳本；腳本操作一個固定名稱的 OpenCLI browser session（`ptt`、`ndltd`、`rednote-liked`），是可見的 Chrome 視窗。
- 腳本**不會關閉** session，並要求閒置 24 小時才回收（OpenCLI 預設 10 分鐘），所以「先搜尋、再閱讀」在同一個頁面、同一份 cookie 與登入狀態下連續進行。
- 出問題時直接看現場，用同名 session 讀取即可：

```bash
oc() { node "$HOME/.opencli/node_modules/@jackwener/opencli/dist/src/main.js" "$@"; }
oc browser ptt state                # 網址、標題、可點元素
oc browser ptt screenshot out.png   # 視窗畫面
oc browser ptt eval '(() => ({ title: document.title }))()'   # 讀頁面任何內容
oc browser ptt close                # 真的想關掉才用
```

OpenCLI 是 npm 套件（`~/.opencli`，用 `node` 執行），不是 `uvx` 工具。

PTT 是純 HTML、免登入無驗證碼，單頁用 `web-reader <ptt 網址>` 就夠；`opencli_ptt.js` 用在「搜尋 → 挑選 → 閱讀」的連續流程：

```bash
node scripts/opencli_ptt.js                                        # 印出索引（預設，離線）
node scripts/opencli_ptt.js --search <關鍵字> [--board Stock] [--pages 2]
node scripts/opencli_ptt.js --fetch <Board>/M.<ts>.A.<x> [...]     # 文章 + 推文，已抓過的略過
```

輸出在 `output/ptt/<board>/<id>-<slug>.md`。

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
