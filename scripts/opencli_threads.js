#!/usr/bin/env node
/**
 * Threads: profile -> recent posts + images, in one pass. No replies.
 * Ported from hello-browser-reader's Threads capture (wxt-dev-wxt/src/lib/sites/threads*.ts,
 * notes/2026-06-04-site-capture.md): same post shape, same GraphQL filter, same pacing.
 * Reuses the installed OpenCLI BrowserBridge; never launches another Chrome or closes the tab.
 * Requires ~/.opencli, its connected browser bridge, and a Threads login in that Chrome.
 *
 * node scripts/opencli_threads.js @wbfw_gw https://www.threads.com/@3laziaga44 --limit 10
 * node scripts/opencli_threads.js wbfw_gw --out ../hello-marketing/gallery/<sample>/clippings
 *
 * <out>/<handle>/index.json                 every post read (text, counts, image URLs)
 * <out>/<handle>/<code>/post.md + assets/   the newest --limit posts by that account
 * Default <out> is output/threads. Images are downloaded right away: Meta's CDN URLs expire.
 * First batch comes from the page's embedded JSON (~4–6 posts); more needs scrolling, which
 * this does at most 3 times (~9 posts each), the way a reader would, and stops on any
 * GraphQL error or login wall. Keep the Chrome window visible: a hidden tab stops loading.
 * Pacing between accounts persists between runs; SIGINT leaves Chrome and completed files.
 */
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const { pathToFileURL } = require('node:url');
const { parseArgs } = require('node:util');

const OPENCLI_DIR = path.join(os.homedir(), '.opencli/node_modules/@jackwener/opencli');
const SESSION = 'site:threads';
const PACING = { rounds: 3, responseWaitMs: 6000, waitMin: 3, waitMax: 6, budgetMs: 40000 };
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const randomMs = (min, max) => Math.round((min + Math.random() * (max - min)) * 1000);
const log = (msg) => process.stderr.write(`[${new Date().toISOString()}] ${msg}\n`);

function writeJson(file, value) {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(`${file}.tmp`, `${JSON.stringify(value, null, 2)}\n`);
  fs.renameSync(`${file}.tmp`, file);
}

function normalizeAccount(value) {
  let account = value.trim();
  if (/^https?:\/\//i.test(account)) {
    const url = new URL(account);
    if (!['threads.com', 'www.threads.com', 'threads.net', 'www.threads.net'].includes(url.hostname)) {
      throw new Error(`不是 Threads 帳號網址：${value}`);
    }
    account = url.pathname.split('/')[1] || '';
  }
  account = account.replace(/^@/, '');
  if (!/^[A-Za-z0-9._]{1,30}$/.test(account)) throw new Error(`無效帳號：${value}`);
  return account.toLowerCase();
}

// In-page: patch XHR/fetch to keep GraphQL responses (the filter is threads-bridge.ts's
// SKIP list; KEEP is left open because profile pagination's friendly name is unverified).
const HOOK = `() => {
  if (window.__hrThreads) return true;
  const st = window.__hrThreads = { blobs: [], failures: 0 };
  const SKIP = /Count|Badge|Hovercard|Mention|Emoji|Nullstate/;
  const fromBody = (b) => { const m = typeof b === 'string' && b.match(/(?:^|&)fb_api_req_friendly_name=([^&]*)/); return m ? decodeURIComponent(m[1]) : undefined; };
  const record = (name, text) => {
    if (!name || SKIP.test(name) || st.blobs.length >= 150) return;
    try { st.blobs.push(JSON.parse(text)); return; } catch {}
    for (const line of text.split('\\n')) { try { if (line.trim()) st.blobs.push(JSON.parse(line)); } catch {} }
  };
  const P = XMLHttpRequest.prototype, open = P.open, setH = P.setRequestHeader, send = P.send;
  P.open = function (m, u, ...r) { this.__u = String(u); this.__n = undefined; return open.call(this, m, u, ...r); };
  P.setRequestHeader = function (n, v) { if (n.toLowerCase() === 'x-fb-friendly-name') this.__n = v; return setH.call(this, n, v); };
  P.send = function (body) {
    if (/graphql/.test(this.__u || '')) {
      const name = this.__n || fromBody(body);
      this.addEventListener('load', () => {
        try {
          if (this.status >= 400) { st.failures++; return; }
          if (this.status === 200 && (this.responseType === '' || this.responseType === 'text')) record(name, this.responseText);
        } catch {}
      });
    }
    return send.call(this, body);
  };
  return true;
}`;

const EMBEDDED = `() => [...document.querySelectorAll('script[type="application/json"][data-sjs]')]
  .map((s) => { try { return JSON.parse(s.textContent); } catch { return null; } }).filter(Boolean)`;

// Post objects share one shape across embedded JSON and every GraphQL response.
function collectPosts(roots, account, into = new Map()) {
  const best = (m) => {
    let top;
    for (const c of m?.image_versions2?.candidates || []) if (c?.url && (!top || (c.width || 0) > (top.width || 0))) top = c;
    return top?.url;
  };
  const hasVideo = (m) => Array.isArray(m?.video_versions) && m.video_versions.length > 0;
  const visit = (o) => {
    if (!o || typeof o !== 'object') return;
    if (Array.isArray(o)) { o.forEach(visit); return; }
    if (typeof o.code === 'string' && o.taken_at && 'caption' in o && o.user?.username?.toLowerCase() === account && !into.has(o.code)) {
      const items = Array.isArray(o.carousel_media) && o.carousel_media.length ? o.carousel_media : [o];
      const t = o.text_post_app_info || {};
      into.set(o.code, {
        code: o.code, author: o.user.username, url: `https://www.threads.com/@${o.user.username}/post/${o.code}`,
        text: o.caption?.text || '', published_at: new Date(o.taken_at * 1000).toISOString(),
        likes: o.like_count ?? null, replies: t.direct_reply_count ?? null, reposts: t.repost_count ?? null, quotes: t.quote_count ?? null,
        images: [...new Set(items.map(best).filter(Boolean))], has_video: hasVideo(o) || items.some(hasVideo),
      });
    }
    for (const v of Object.values(o)) if (v && typeof v === 'object') visit(v);
  };
  visit(roots);
  return into;
}

async function loadMore(page, account, posts, limit) {
  const started = Date.now();
  const state = () => page.evaluate(`() => ({ n: window.__hrThreads.blobs.length, failures: window.__hrThreads.failures,
    hidden: document.hidden || innerHeight === 0, login: /^\\/(login|accounts)/.test(location.pathname),
    bottom: scrollY + innerHeight >= document.documentElement.scrollHeight - 80 })`);
  let s = await state();
  const baseFailures = s.failures;
  for (let round = 0; round < PACING.rounds && posts.size < limit; round += 1) {
    if (s.failures > baseFailures || s.login) return 'blocked';
    if (s.hidden) return 'hidden';
    if (Date.now() - started > PACING.budgetMs) return 'budget';
    // A few uneven strokes to the end, not one jump.
    const strokes = 2 + Math.floor(Math.random() * 3);
    for (let i = 1; i <= strokes; i += 1) {
      await page.evaluate(`() => { const from = scrollY, to = Math.max(document.documentElement.scrollHeight - innerHeight, from); scrollTo(0, from + (to - from) * ${i} / ${strokes}); }`);
      await sleep(120 + Math.random() * 200);
    }
    const before = s.n, deadline = Date.now() + PACING.responseWaitMs;
    do { await sleep(400); s = await state(); } while (s.n === before && Date.now() < deadline);
    if (s.n === before) return s.bottom ? 'end' : 'quiet';
    const blobs = await page.evaluate(`() => window.__hrThreads.blobs.slice(${before})`);
    const had = posts.size;
    collectPosts(blobs, account, posts);
    log(`@${account} 第 ${round + 1} 頁：+${posts.size - had} 篇`);
    // Step back up so the end-of-list marker leaves view; otherwise Threads chains pages.
    await page.evaluate(`() => scrollBy(0, -Math.round(innerHeight * 1.5))`);
    await sleep(randomMs(PACING.waitMin, PACING.waitMax));
  }
  return posts.size >= limit ? 'limit' : 'rounds';
}

async function download(url, dest) {
  const host = new URL(url).hostname;
  if (!/(\.|^)(fbcdn\.net|cdninstagram\.com)$/.test(host)) throw new Error(`未知媒體來源 ${host}`);
  const response = await fetch(url, { signal: AbortSignal.timeout(60000) });
  if ([401, 403, 429].includes(response.status)) throw new Error(`媒體 HTTP ${response.status}，停止批次`);
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  const type = response.headers.get('content-type') || '';
  if (!type.startsWith('image/')) throw new Error('回傳不是圖片');
  fs.writeFileSync(`${dest}.part`, Buffer.from(await response.arrayBuffer()));
  fs.renameSync(`${dest}.part`, dest);
}

function markdown(post, captured_at, media) {
  const title = post.text.split('\n').find((line) => line.trim())?.slice(0, 100) || post.code;
  const fields = { title, url: post.url, author: post.author, source_type: 'threads', site: 'threads.com', published_at: post.published_at, captured_at, likes: post.likes, replies: post.replies, reposts: post.reposts, quotes: post.quotes, has_video: post.has_video };
  let text = `---\n${Object.entries(fields).map(([k, v]) => `${k}: ${JSON.stringify(v)}`).join('\n')}\n---\n\n${post.text}\n`;
  if (post.has_video) text += '\n（含影片，未下載；圖片為影片封面）\n';
  if (media.length) text += `\n## 附件\n\n${media.map((m) => (m.error ? `- 下載失敗：${m.url}（${m.error}）` : `![貼文圖片](${m.file})`)).join('\n\n')}\n`;
  return text;
}

async function capture(page, account, limit, out) {
  await page.goto(`https://www.threads.com/@${account}`);
  await sleep(randomMs(3, 6));
  const wall = await page.evaluate(`() => ({ url: location.href, posts: document.querySelectorAll('script[data-sjs]').length })`);
  if (/\/(login|accounts)/.test(new URL(wall.url).pathname)) throw new Error('登入頁，請在該 Chrome 登入 Threads 後重跑');
  await page.evaluate(HOOK);
  const posts = collectPosts(await page.evaluate(EMBEDDED), account);
  log(`@${account} 首批 ${posts.size} 篇`);
  if (!posts.size) throw new Error('頁面沒有貼文資料（未登入、帳號不存在或私人帳號）');
  const reason = posts.size >= limit ? 'limit' : await loadMore(page, account, posts, limit);
  const captured_at = new Date().toISOString();
  const all = [...posts.values()].sort((a, b) => b.published_at.localeCompare(a.published_at));
  const base = path.join(out, account);
  writeJson(path.join(base, 'index.json'), { account, source: `https://www.threads.com/@${account}`, captured_at, stop_reason: reason, posts: all });
  for (const post of all.slice(0, limit)) {
    const dir = path.join(base, post.code);
    fs.mkdirSync(path.join(dir, post.images.length ? 'assets' : ''), { recursive: true });
    const media = [];
    for (const [i, url] of post.images.entries()) {
      const file = `assets/${i + 1}.jpg`;
      try { if (!fs.existsSync(path.join(dir, file))) { await download(url, path.join(dir, file)); await sleep(randomMs(0.5, 1.5)); } media.push({ url, file }); }
      catch (error) { if (/停止批次/.test(error.message)) throw error; media.push({ url, file, error: error.message }); }
    }
    fs.writeFileSync(path.join(dir, 'post.md'), markdown(post, captured_at, media));
  }
  log(`@${account}：讀到 ${all.length} 篇，存 ${Math.min(limit, all.length)} 篇，停止原因 ${reason} → ${base}`);
  console.log(base);
}

function pacer(base) {
  const file = path.join(base, '.pacing.json');
  const state = fs.existsSync(file) ? JSON.parse(fs.readFileSync(file, 'utf8')) : { next_at: 0 };
  return {
    async before() {
      const wait = state.next_at - Date.now();
      if (wait > 0) { log(`休息 ${(wait / 1000).toFixed(1)} 秒`); await sleep(wait); }
    },
    after() { state.next_at = Date.now() + randomMs(30, 90); writeJson(file, state); },
  };
}

async function main() {
  const { values, positionals } = parseArgs({ options: {
    limit: { type: 'string', default: '10' }, out: { type: 'string' }, help: { type: 'boolean' },
  }, allowPositionals: true });
  if (values.help || !positionals.length) { console.log(fs.readFileSync(__filename, 'utf8').split(' */')[0].replace(/^.*\/\*\*\n/s, '').replace(/^ \* ?/gm, '')); return; }
  const limit = Number(values.limit);
  if (!Number.isInteger(limit) || limit < 1 || limit > 30) throw new Error('--limit 必須介於 1–30');
  const accounts = [...new Set(positionals.map(normalizeAccount))];
  const out = path.resolve(values.out || path.join(__dirname, '..', 'output', 'threads'));
  const { BrowserBridge } = await import(pathToFileURL(path.join(OPENCLI_DIR, 'dist/src/browser/bridge.js')).href);
  const page = await new BrowserBridge().connect({ session: SESSION, surface: 'adapter', siteSession: 'persistent', windowMode: 'foreground', idleTimeout: 86400 });
  for (const signal of ['SIGINT', 'SIGTERM']) process.once(signal, () => { log('已中斷，保留瀏覽器與已完成資料'); process.exit(signal === 'SIGINT' ? 130 : 143); });
  const pace = pacer(path.join(__dirname, '..', 'output', 'threads'));
  for (const account of accounts) {
    await pace.before();
    try { await capture(page, account, limit, out); } finally { pace.after(); }
  }
}

module.exports = { normalizeAccount, collectPosts, markdown };
if (require.main === module) main().catch((error) => { log(error.message); process.exitCode = 1; });
