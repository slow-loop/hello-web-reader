/**
 * NDLTD (臺灣博碩士論文知識加值系統) — search, browse the index, fetch chosen theses.
 *
 * THE SHAPE
 * Same two decoupled stages as opencli_reddit_subs.js / opencli_rednote_liked.js.
 * Searching costs one page load per query and covers everything it returns, so
 * it runs whenever you want to look. Fetching a thesis costs another page load
 * (plus a tab click for each abstract), so it runs only on the ones actually
 * picked.
 *
 *   --list (default)   read the local index, print it. Never touches the network.
 *   --search <query>    run a search, snapshot the results.
 *   --fetch <id> [...]  read those theses, save biblio + abstracts as Markdown.
 *
 * WHY THE OPENCLI browser ADAPTER AND NOT A DEDICATED opencli ndltd COMMAND
 * OpenCLI has no built-in NDLTD adapter (checked 2026-09-22: `opencli list`
 * has nothing for ndltd/thesis/taiwan). The site has no JSON API either
 * (`opencli browser <s> analyze` classifies it Pattern C, HTML-only). So this
 * drives the generic `browser` subcommand — open/fill/click/eval — the same
 * way opencli_rednote_liked.js does, against a named, reusable session.
 *
 * TWO SEPARATE CAPTCHA GATES — do not confuse them
 * - Site-wide gate ("驗證碼檢查機制"): appears on first visit and can
 *   reappear mid-session with no obvious trigger (observed twice in one
 *   session, ~90s apart, with the reported connection IP also changing).
 *   This blocks EVERYTHING — search, browsing, reading a record. There is no
 *   way to tell it apart from a normal page in advance; every step here
 *   checks for it and STOPS THE WHOLE RUN when it shows up.
 * - Download-declaration gate ("下載電子全文宣告"): a second, separate
 *   CAPTCHA + copyright-acknowledgement page that appears when you click
 *   電子全文, even while logged in (verified 2026-09-22, logged in as a real
 *   member). This one is scoped to a single thesis, not the whole site, so a
 *   fetch run does NOT stop for it — it just can't get past it, and says so.
 *
 * WHAT --fetch CAN AND CANNOT GET
 * Automatic: title (zh/en), author (zh/en), advisor(s), committee, school,
 * department, degree, field, year, language, page count, keywords (zh/en),
 * permanent handle URL, and both abstracts (each is a separate tab in a YUI
 * TabView widget — read by finding the one panel under .yui-content that
 * isn't display:none).
 * Not automatic: the PDF itself. Every fetch that has 電子全文 records the
 * fulltextdeclare URL (valid only for the current login session — the ccd in
 * it dies with the session) and the permanent handle URL, and marks
 * fulltext_status accordingly. Finishing the declaration page by hand is the
 * only way past it right now; nobody has yet checked what that page hands you
 * once cleared, so there is no code here for what comes after it.
 *
 * DEPENDS ON
 * - OpenCLI installed at ~/.opencli, daemon + browser bridge connected
 *   (check: node ~/.opencli/node_modules/@jackwener/opencli/dist/src/main.js doctor)
 * - A browser session named `ndltd` (see BROWSER_SESSION) that is already
 *   past the site-wide CAPTCHA and, for full-text attempts, logged in to a
 *   free member account. This script never creates that session or handles a
 *   CAPTCHA itself — both are a human's job, once, in:
 *     node ~/.opencli/node_modules/@jackwener/opencli/dist/src/main.js browser ndltd open https://ndltd.ncl.edu.tw/
 * - Unlike the reddit/rednote scripts, this one never closes that session
 *   when it finishes. It is meant to be a standing, logged-in session you
 *   keep reusing across runs, not a disposable one spun up per run.
 *
 * LAYOUT (under output/ndltd/)
 *   _index/<timestamp>.json      one snapshot per --search run; write-once
 *   <year>/<id>-<slug>.md        one fetched thesis (frontmatter + abstracts)
 *
 * The thesis id (e.g. 100CCU00442070) is the site's own stable identifier —
 * read from the citation ("被引用") link's dbid param, not from the record
 * page's r1=N position, which is only valid within one search's result set.
 *
 * USAGE
 *   node scripts/opencli_ndltd.js                          # print the index
 *   node scripts/opencli_ndltd.js --list-all
 *   node scripts/opencli_ndltd.js --search 深度學習
 *   node scripts/opencli_ndltd.js --fetch 100CCU00442070
 *   node scripts/opencli_ndltd.js --fetch <id> [<id>...] --force
 */
const fs = require('fs');
const path = require('path');
const { spawnSync } = require('child_process');
const { parseArgs } = require('node:util');

const OPENCLI_BIN = path.join(
  process.env.HOME,
  '.opencli/node_modules/@jackwener/opencli/dist/src/main.js',
);
const BASE_DIR = path.join(__dirname, '..', 'output', 'ndltd');
const INDEX_DIR = path.join(BASE_DIR, '_index');
const BROWSER_SESSION = 'ndltd';
const HOST = 'ndltd.ncl.edu.tw';
const DEFAULT_LIST_LIMIT = 80;

// ---------------------------------------------------------------- primitives

function log(message) {
  process.stderr.write(`[${new Date().toISOString()}] ${message}\n`);
}

function opencli(args) {
  return spawnSync('node', [OPENCLI_BIN, ...args], {
    encoding: 'utf8',
    maxBuffer: 1024 * 1024 * 64,
  });
}

function opencliJson(args, label) {
  const res = opencli(args);
  if (res.status !== 0) {
    throw new Error(`${label} failed: ${(res.stderr || res.stdout || '').trim()}`);
  }
  try {
    return JSON.parse(res.stdout);
  } catch {
    throw new Error(`${label} returned invalid JSON: ${res.stdout.slice(0, 500)}`);
  }
}

function browserEval(js) {
  return opencliJson(['browser', BROWSER_SESSION, 'eval', js], 'browser eval');
}

// Fire-and-forget eval: for clicks with no return value worth parsing.
function browserRun(js) {
  const res = opencli(['browser', BROWSER_SESSION, 'eval', js]);
  if (res.status !== 0) {
    throw new Error(`browser eval failed: ${(res.stderr || res.stdout || '').trim()}`);
  }
}

function browserWait(seconds) {
  opencli(['browser', BROWSER_SESSION, 'wait', 'time', String(seconds)]);
}

function browserOpen(url) {
  const res = opencli(['browser', BROWSER_SESSION, 'open', url, '--window', 'background']);
  if (res.status !== 0) {
    throw new Error(`could not open ${url}: ${(res.stderr || '').trim()}`);
  }
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

/**
 * Filename-safe, readable, short — same rule as opencli_reddit_subs.js, so a
 * one-character title edit upstream doesn't rewrite the filename people cite.
 */
function slugify(title) {
  const cleaned = toText(title)
    .replace(/[^\p{L}\p{N}]+/gu, '-')
    .replace(/^-+|-+$/g, '');
  if (!cleaned) return 'untitled';
  return cleaned.length <= 60 ? cleaned : cleaned.slice(0, 60).replace(/-+$/, '');
}

// ------------------------------------------------------------ site plumbing

/**
 * The site-wide CAPTCHA gate can appear on ANY page, not just the first one
 * loaded. Call this after every navigation. Throwing here is deliberate: a
 * script that silently kept clicking through a CAPTCHA wall would either
 * scrape garbage or hammer the anti-bot layer — both worse than stopping.
 */
function readState() {
  return browserEval(`(() => ({
    url: location.href,
    captcha: /驗證碼檢查機制/.test(document.body?.innerText || ''),
    loggedIn: /登出/.test(document.body?.innerText || ''),
  }))()`);
}

function checkNotBlocked(context, state) {
  if (state.captcha) {
    throw new Error(
      `STOPPED (${context}): hit the site-wide CAPTCHA gate at ${state.url}\n`
      + `Clear it by hand: node ${OPENCLI_BIN} browser ${BROWSER_SESSION} open ${state.url}\n`
      + '(bring the window to the foreground, solve it, then re-run this command)',
    );
  }
  return state;
}

/**
 * Returns the ccd every ndltd URL needs to be scoped to.
 *
 * The `ndltd` browser session is a real, persistent, headed Chrome tab — it
 * does not need re-opening every run. Re-visiting the bare site root
 * (`https://ndltd.ncl.edu.tw/`) triggers the site's own SSO/session bootstrap
 * (`login?ssoauth=1&o=dwebmge`), which can mint a fresh ccd and re-trigger
 * the CAPTCHA even when the tab's cookies are still a perfectly good, logged
 * in session. So: read wherever the tab already is first, and only fall back
 * to opening the root when that isn't a usable ndltd page at all (a blank
 * tab, or some other site) — never as the default first move.
 */
function ensureSession() {
  let state = readState();
  const onSite = /^https:\/\/ndltd\.ncl\.edu\.tw\//.test(state.url);
  if (!onSite) {
    log('session tab is not on ndltd.ncl.edu.tw — opening the site root');
    browserOpen(`https://${HOST}/`);
    browserWait(2.5);
    state = readState();
  }
  checkNotBlocked('resume session', state);
  const ccdMatch = /\/ccd=([^/]+)\//.exec(state.url);
  if (!ccdMatch) throw new Error(`could not read ccd from the current URL: ${state.url}`);
  if (!state.loggedIn) {
    log('WARN not logged in — biblio and abstracts still work, but 電子全文 will refuse outright');
  }
  return { ccd: ccdMatch[1], loggedIn: state.loggedIn };
}

// ------------------------------------------------------------------ search

const SEARCH_ROWS_JS = `(() => {
  const rows = [...document.querySelectorAll('td.tdfmt1-content')];
  return rows.map((row) => {
    const cells = [...row.querySelectorAll('table.tableoutsimplefmt2 > tbody > tr > td.std2')]
      .map((td) => td.innerText.trim());
    const quoteLink = row.querySelector('a[onclick*="nclcdrbequote"]');
    const idMatch = quoteLink && /dbid=([^&']+)/.exec(quoteLink.getAttribute('onclick') || '');
    const fullTextLink = [...row.querySelectorAll('a')]
      .find((a) => a.getAttribute('title') === '電子全文');
    const statsText = row.querySelector('ul.hotarea_ul')?.innerText.replace(/\\s+/g, ' ') || '';
    return {
      id: idMatch ? idMatch[1] : null,
      title: cells[0] || '',
      meta: cells[1] || '',
      student: (cells[2] || '').replace(/^研究生[:：]/, ''),
      advisor: (cells[3] || '').replace(/^指導教授[:：]/, ''),
      thesisType: (cells[4] || '').replace(/^論文種類\\s*[:：]\\s*/, ''),
      hasFullText: !!fullTextLink,
      statsText,
    };
  });
})()`;

function parseMeta(meta) {
  const [school = '', department = '', year = '', degree = '', field = '', subfield = ''] = meta.split('／');
  return { school, department, year, degree, field, subfield };
}

function parseStats(statsText) {
  const num = (label) => Number((new RegExp(`${label}[:：]\\s*(\\d+)`).exec(statsText) || [])[1] || 0);
  return { cited: num('被引用'), views: num('點閱'), downloads: num('下載') };
}

function cmdSearch(query) {
  if (!query) throw new Error('--search needs a query string');
  const { ccd } = ensureSession();
  const searchUrl = `https://${HOST}/cgi-bin/gs32/gsweb.cgi/ccd=${ccd}/webmge?webmgemode=general&mode=basic`;
  browserOpen(searchUrl);
  browserWait(2);
  checkNotBlocked('open search form', readState());

  const fillRes = opencli(['browser', BROWSER_SESSION, 'fill', '#ysearchinput0', query]);
  if (fillRes.status !== 0) throw new Error(`could not fill the search box: ${(fillRes.stderr || '').trim()}`);
  const clickRes = opencli(['browser', BROWSER_SESSION, 'click', '#gs32search']);
  if (clickRes.status !== 0) throw new Error(`could not submit the search: ${(clickRes.stderr || '').trim()}`);
  browserWait(3);
  checkNotBlocked('read search results', readState());

  const rows = browserEval(SEARCH_ROWS_JS).filter((row) => row.id);
  if (rows.length === 0) {
    throw new Error('search returned zero usable rows — the result layout may have changed');
  }

  const known = new Set(mergeIndex().keys());
  const results = rows.map((row) => ({
    id: row.id,
    title: row.title,
    student: row.student,
    advisor: row.advisor,
    thesisType: row.thesisType,
    hasFullText: row.hasFullText,
    ...parseMeta(row.meta),
    ...parseStats(row.statsText),
  }));

  fs.mkdirSync(INDEX_DIR, { recursive: true });
  const outPath = path.join(INDEX_DIR, `${stamp()}.json`);
  fs.writeFileSync(outPath, JSON.stringify({
    captured_at: new Date().toISOString(),
    query,
    results,
  }, null, 2));

  const fresh = results.filter((r) => !known.has(r.id)).length;
  log(`Snapshot: ${path.relative(process.cwd(), outPath)}`);
  process.stdout.write(`"${query}": ${results.length} results on this page, ${fresh} new to the index\n`);
}

/**
 * Fold every snapshot into one view, newest snapshot wins — same rationale as
 * the reddit/rednote scripts: snapshots are write-once, so the merge happens
 * at read time.
 */
function mergeIndex() {
  if (!fs.existsSync(INDEX_DIR)) return new Map();
  const files = fs.readdirSync(INDEX_DIR).filter((n) => n.endsWith('.json')).sort().reverse();
  const merged = new Map();
  for (const name of files) {
    const snapshot = readJson(path.join(INDEX_DIR, name), null);
    for (const result of snapshot?.results || []) {
      if (!result?.id || merged.has(result.id)) continue;
      merged.set(result.id, { ...result, snapshot: name, query: snapshot.query });
    }
  }
  return merged;
}

// -------------------------------------------------------------------- list

function findFetched(id) {
  if (!fs.existsSync(BASE_DIR)) return null;
  for (const dir of fs.readdirSync(BASE_DIR)) {
    if (dir.startsWith('_') || dir.startsWith('.')) continue;
    const dirPath = path.join(BASE_DIR, dir);
    if (!fs.statSync(dirPath).isDirectory()) continue;
    for (const file of fs.readdirSync(dirPath)) {
      if (file.startsWith(`${id}-`) && file.endsWith('.md')) return path.join(dirPath, file);
    }
  }
  return null;
}

function cmdList(limitRaw, listAll) {
  const merged = mergeIndex();
  if (merged.size === 0) {
    process.stdout.write('index is empty — run `node scripts/opencli_ndltd.js --search <query>` first\n');
    return;
  }
  const limit = listAll ? merged.size : (limitRaw ? Number.parseInt(limitRaw, 10) : DEFAULT_LIST_LIMIT);
  if (!Number.isInteger(limit) || limit < 1) {
    throw new Error(`--list-limit takes a positive integer, got ${JSON.stringify(limitRaw)}`);
  }

  const rows = [...merged.values()].sort((a, b) => Number(b.cited || 0) - Number(a.cited || 0));
  process.stdout.write(`pool: ${path.relative(process.cwd(), BASE_DIR)}/<year>/<id>-<slug>.md\n\n`);
  for (const row of rows.slice(0, limit)) {
    const mark = findFetched(row.id) ? '✓' : '·';
    const ft = row.hasFullText ? '全文' : '  -';
    process.stdout.write(
      `${mark} ${row.id.padEnd(16)} ${String(row.cited ?? 0).padStart(4)}引 ${ft}  `
      + `${toText(row.school).padEnd(14)} ${toText(row.year).padEnd(4)} ${toText(row.title).slice(0, 50)}\n`,
    );
  }
  const shown = Math.min(limit, rows.length);
  process.stdout.write(
    `\n${rows.length} indexed${shown < rows.length ? ` · showing ${shown} (--list-all for everything)` : ''}\n`,
  );
}

// ------------------------------------------------------------------- fetch

function gotoRecord(ccd, id) {
  const url = `https://${HOST}/cgi-bin/gs32/gsweb.cgi/ccd=${ccd}/search?s=id=%22${encodeURIComponent(id)}%22.&searchmode=basic`;
  browserOpen(url);
  browserWait(2.5);
  checkNotBlocked(`open record ${id}`, readState());

  const probe = browserEval(`(() => ({
    isList: !!document.querySelector('td.tdfmt1-content'),
    isDetail: !!document.querySelector('table.tableoutfmt2'),
  }))()`);
  if (probe.isDetail) return;
  if (probe.isList) {
    browserRun("document.querySelector('td.tdfmt1-content a.slink')?.click()");
    browserWait(2.5);
    checkNotBlocked(`open record ${id} (from search)`, readState());
    return;
  }
  throw new Error(`[${id}] landed on neither a result list nor a record page — layout may have changed`);
}

const BIBLIO_JS = `(() => {
  const table = document.querySelector('table.tableoutfmt2');
  if (!table) return null;
  const fields = {};
  for (const tr of table.querySelectorAll('tr')) {
    const th = tr.querySelector('th');
    const td = tr.querySelector('td');
    if (!th || !td) continue;
    const label = th.innerText.replace(/[:：]\\s*$/, '').trim();
    fields[label] = td.innerText.trim();
  }
  return { fields, permalink: document.querySelector('#fe_text1')?.value || '' };
})()`;

/**
 * The 論文基本資料/摘要/外文摘要/... tabs are a YUI2 TabView: clicking a tab
 * label toggles which sibling panel under .yui-content loses its inline
 * display:none. Reading "whichever panel is visible" after a click is more
 * robust than guessing panel ids, which weren't stable across pages tested.
 */
function clickTab(label) {
  browserRun(`(() => {
    const a = [...document.querySelectorAll('a')].find((x) => x.innerText.trim() === ${JSON.stringify(label)});
    a && a.click();
  })()`);
  browserWait(1);
}

// `browser eval` only JSON-encodes an object/array result — a plain string
// comes back as bare, unquoted text (see opencli_rednote_liked.js's
// browserRun comment), which breaks opencliJson's JSON.parse the moment the
// text holds a newline or a quote. Wrap it in an object to force encoding.
function readActiveTabText() {
  return browserEval(`(() => {
    const panels = [...document.querySelectorAll('.yui-content > div')];
    const active = panels.find((d) => d.style.display !== 'none') || panels[0];
    return { text: active ? active.innerText.trim() : '' };
  })()`).text;
}

/**
 * Best-effort only. The declaration page (fulltextdeclare) puts up its own
 * CAPTCHA even when logged in — verified 2026-09-22 — so this never gets a
 * PDF back. It records what a human needs to finish the job by hand: the
 * declare URL (dies with the current ccd) and the permanent handle URL
 * (never dies). Nobody has checked what the declare page hands you once
 * cleared, so there's nothing here for beyond that.
 */
function fullTextInfo() {
  clickTab('電子全文');
  const info = browserEval(`(() => {
    const a = [...document.querySelectorAll('a')]
      .find((x) => (x.getAttribute('onclick') || '').includes('fulltextdeclare')
        || (x.getAttribute('onclick') || '').includes('nclconfirmlogin'));
    return { onclick: a ? a.getAttribute('onclick') : null };
  })()`);
  if (!info.onclick) return { status: 'not-available' };
  if (info.onclick.includes('nclconfirmlogin')) return { status: 'login-required' };
  // Grab window.open's whole first argument, not just the "fulltextdeclare?…"
  // tail — the ccd=<session>/ segment sits in front of it in the path, and
  // dropping it (an earlier version of this did) produces a URL the site
  // can't resolve.
  const match = /window\.open\('([^']+)'/.exec(info.onclick);
  if (!match) return { status: 'unrecognized-link' };
  return { status: 'declaration-required', declareUrl: new URL(match[1], `https://${HOST}/`).toString() };
}

/**
 * Frontmatter values go through JSON.stringify so a title or keyword list
 * holding a colon or quote doesn't produce unparseable YAML — same rule as
 * opencli_reddit_subs.js's generateMarkdown.
 */
/**
 * Not every record carries 論文出版年 — some skip straight from 論文種類 to
 * 畢業學年度 (observed on 103TIT05146036). 畢業學年度 is a ROC academic year
 * and is present on effectively everything, so fall back to it (+1911)
 * rather than losing the year, or worse, dumping the thesis into a shared
 * "0000" bucket alongside every other record with the same gap.
 */
function resolveYear(f) {
  const published = /^\d{4}/.exec(f['論文出版年'] || '')?.[0];
  if (published) return published;
  const roc = Number.parseInt(f['畢業學年度'], 10);
  return Number.isInteger(roc) && roc > 0 ? String(roc + 1911) : '';
}

function generateMarkdown(id, biblio, abstractZh, abstractEn, fullText) {
  const f = biblio.fields;
  const title = f['論文名稱'] || '(untitled)';

  const fulltextLine = {
    'not-available': '尚無電子全文。',
    'login-required': '需要登入會員才能看到電子全文選項——重新登入 ndltd session 後 `--fetch --force`。',
    'unrecognized-link': '偵測到電子全文連結，但格式看不懂——網站版面可能改了。',
    'declaration-required': `尚未取得電子全文：下載前有獨立的驗證碼+著作權聲明頁擋著，需要人工完成。\n`
      + `- 聲明頁（僅本次登入 session 有效）：${fullText.declareUrl}\n`
      + `- 永久網址（長期有效）：${biblio.permalink}`,
  }[fullText.status];

  return `---
id: ${JSON.stringify(id)}
title: ${JSON.stringify(title)}
title_en: ${JSON.stringify(f['論文名稱(外文)'] || '')}
permalink: ${JSON.stringify(biblio.permalink)}
author: ${JSON.stringify(f['研究生'] || '')}
author_en: ${JSON.stringify(f['研究生(外文)'] || '')}
advisor: ${JSON.stringify(f['指導教授'] || '')}
committee: ${JSON.stringify(f['口試委員'] || '')}
school: ${JSON.stringify(f['校院名稱'] || '')}
department: ${JSON.stringify(f['系所名稱'] || '')}
degree: ${JSON.stringify(f['學位類別'] || '')}
field: ${JSON.stringify(f['學門'] || '')}
year: ${JSON.stringify(resolveYear(f))}
language: ${JSON.stringify(f['語文別'] || '')}
pages: ${JSON.stringify(f['論文頁數'] || '')}
keywords_zh: ${JSON.stringify(f['中文關鍵詞'] || '')}
keywords_en: ${JSON.stringify(f['外文關鍵詞'] || '')}
fulltext_status: ${JSON.stringify(fullText.status)}
fetched: ${JSON.stringify(new Date().toISOString().slice(0, 10))}
---

# ${title}

## 摘要
${abstractZh || '(無中文摘要)'}

## Abstract
${abstractEn || '(no English abstract)'}

## 電子全文
${fulltextLine}
`;
}

function fetchOne(ccd, id, force) {
  const existing = findFetched(id);
  if (existing && !force) {
    log(`skip ${id} (already fetched: ${path.relative(BASE_DIR, existing)})`);
    return false;
  }

  gotoRecord(ccd, id);
  const biblio = browserEval(BIBLIO_JS);
  if (!biblio) throw new Error(`[${id}] no biblio table found on the record page`);

  clickTab('摘要');
  const abstractZh = readActiveTabText();
  clickTab('外文摘要');
  const abstractEn = readActiveTabText();
  const fullText = fullTextInfo();

  const yearDir = resolveYear(biblio.fields) || '0000';
  const dir = path.join(BASE_DIR, yearDir);
  const base = `${id}-${slugify(biblio.fields['論文名稱'])}`;
  fs.mkdirSync(dir, { recursive: true });

  if (existing && existing !== path.join(dir, `${base}.md`)) fs.rmSync(existing);
  fs.writeFileSync(
    path.join(dir, `${base}.md`),
    generateMarkdown(id, biblio, abstractZh, abstractEn, fullText),
  );
  log(`saved ${path.relative(BASE_DIR, path.join(dir, `${base}.md`))} (fulltext: ${fullText.status})`);
  return true;
}

function cmdFetch(ids, force) {
  const { ccd } = ensureSession();
  let done = 0;
  for (const id of ids) {
    if (fetchOne(ccd, id, force)) done += 1;
  }
  process.stdout.write(`fetched ${done} of ${ids.length}\n`);
}

// --------------------------------------------------------------------- main

function main() {
  const { values, positionals } = parseArgs({
    args: process.argv.slice(2),
    options: {
      'list-all': { type: 'boolean', default: false },
      'list-limit': { type: 'string' },
      search: { type: 'string' },
      fetch: { type: 'boolean', default: false },
      force: { type: 'boolean', default: false },
    },
    allowPositionals: true,
  });

  if (values.search && values.fetch) {
    throw new Error('--search and --fetch are separate runs; pass one or the other');
  }
  if (!values.fetch && positionals.length > 0) {
    throw new Error(`unexpected argument: ${positionals[0]} (thesis ids belong to --fetch)`);
  }

  if (values.search) {
    cmdSearch(values.search);
    return;
  }
  if (values.fetch) {
    if (positionals.length === 0) throw new Error('--fetch needs at least one thesis id');
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
