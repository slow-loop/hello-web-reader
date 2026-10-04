#!/usr/bin/env node
/**
 * Rednote Interactive Search & Curation CLI
 *
 * Implements the full Rednote media extraction and anti-risk safety protocol:
 * - Keeps a single live Chrome tab open (`--site-session persistent` or browser session).
 * - Decouples search/indexing from targeted fetching.
 * - Downloads BOTH note content, comments, AND all image cards / video media.
 * - Safety pacing: random 30-90s between notes, 3-5 min rest every 5th note.
 * - Immediate stop on risk-control or login-wall flags.
 *
 * Usage:
 *   node scripts/opencli_rednote_search.js --search "拆解一個帳號" [--limit 10]
 *   node scripts/opencli_rednote_search.js --list
 *   node scripts/opencli_rednote_search.js --inspect 1
 *   node scripts/opencli_rednote_search.js --fetch 1 2 4
 *   node scripts/opencli_rednote_search.js --current
 */

const fs = require('fs');
const path = require('path');
const { spawnSync } = require('child_process');
const { parseArgs } = require('node:util');
const { Readable } = require('node:stream');
const { pipeline } = require('node:stream/promises');

const OPENCLI_BIN = path.join(
  process.env.HOME,
  '.opencli/node_modules/@jackwener/opencli/dist/src/main.js',
);
const BASE_DIR = path.join(__dirname, '..', 'output', 'rednote', 'search');
const LAST_SEARCH_FILE = path.join(BASE_DIR, '.last_search.json');
const WEB_HOST = 'www.rednote.com';
const BROWSER_SESSION = 'site:rednote';
const IDLE_TIMEOUT_SECONDS = '86400';

const CDN_HEADERS = {
  Referer: `https://${WEB_HOST}/`,
  'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'
    + ' (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
};

function log(msg) {
  process.stderr.write(`[${new Date().toISOString()}] ${msg}\n`);
}

function sleepSeconds(seconds) {
  const waitBuffer = new SharedArrayBuffer(4);
  Atomics.wait(new Int32Array(waitBuffer), 0, 0, Math.round(seconds * 1000));
}

function toText(value) {
  return typeof value === 'string' ? value.trim() : value == null ? '' : String(value).trim();
}

function formatDate(ms) {
  if (!Number.isFinite(ms) || ms <= 0) return '';
  return new Date(ms).toISOString().slice(0, 10);
}

function sanitizeDirName(name) {
  return name.replace(/[\\/:*?"<>|]/g, '_').trim();
}

function parseNoteId(rawUrl) {
  const m = /\/(?:search_result|explore|note)\/([a-zA-Z0-9]+)/.exec(rawUrl);
  return m ? m[1] : 'unknown';
}

function padDisplay(text, width) {
  let used = 0;
  let out = '';
  for (const ch of String(text || '')) {
    const w = /[ᄀ-ᅟ⺀-꓏가-힣豈-﫿︰-﹯＀-｠￠-￦]/.test(ch) ? 2 : 1;
    if (used + w > width) return `${out}…`;
    out += ch;
    used += w;
  }
  return out + ' '.repeat(Math.max(0, width - used));
}

function opencli(args) {
  return spawnSync('node', [OPENCLI_BIN, ...args], {
    encoding: 'utf8',
    maxBuffer: 1024 * 1024 * 64,
    env: { OPENCLI_BROWSER_IDLE_TIMEOUT: IDLE_TIMEOUT_SECONDS, ...process.env },
  });
}

function opencliJson(args, label) {
  const res = opencli(args);
  if (res.status !== 0) {
    throw new Error(`${label} failed: ${(res.stderr || res.stdout || '').trim()}`);
  }
  try {
    return JSON.parse(res.stdout);
  } catch (e) {
    throw new Error(`${label} returned invalid JSON: ${(res.stdout || '').slice(0, 500)}`);
  }
}

function browserEval(js) {
  return opencliJson(['browser', BROWSER_SESSION, 'eval', js], 'browser eval');
}

function browserWait(seconds) {
  opencli(['browser', BROWSER_SESSION, 'wait', 'time', String(seconds)]);
}

function loadLastSearch() {
  if (fs.existsSync(LAST_SEARCH_FILE)) {
    try {
      return JSON.parse(fs.readFileSync(LAST_SEARCH_FILE, 'utf8'));
    } catch (_) {}
  }
  return null;
}

function saveLastSearch(data) {
  fs.mkdirSync(BASE_DIR, { recursive: true });
  fs.writeFileSync(LAST_SEARCH_FILE, JSON.stringify(data, null, 2), 'utf8');
}

// ---------------------------------------------------------------- media download

async function cdnGet(url, timeoutMs = 120000) {
  const res = await fetch(url, { headers: CDN_HEADERS, signal: AbortSignal.timeout(timeoutMs) });
  if (!res.ok) throw new Error(`${url.slice(0, 60)}… responded ${res.status}`);
  return res;
}

function pickImageUrl(image) {
  const info = Array.isArray(image?.infoList) ? image.infoList : [];
  const preferred = info.find((item) => item?.imageScene === 'WB_DFT');
  return toText(preferred?.url || image?.urlDefault || image?.url || info[0]?.url);
}

function extensionFor(contentType, url) {
  const type = toText(contentType).toLowerCase();
  if (type.includes('webp')) return '.webp';
  if (type.includes('png')) return '.png';
  if (type.includes('gif')) return '.gif';
  if (type.includes('jpeg') || type.includes('jpg')) return '.jpg';
  const match = /\.(jpe?g|png|webp|gif)(?:[?#]|$)/i.exec(url);
  return match ? `.${match[1].toLowerCase().replace('jpeg', 'jpg')}` : '.jpg';
}

function pickVideoStream(note, preferLow) {
  const stream = note?.video?.media?.stream;
  if (!stream || typeof stream !== 'object') return null;
  const all = Object.values(stream).flat().filter((item) => item && item.masterUrl);
  if (all.length === 0) return null;
  if (preferLow) return all.slice().sort((a, b) => (a.size || 0) - (b.size || 0))[0];
  return all.slice().sort((a, b) => (
    (b.width * b.height) - (a.width * a.height) || (a.size || 0) - (b.size || 0)
  ))[0];
}

async function downloadStream(urls, destPath) {
  let lastError = null;
  for (const url of urls) {
    try {
      const res = await cdnGet(url);
      await pipeline(Readable.fromWeb(res.body), fs.createWriteStream(destPath));
      return;
    } catch (error) {
      lastError = error;
    }
  }
  throw lastError || new Error('no usable stream URL');
}

async function downloadMedia(note, noteDir, noteId, preferLow = false) {
  const saved = [];
  let index = 0;

  if (toText(note.type) === 'video') {
    const stream = pickVideoStream(note, preferLow);
    if (stream) {
      index += 1;
      const fileName = `${noteId}_${index}.mp4`;
      log(`  Downloading video ${stream.width}x${stream.height} ~${((stream.size || 0) / 1048576).toFixed(1)}MB...`);
      await downloadStream([stream.masterUrl, ...(stream.backupUrls || [])].filter(Boolean),
        path.join(noteDir, fileName));
      saved.push(fileName);
    }
  }

  const imageList = note.imageList || [];
  if (imageList.length > 0) {
    log(`  Downloading ${imageList.length} image card(s)...`);
  }
  for (const image of imageList) {
    const url = pickImageUrl(image);
    if (!url) continue;
    try {
      const res = await cdnGet(url, 60000);
      index += 1;
      const fileName = `${noteId}_${index}${extensionFor(res.headers.get('content-type'), url)}`;
      fs.writeFileSync(path.join(noteDir, fileName), Buffer.from(await res.arrayBuffer()));
      saved.push(fileName);
    } catch (err) {
      log(`  WARN: Failed to download image ${index + 1}: ${err.message}`);
    }
  }
  return saved;
}

function renderComments(comments) {
  const list = Array.isArray(comments) ? comments : [];
  if (list.length === 0) return '(无评论 / 未抓到评论)';
  const lines = [];
  list.forEach((comment, index) => {
    const author = toText(comment.userInfo?.nickname || comment.author);
    const when = formatDate(Number(comment.createTime)) || toText(comment.time);
    const likes = toText(comment.likeCount ?? comment.likes ?? 0);
    lines.push(`${index + 1}. **${author}**（${likes}赞，${when}）：${toText(comment.content || comment.text)}`);
    for (const sub of comment.subComments || []) {
      const target = toText(sub.targetComment?.userInfo?.nickname);
      lines.push(`   ↳ 回复${target ? ` @${target}` : ''}：${toText(sub.content || sub.text)}`);
    }
  });
  return lines.join('\n');
}

function generateMarkdown(url, detail, mediaFiles) {
  const note = detail?.note || {};
  const interact = note.interactInfo || {};
  const tags = (note.tagList || []).map((tag) => `#${toText(tag.name)}`).join(' ');
  const media = mediaFiles.length
    ? mediaFiles.map((fileName, index) => (
      /\.(mp4|mov)$/i.test(fileName)
        ? `[视频${index + 1}](./${fileName})`
        : `![图${index + 1}](./${fileName})`
    )).join('\n')
    : '(无媒体文件)';

  return `# ${toText(note.title) || '(无标题)'}

- **作者**：${toText(note.user?.nickname)}
- **數據**：赞 ${toText(interact.likedCount)} · 收藏 ${toText(interact.collectedCount)} · 评论 ${toText(interact.commentCount)} · 转发 ${toText(interact.shareCount)}
- **发布**：${formatDate(Number(note.time))} · **类型**：${toText(note.type)}
- **标签**：${tags}
- **原文链接**：${url}

## 媒体 (共 ${mediaFiles.length} 个)
${media}

## 正文
${toText(note.desc) || '(正文为空，完整内容请查看图文卡片媒体)'}

## 热门评论
${renderComments(detail?.comments?.list || detail?.comments)}
`;
}

// ---------------------------------------------------------------- note extraction

function buildNoteExtractJs(noteId) {
  return `(() => {
    const bodyText = document.body?.innerText || '';
    const securityBlock = /安全限制|访问链接异常|Security Verification/.test(bodyText)
      || /website-login\\/error|error_code=300017|error_code=300031/.test(location.href);
    const loginWall = /登录后查看|请登录/.test(bodyText);
    const notFound = /页面不见了|笔记不存在|无法浏览/.test(bodyText);
    const nd = window.__INITIAL_STATE__?.note;
    const rawMap = nd?.noteDetailMap?._value ?? nd?.noteDetailMap ?? null;
    const map = rawMap && typeof rawMap === 'object' ? rawMap : null;
    const picked = map ? (map[${JSON.stringify(noteId)}] ?? map[Object.keys(map)[0]]) : null;
    return {
      securityBlock,
      loginWall,
      notFound,
      pageUrl: location.href,
      detail: picked ? JSON.parse(JSON.stringify(picked)) : null,
    };
  })()`;
}

async function fetchOneNote(targetUrl, targetDir, prefix = '') {
  const noteId = parseNoteId(targetUrl);
  const noteFolder = path.join(targetDir, prefix ? `${prefix}_${noteId}` : noteId);
  fs.mkdirSync(noteFolder, { recursive: true });

  log(`Navigating to note [${noteId}] in live browser...`);
  const openRes = opencli(['browser', BROWSER_SESSION, 'open', targetUrl]);
  if (openRes.status !== 0) {
    throw new Error(`Could not open [${noteId}]: ${(openRes.stderr || '').trim()}`);
  }

  // Initial wait for page store hydration
  browserWait(2 + Math.random() * 2);

  const extractJs = buildNoteExtractJs(noteId);
  const page = browserEval(extractJs);

  if (page.securityBlock) {
    throw new Error(`STOPPED: Risk control detected on [${noteId}]! Aborting to protect account.`);
  }
  if (page.loginWall) {
    throw new Error(`STOPPED: Login wall detected on [${noteId}] — please verify login in Chrome.`);
  }
  if (page.notFound) {
    log(`WARN: Note [${noteId}] not found (deleted or restricted)`);
    return null;
  }
  if (!page.detail?.note) {
    log(`WARN: [${noteId}] note store was empty; attempting opencli rednote note fallback`);
  }

  const detail = page.detail || {};
  const note = detail.note || {};

  // Download media (images + video)
  let mediaFiles = [];
  try {
    mediaFiles = await downloadMedia(note, noteFolder, noteId);
  } catch (err) {
    log(`WARN [${noteId}] media download error: ${err.message}`);
  }

  // Save note.json & note.md
  fs.writeFileSync(path.join(noteFolder, 'note.json'), JSON.stringify(detail, null, 2), 'utf8');
  fs.writeFileSync(path.join(noteFolder, 'note.md'), generateMarkdown(targetUrl, detail, mediaFiles), 'utf8');

  log(`Saved note to: ${path.relative(process.cwd(), noteFolder)}/ (images=${mediaFiles.length})`);
  return { noteFolder, mediaFiles, title: note.title };
}

function printCandidatesTable(results, query) {
  process.stdout.write(`\n=== 搜尋候選列表：${query} (${results.length} 篇) ===\n\n`);
  process.stdout.write(`  #   讚數    作者             標題\n`);
  process.stdout.write(`────────────────────────────────────────────────────────────────────────────\n`);
  results.forEach((item, idx) => {
    const rank = String(item.rank || idx + 1).padStart(2);
    const likes = String(item.likes || 0).padStart(6);
    const author = padDisplay(item.author || '(未知)', 16);
    const title = padDisplay(item.title || '(無標題)', 44);
    process.stdout.write(` [${rank}] ${likes}  ${author} ${title}\n`);
  });
  process.stdout.write(`────────────────────────────────────────────────────────────────────────────\n`);
  process.stdout.write(`操作提示：\n`);
  process.stdout.write(`  - 深度視察：node scripts/opencli_rednote_search.js --inspect <編號>\n`);
  process.stdout.write(`  - 抓取選定 (文+圖)：node scripts/opencli_rednote_search.js --fetch <編號1> <編號2>...\n\n`);
}

// ---------------------------------------------------------------- main

async function main() {
  const args = process.argv.slice(2);
  const { values, positionals } = parseArgs({
    args,
    options: {
      search: { type: 'string', short: 's' },
      list: { type: 'boolean', short: 'l' },
      inspect: { type: 'string', short: 'i' },
      fetch: { type: 'boolean', short: 'f' },
      current: { type: 'boolean', short: 'c' },
      limit: { type: 'string', default: '10' },
      'min-delay': { type: 'string', default: '30' },
      'max-delay': { type: 'string', default: '70' },
      help: { type: 'boolean', short: 'h' },
    },
    allowPositionals: true,
  });

  if (values.help || args.length === 0) {
    process.stdout.write(`
使用方式:
  1. 搜尋並取得候選清單 (瀏覽器保持開啟):
     node scripts/opencli_rednote_search.js --search "關鍵字" [--limit 10]

  2. 查看上次搜尋結果:
     node scripts/opencli_rednote_search.js --list

  3. 深入視察某一篇內容 (僅預覽，不落盤):
     node scripts/opencli_rednote_search.js --inspect <編號 1..N 或 網址>

  4. 抓取選定的一篇或多篇 (文 + 圖卡片 + 留言，嚴格安全間隔 30~90s):
     node scripts/opencli_rednote_search.js --fetch 1 2 4

  5. 直接抓取瀏覽器當前分頁筆記:
     node scripts/opencli_rednote_search.js --current
\n`);
    return;
  }

  // 1. Current tab fetch
  if (values.current) {
    log('Binding to currently active Chrome tab...');
    const bindRes = opencliJson(['browser', BROWSER_SESSION, 'bind'], 'browser bind');
    const currentUrl = bindRes.url;
    if (!currentUrl || !currentUrl.includes('rednote.com')) {
      throw new Error(`Current active tab is not on rednote.com (URL: ${currentUrl})`);
    }
    const currentDir = path.join(BASE_DIR, '_current');
    log(`Current active tab: ${currentUrl} (${bindRes.title})`);
    await fetchOneNote(currentUrl, currentDir, 'current');
    return;
  }

  // 2. Search
  if (values.search) {
    const query = values.search;
    const limit = Math.max(1, parseInt(values.limit, 10) || 10);
    const targetDir = path.join(BASE_DIR, sanitizeDirName(query));
    fs.mkdirSync(targetDir, { recursive: true });

    log(`Searching "${query}" (limit=${limit}) via persistent Chrome tab...`);
    const results = opencliJson(
      ['rednote', 'search', query, '--limit', String(limit), '--site-session', 'persistent', '-f', 'json'],
      'rednote search',
    );

    saveLastSearch({ query, results, targetDir, timestamp: new Date().toISOString() });
    fs.writeFileSync(path.join(targetDir, 'search_results.json'), JSON.stringify(results, null, 2), 'utf8');

    printCandidatesTable(results, query);
    return;
  }

  // 3. List
  if (values.list) {
    const last = loadLastSearch();
    if (!last || !last.results) {
      process.stdout.write('尚未有搜尋紀錄，請先執行 --search <關鍵字>\n');
      return;
    }
    printCandidatesTable(last.results, last.query);
    return;
  }

  // 4. Inspect
  if (values.inspect) {
    const last = loadLastSearch();
    let targetUrl = values.inspect;
    let targetTitle = '';

    const idx = parseInt(values.inspect, 10);
    if (!isNaN(idx) && last?.results?.[idx - 1]) {
      targetUrl = last.results[idx - 1].url;
      targetTitle = last.results[idx - 1].title;
    }

    log(`Inspecting "${targetTitle || targetUrl}" in live tab...`);
    const noteId = parseNoteId(targetUrl);
    opencli(['browser', BROWSER_SESSION, 'open', targetUrl]);
    browserWait(2 + Math.random() * 2);

    const page = browserEval(buildNoteExtractJs(noteId));
    const note = page?.detail?.note || {};
    const interact = note.interactInfo || {};

    process.stdout.write(`\n=== 筆記視察預覽 ===\n`);
    process.stdout.write(`標題：${note.title || targetTitle}\n`);
    process.stdout.write(`作者：${note.user?.nickname || '(未知)'}\n`);
    process.stdout.write(`數據：讚 ${interact.likedCount || 0} · 收藏 ${interact.collectedCount || 0} · 留言 ${interact.commentCount || 0}\n`);
    process.stdout.write(`圖片卡片：${note.imageList?.length || 0} 張\n`);
    process.stdout.write(`內文摘要：\n${(note.desc || '').slice(0, 300)}...\n`);
    process.stdout.write(`===================\n\n`);
    return;
  }

  // 5. Fetch selected
  if (values.fetch || positionals.length > 0) {
    const last = loadLastSearch();
    if (!last || !last.results) {
      throw new Error('請先執行 --search 取得候選清單，或指定完整網址');
    }

    const targets = positionals.length > 0 ? positionals : [];
    if (targets.length === 0) {
      throw new Error('請指定要抓取的候選編號，例如: --fetch 1 2 4');
    }

    const minDelay = Math.max(10, parseInt(values['min-delay'], 10) || 30);
    const maxDelay = Math.max(minDelay, parseInt(values['max-delay'], 10) || 70);

    log(`Starting safe sequential fetch for ${targets.length} notes (with image cards)...`);

    for (let i = 0; i < targets.length; i++) {
      const t = targets[i];
      let url = t;
      let rankPrefix = '';

      const idx = parseInt(t, 10);
      if (!isNaN(idx) && last.results[idx - 1]) {
        url = last.results[idx - 1].url;
        rankPrefix = String(idx).padStart(2, '0');
      }

      await fetchOneNote(url, last.targetDir, rankPrefix);

      // Safety sleep policy: 30~90s between notes, 3~5 mins every 5th note
      if (i < targets.length - 1) {
        const isFifth = (i + 1) % 5 === 0;
        const delay = isFifth
          ? 180 + Math.random() * 120 // 3-5 min long break
          : minDelay + Math.random() * (maxDelay - minDelay); // 30-70s regular break

        log(`[SAFETY] Resting ${Math.round(delay)}s${isFifth ? ' (5th note long break)' : ''} before next note...`);
        sleepSeconds(delay);
      }
    }

    log(`\nFetch completed! Files saved in: ${last.targetDir}`);
  }
}

main().catch((err) => {
  console.error(`Error: ${err.message}`);
  process.exit(1);
});
