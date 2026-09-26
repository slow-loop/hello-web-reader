/**
 * PTT (ptt.cc) — search a board, browse the index, fetch chosen threads.
 *
 * THE SHAPE
 * Same two decoupled stages as opencli_ndltd.js. Searching costs one page load
 * per results page and covers everything it lists, so it runs whenever you
 * want to look. Fetching a thread costs another page load, so it runs only on
 * the ones actually picked.
 *
 *   --list (default)    read the local index, print it. Never touches the network.
 *   --search <query>    search one board, snapshot the results.
 *   --fetch <ref> [...] read those threads, save them as Markdown.
 *
 * WHY THE OPENCLI browser ADAPTER
 * OpenCLI has no PTT adapter (checked 2026-09-26: `opencli list` has nothing
 * for ptt). This drives the generic `browser` subcommand against a named,
 * persistent session, so search -> read happens in ONE browser that stays open
 * — cookies, login and the over-18 acknowledgement carry over between runs.
 * The window is a real, visible (headed) Chrome window: `open` is run without
 * --window, so OpenCLI's default (foreground) applies. Set OPENCLI_WINDOW=background
 * to keep it from grabbing focus.
 * (src/web_reader/readers/ptt.py is the stateless httpx reader; use that when
 * you only need one page and no session.)
 *
 * A THREAD REF is either a full https://www.ptt.cc/bbs/<Board>/M.<ts>.A.<x>.html
 * URL or the short form <Board>/M.<ts>.A.<x> that --list prints.
 *
 * DEPENDS ON
 * - OpenCLI installed at ~/.opencli (an npm package, run with node — not uvx),
 *   daemon + browser bridge connected
 *   (check: node ~/.opencli/node_modules/@jackwener/opencli/dist/src/main.js doctor)
 * - Nothing else: PTT has no CAPTCHA. The script opens the `ptt` session itself
 *   the first time, and — like opencli_ndltd.js — never closes it. To bind it
 *   to a Chrome tab you are already logged in on, use `browser ptt bind`.
 *
 * LAYOUT (under output/ptt/)
 *   _index/<timestamp>.json      one snapshot per --search run; write-once
 *   <board>/<id>-<slug>.md       one fetched thread (frontmatter + body + pushes)
 *
 * USAGE
 *   node scripts/opencli_ptt.js                              # print the index
 *   node scripts/opencli_ptt.js --list-all
 *   node scripts/opencli_ptt.js --search 台積電 [--board Stock] [--pages 2]
 *   node scripts/opencli_ptt.js --fetch Stock/M.1790307232.A.252 [<ref>...] [--force]
 */
const fs = require('fs');
const path = require('path');
const { spawnSync } = require('child_process');
const { parseArgs } = require('node:util');

const OPENCLI_BIN = path.join(
  process.env.HOME,
  '.opencli/node_modules/@jackwener/opencli/dist/src/main.js',
);
const BASE_DIR = path.join(__dirname, '..', 'output', 'ptt');
const INDEX_DIR = path.join(BASE_DIR, '_index');
const BROWSER_SESSION = 'ptt';
const HOST = 'www.ptt.cc';
const DEFAULT_BOARD = 'Stock';
const DEFAULT_LIST_LIMIT = 80;
// An owned OpenCLI browser session is garbage-collected after 10 idle minutes
// (the tab resets to about:blank). Ask for a day instead — value is in seconds.
// A session made with `browser ptt bind` has no idle timer at all.
const IDLE_TIMEOUT_SECONDS = '86400';
const THREAD_ID_RE = /M\.\d+\.A\.[0-9A-Fa-f]+/;

// ---------------------------------------------------------------- primitives

function log(message) {
  process.stderr.write(`[${new Date().toISOString()}] ${message}\n`);
}

function opencli(args) {
  return spawnSync('node', [OPENCLI_BIN, ...args], {
    encoding: 'utf8',
    maxBuffer: 1024 * 1024 * 64,
    env: { OPENCLI_BROWSER_IDLE_TIMEOUT: IDLE_TIMEOUT_SECONDS, ...process.env },
  });
}

// `browser eval` only JSON-encodes an object/array result, so every eval below
// returns an object. stdout also carries an "Update available" banner on some
// runs — parse from the first brace.
function browserEval(js) {
  const res = opencli(['browser', BROWSER_SESSION, 'eval', js]);
  if (res.status !== 0) {
    throw new Error(`browser eval failed: ${(res.stderr || res.stdout || '').trim()}`);
  }
  const start = res.stdout.search(/[{[]/);
  try {
    return JSON.parse(res.stdout.slice(start));
  } catch {
    throw new Error(`browser eval returned invalid JSON: ${res.stdout.slice(0, 500)}`);
  }
}

function browserWait(seconds) {
  opencli(['browser', BROWSER_SESSION, 'wait', 'time', String(seconds)]);
}

function browserOpen(url) {
  const res = opencli(['browser', BROWSER_SESSION, 'open', url]);
  if (res.status !== 0) {
    throw new Error(`could not open ${url}: ${(res.stderr || '').trim()}`);
  }
  browserWait(1.5); // also the politeness gap between PTT requests
  passOver18(url);
}

/**
 * Some boards (and a fresh browser profile) bounce to /ask/over18 first. The
 * page is a form with a "yes" button; clicking it sets the over18 cookie in
 * this persistent session, so it only ever happens once.
 */
function passOver18(url) {
  const state = browserEval('(() => ({ url: location.href }))()');
  if (!/\/ask\/over18/.test(state.url)) return;
  log('over-18 gate — acknowledging it');
  browserEval(`(() => { document.querySelector('button[name="yes"]')?.click(); return {}; })()`);
  browserWait(1.5);
  const res = opencli(['browser', BROWSER_SESSION, 'open', url]);
  if (res.status !== 0) throw new Error(`could not re-open ${url}: ${(res.stderr || '').trim()}`);
  browserWait(1.5);
}

function readJson(filePath, fallback) {
  try {
    return JSON.parse(fs.readFileSync(filePath, 'utf8'));
  } catch {
    return fallback;
  }
}

function toText(value) {
  return value === undefined || value === null ? '' : String(value).trim();
}

function stamp() {
  return new Date().toISOString().slice(0, 19).replace(/[-:]/g, '');
}

/** Filename-safe, readable, short — same rule as opencli_ndltd.js. */
function slugify(title) {
  const cleaned = toText(title)
    .replace(/[^\p{L}\p{N}]+/gu, '-')
    .replace(/^-+|-+$/g, '');
  if (!cleaned) return 'untitled';
  return cleaned.length <= 60 ? cleaned : cleaned.slice(0, 60).replace(/-+$/, '');
}

/** "Stock/M.1.A.2" or a full URL -> { board, id, url }. */
function parseRef(ref) {
  const id = THREAD_ID_RE.exec(ref)?.[0];
  const board = /\/bbs\/([^/]+)\//.exec(ref)?.[1] || /^([^/]+)\//.exec(ref)?.[1];
  if (!id || !board) throw new Error(`not a PTT thread ref: ${ref} (want <Board>/M.<ts>.A.<x> or a full URL)`);
  return { board, id, url: `https://${HOST}/bbs/${board}/${id}.html` };
}

// ------------------------------------------------------------------ search

const SEARCH_ROWS_JS = `(() => {
  const rows = [...document.querySelectorAll('div.r-ent')].map((entry) => {
    const a = entry.querySelector('div.title a');
    if (!a) return null; // deleted post: no link
    return {
      title: a.innerText.trim(),
      href: a.getAttribute('href'),
      push: entry.querySelector('div.nrec')?.innerText.trim() || '',
      author: entry.querySelector('div.author')?.innerText.trim() || '',
      date: entry.querySelector('div.date')?.innerText.trim() || '',
    };
  }).filter(Boolean);
  return { rows, hasNext: [...document.querySelectorAll('.btn-group-paging a')]
    .some((a) => a.innerText.includes('上頁') && a.getAttribute('href')) };
})()`;

function cmdSearch(query, board, pages) {
  if (!query) throw new Error('--search needs a query string');
  const known = new Set(mergeIndex().keys());
  const results = [];

  for (let page = 1; page <= pages; page += 1) {
    const url = `https://${HOST}/bbs/${board}/search?page=${page}&q=${encodeURIComponent(query)}`;
    browserOpen(url);
    const { rows, hasNext } = browserEval(SEARCH_ROWS_JS);
    for (const row of rows) {
      const id = THREAD_ID_RE.exec(row.href)?.[0];
      if (id) {
        results.push({
          ref: `${board}/${id}`,
          title: row.title,
          author: row.author,
          date: row.date,
          push: row.push,
        });
      }
    }
    if (!hasNext) break;
  }
  if (results.length === 0) {
    log('search returned zero rows — either no matches, or the result layout changed');
  }

  fs.mkdirSync(INDEX_DIR, { recursive: true });
  const outPath = path.join(INDEX_DIR, `${stamp()}.json`);
  fs.writeFileSync(outPath, JSON.stringify({
    captured_at: new Date().toISOString(),
    board,
    query,
    results,
  }, null, 2));

  const fresh = results.filter((r) => !known.has(r.ref)).length;
  log(`Snapshot: ${path.relative(process.cwd(), outPath)}`);
  process.stdout.write(`${board} "${query}": ${results.length} results, ${fresh} new to the index\n`);
}

/** Fold every snapshot into one view, newest snapshot wins. */
function mergeIndex() {
  if (!fs.existsSync(INDEX_DIR)) return new Map();
  const files = fs.readdirSync(INDEX_DIR).filter((n) => n.endsWith('.json')).sort().reverse();
  const merged = new Map();
  for (const name of files) {
    const snapshot = readJson(path.join(INDEX_DIR, name), null);
    for (const result of snapshot?.results || []) {
      if (!result?.ref || merged.has(result.ref)) continue;
      merged.set(result.ref, { ...result, snapshot: name, query: snapshot.query });
    }
  }
  return merged;
}

// -------------------------------------------------------------------- list

function findFetched({ board, id }) {
  const dirPath = path.join(BASE_DIR, board);
  if (!fs.existsSync(dirPath)) return null;
  const file = fs.readdirSync(dirPath).find((n) => n.startsWith(`${id}-`) && n.endsWith('.md'));
  return file ? path.join(dirPath, file) : null;
}

function cmdList(limitRaw, listAll) {
  const merged = mergeIndex();
  if (merged.size === 0) {
    process.stdout.write('index is empty — run `node scripts/opencli_ptt.js --search <query>` first\n');
    return;
  }
  const limit = listAll ? merged.size : (limitRaw ? Number.parseInt(limitRaw, 10) : DEFAULT_LIST_LIMIT);
  if (!Number.isInteger(limit) || limit < 1) {
    throw new Error(`--list-limit takes a positive integer, got ${JSON.stringify(limitRaw)}`);
  }

  // PTT pushes are a count, "爆", "X1".."X9" or blank — sort the counts, float 爆 to the top.
  const score = (p) => (p === '爆' ? 100 : Number.parseInt(p, 10) || 0);
  const rows = [...merged.values()].sort((a, b) => score(b.push) - score(a.push));
  process.stdout.write(`pool: ${path.relative(process.cwd(), BASE_DIR)}/<board>/<id>-<slug>.md\n\n`);
  for (const row of rows.slice(0, limit)) {
    const mark = findFetched(parseRef(row.ref)) ? '✓' : '·';
    process.stdout.write(
      `${mark} ${row.ref.padEnd(34)} ${toText(row.push).padStart(3)}推 ${toText(row.date).padEnd(5)} `
      + `${toText(row.author).padEnd(12)} ${toText(row.title).slice(0, 50)}\n`,
    );
  }
  const shown = Math.min(limit, rows.length);
  process.stdout.write(
    `\n${rows.length} indexed${shown < rows.length ? ` · showing ${shown} (--list-all for everything)` : ''}\n`,
  );
}

// ------------------------------------------------------------------- fetch

const THREAD_JS = `(() => {
  const main = document.getElementById('main-content');
  if (!main) return { found: false };
  const meta = {};
  for (const row of main.querySelectorAll('.article-metaline, .article-metaline-right')) {
    const key = row.querySelector('.article-meta-tag')?.innerText.trim();
    const val = row.querySelector('.article-meta-value')?.innerText.trim();
    if (key && val) meta[key] = val;
  }
  const pushes = [...main.querySelectorAll('div.push')].map((p) => ({
    tag: p.querySelector('.push-tag')?.innerText.trim() || '',
    user: p.querySelector('.push-userid')?.innerText.trim() || '',
    text: (p.querySelector('.push-content')?.innerText || '').replace(/^\\s*:\\s*/, '').trim(),
    time: p.querySelector('.push-ipdatetime')?.innerText.trim() || '',
  }));
  // Body = main-content minus the meta header lines and the pushes.
  const clone = main.cloneNode(true);
  clone.querySelectorAll('.article-metaline, .article-metaline-right, div.push, .richcontent')
    .forEach((n) => n.remove());
  return { found: true, meta, pushes, body: clone.innerText.replace(/\\n{3,}/g, '\\n\\n').trim() };
})()`;

function generateMarkdown({ board, id, url }, t) {
  const title = t.meta['標題'] || '(untitled)';
  const pushLines = t.pushes.map((p) => `${p.tag} ${p.user}: ${p.text} [${p.time}]`);
  const count = (tag) => t.pushes.filter((p) => p.tag === tag).length;
  return `---
id: ${JSON.stringify(id)}
board: ${JSON.stringify(board)}
url: ${JSON.stringify(url)}
title: ${JSON.stringify(title)}
author: ${JSON.stringify(t.meta['作者'] || '')}
time: ${JSON.stringify(t.meta['時間'] || '')}
pushes: ${JSON.stringify({ up: count('推'), down: count('噓'), neutral: count('→') })}
fetched: ${JSON.stringify(new Date().toISOString().slice(0, 10))}
---

# ${title}

${t.body}
${pushLines.length ? `\n---\n## 推文\n\n${pushLines.join('\n')}\n` : ''}`;
}

function fetchOne(ref, force) {
  const existing = findFetched(ref);
  if (existing && !force) {
    log(`skip ${ref.board}/${ref.id} (already fetched: ${path.relative(BASE_DIR, existing)})`);
    return false;
  }

  browserOpen(ref.url);
  const thread = browserEval(THREAD_JS);
  if (!thread.found) throw new Error(`[${ref.id}] no #main-content on ${ref.url} — deleted post, or layout changed`);

  const dir = path.join(BASE_DIR, ref.board);
  const file = path.join(dir, `${ref.id}-${slugify(thread.meta['標題'])}.md`);
  fs.mkdirSync(dir, { recursive: true });
  if (existing && existing !== file) fs.rmSync(existing);
  fs.writeFileSync(file, generateMarkdown(ref, thread));
  log(`saved ${path.relative(BASE_DIR, file)} (${thread.pushes.length} pushes)`);
  return true;
}

function cmdFetch(refs, force) {
  let done = 0;
  for (const ref of refs) {
    if (fetchOne(parseRef(ref), force)) done += 1;
  }
  process.stdout.write(`fetched ${done} of ${refs.length}\n`);
}

// --------------------------------------------------------------------- main

function main() {
  const { values, positionals } = parseArgs({
    args: process.argv.slice(2),
    options: {
      'list-all': { type: 'boolean', default: false },
      'list-limit': { type: 'string' },
      search: { type: 'string' },
      board: { type: 'string', default: DEFAULT_BOARD },
      pages: { type: 'string', default: '1' },
      fetch: { type: 'boolean', default: false },
      force: { type: 'boolean', default: false },
    },
    allowPositionals: true,
  });

  if (values.search && values.fetch) {
    throw new Error('--search and --fetch are separate runs; pass one or the other');
  }
  if (!values.fetch && positionals.length > 0) {
    throw new Error(`unexpected argument: ${positionals[0]} (thread refs belong to --fetch)`);
  }

  if (values.search) {
    const pages = Number.parseInt(values.pages, 10);
    if (!Number.isInteger(pages) || pages < 1) throw new Error(`--pages takes a positive integer, got ${values.pages}`);
    cmdSearch(values.search, values.board, pages);
    return;
  }
  if (values.fetch) {
    if (positionals.length === 0) throw new Error('--fetch needs at least one thread ref');
    cmdFetch(positionals, values.force);
    return;
  }
  cmdList(values['list-limit'], values['list-all']);
}

try {
  main();
} catch (error) {
  process.stderr.write(`${error.message}\n`);
  process.exitCode = 1;
}
