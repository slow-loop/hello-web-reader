#!/usr/bin/env node
/**
 * Rednote Interactive Search & Curation CLI
 *
 * Designed for human + agent co-piloting:
 * - Keeps a single live Chrome tab open (`--site-session persistent`).
 * - Decouples search/indexing from targeted fetching.
 * - Allows agent or human to inspect candidates before fetching.
 * - Supports fetching the currently opened note in Chrome (`--current`).
 *
 * Usage:
 *   node scripts/opencli_rednote_search.js --search "拆解一個帳號" [--limit 10]
 *   node scripts/opencli_rednote_search.js --list ["拆解一個帳號"]
 *   node scripts/opencli_rednote_search.js --inspect 1
 *   node scripts/opencli_rednote_search.js --fetch 1 2 4
 *   node scripts/opencli_rednote_search.js --current
 */

const fs = require('fs');
const path = require('path');
const { spawnSync } = require('child_process');
const { parseArgs } = require('node:util');

const OPENCLI_BIN = path.join(
  process.env.HOME,
  '.opencli/node_modules/@jackwener/opencli/dist/src/main.js',
);
const BASE_DIR = path.join(__dirname, '..', 'output', 'rednote', 'search');
const LAST_SEARCH_FILE = path.join(BASE_DIR, '.last_search.json');

function log(msg) {
  process.stderr.write(`[${new Date().toISOString()}] ${msg}\n`);
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function opencli(args) {
  return spawnSync('node', [OPENCLI_BIN, ...args], {
    encoding: 'utf8',
    maxBuffer: 1024 * 1024 * 64,
    env: { ...process.env },
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

function sanitizeDirName(name) {
  return name.replace(/[\\/:*?"<>|]/g, '_').trim();
}

function parseNoteId(rawUrl) {
  const m = /\/(?:search_result|explore)\/([a-zA-Z0-9]+)/.exec(rawUrl);
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
  process.stdout.write(`  - 抓取選定：node scripts/opencli_rednote_search.js --fetch <編號1> <編號2>...\n\n`);
}

async function fetchSingleNote(targetUrl, targetDir, prefixHint = '') {
  const noteId = parseNoteId(targetUrl);
  const prefix = prefixHint ? `${prefixHint}_${noteId}` : noteId;

  log(`Fetching note (${noteId}): ${targetUrl}...`);

  let noteFields = [];
  try {
    noteFields = opencliJson(
      ['rednote', 'note', targetUrl, '--site-session', 'persistent', '-f', 'json'],
      `rednote note ${noteId}`,
    );
  } catch (err) {
    log(`  Error reading note: ${err.message}`);
    return null;
  }

  const noteMap = {};
  if (Array.isArray(noteFields)) {
    for (const field of noteFields) {
      if (field.field) noteMap[field.field] = field.value;
    }
  }

  await sleep(1500);
  let comments = [];
  try {
    log(`  Fetching comments for note ${noteId}...`);
    comments = opencliJson(
      ['rednote', 'comments', targetUrl, '--site-session', 'persistent', '-f', 'json'],
      `rednote comments ${noteId}`,
    );
  } catch (err) {
    log(`  Comments could not be fetched: ${err.message}`);
  }

  const fullData = {
    note_id: noteId,
    url: targetUrl,
    note: noteMap,
    comments: comments,
    fetched_at: new Date().toISOString(),
  };

  fs.mkdirSync(targetDir, { recursive: true });
  fs.writeFileSync(path.join(targetDir, `${prefix}.json`), JSON.stringify(fullData, null, 2), 'utf8');

  const mdLines = [
    `# ${noteMap.title || '(無標題)'}`,
    '',
    `- **作者**：${noteMap.author || '(未知)'}`,
    `- **互動數據**：讚 ${noteMap.likes || 0} · 收藏 ${noteMap.collects || 0} · 留言 ${noteMap.comments || 0}`,
    `- **原始連結**：${targetUrl}`,
    noteMap.tags ? `- **標籤**：${noteMap.tags}` : '',
    '',
    '## 筆記內容',
    '',
    noteMap.content || '(正文為空或僅包含圖片)',
    '',
  ];

  if (Array.isArray(comments) && comments.length > 0) {
    mdLines.push('## 熱門留言', '');
    for (const c of comments.slice(0, 20)) {
      const replyTag = c.is_reply ? ` (回覆 @${c.reply_to})` : '';
      mdLines.push(`- **${c.author || '匿名'}**${replyTag} [${c.likes || 0}讚 · ${c.time || ''}]: ${c.text || ''}`);
    }
    mdLines.push('');
  }

  const outMd = path.join(targetDir, `${prefix}.md`);
  fs.writeFileSync(outMd, mdLines.filter(Boolean).join('\n'), 'utf8');
  log(`  Saved to ${path.relative(process.cwd(), outMd)}`);
  return fullData;
}

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
      delay: { type: 'string', default: '4' },
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

  3. 深入視察某一篇內容:
     node scripts/opencli_rednote_search.js --inspect <編號 1..N 或 網址>

  4. 抓取選定的一篇或多篇:
     node scripts/opencli_rednote_search.js --fetch 1 2 4

  5. 直接抓取瀏覽器當前分頁筆記:
     node scripts/opencli_rednote_search.js --current
\n`);
    return;
  }

  // 1. Current tab fetch
  if (values.current) {
    log('Binding to currently active Chrome tab...');
    const bindRes = opencliJson(['browser', 'site:rednote', 'bind'], 'browser bind');
    const currentUrl = bindRes.url;
    if (!currentUrl || !currentUrl.includes('rednote.com')) {
      throw new Error(`Current active tab is not on rednote.com (URL: ${currentUrl})`);
    }
    const currentDir = path.join(BASE_DIR, '_current');
    log(`Current active tab is: ${currentUrl} (${bindRes.title})`);
    await fetchSingleNote(currentUrl, currentDir, 'current');
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
    const noteFields = opencliJson(
      ['rednote', 'note', targetUrl, '--site-session', 'persistent', '-f', 'json'],
      'inspect note',
    );

    const noteMap = {};
    for (const f of noteFields) noteMap[f.field] = f.value;

    process.stdout.write(`\n=== 筆記視察預覽 ===\n`);
    process.stdout.write(`標題：${noteMap.title}\n`);
    process.stdout.write(`作者：${noteMap.author}\n`);
    process.stdout.write(`數據：讚 ${noteMap.likes} · 收藏 ${noteMap.collects} · 留言 ${noteMap.comments}\n`);
    process.stdout.write(`內文摘要：\n${(noteMap.content || '').slice(0, 300)}...\n`);
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

    const minDelay = Math.max(1, parseInt(values.delay, 10) || 4);
    log(`Starting sequential fetch for ${targets.length} notes...`);

    for (let i = 0; i < targets.length; i++) {
      const t = targets[i];
      let url = t;
      let rankPrefix = '';

      const idx = parseInt(t, 10);
      if (!isNaN(idx) && last.results[idx - 1]) {
        url = last.results[idx - 1].url;
        rankPrefix = String(idx).padStart(2, '0');
      }

      await fetchSingleNote(url, last.targetDir, rankPrefix);

      if (i < targets.length - 1) {
        const wait = minDelay * 1000 + Math.floor(Math.random() * 2000);
        log(`Sleeping ${(wait / 1000).toFixed(1)}s before next note...`);
        await sleep(wait);
      }
    }

    log(`Fetch completed! Check files in: ${last.targetDir}`);
  }
}

main().catch((err) => {
  console.error(`Error: ${err.message}`);
  process.exit(1);
});
