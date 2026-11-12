#!/usr/bin/env node
/**
 * Threads: profiles, search, home feed and single posts (with replies), images and optional video.
 * Ported from hello-browser-reader's Threads capture (wxt-dev-wxt/src/lib/sites/threads*.ts,
 * notes/2026-06-04-site-capture.md): same post shape, same GraphQL filter, same pacing.
 * Reuses the installed OpenCLI BrowserBridge; never launches another Chrome or closes the tab.
 * Requires ~/.opencli, its connected browser bridge, and a Threads login in that Chrome.
 *
 * node scripts/opencli_threads.js @wbfw_gw https://www.threads.com/@3laziaga44 --limit 10
 * node scripts/opencli_threads.js --search 排名表 --search 分級表 --limit 30
 * node scripts/opencli_threads.js --home --limit 30
 * node scripts/opencli_threads.js https://www.threads.com/@wbfw_gw/post/DfeEi-skx0X --video
 *
 * Profile  <out>/<handle>/index.json + <out>/<handle>/<code>/post.md   newest --limit posts by that account
 * Search   <out>/_index/search-<query>-<stamp>.json                   every post read, any author; nothing downloaded
 * Home     <out>/_index/home-<stamp>.json                             same, from the logged-in "For you" feed
 * Post     <out>/<author>/<code>/post.md + assets/                    the post, its media, and up to --limit replies
 * Search and home only index (text, counts, media URLs). Pick from the index, then pass the chosen
 * post URLs to save them: images are downloaded right away (Meta's CDN URLs expire); videos only
 * with --video, otherwise the cover image stands in.
 * Default <out> is output/threads. First batch comes from the page's embedded JSON; more needs
 * scrolling, which this does at most 3 times, the way a reader would, and stops on any GraphQL
 * error or login wall. Keep the Chrome window visible: a hidden tab stops loading.
 * Pacing between pages persists between runs; SIGINT leaves Chrome and completed files.
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
const THREADS_HOSTS = ['threads.com', 'www.threads.com', 'threads.net', 'www.threads.net'];

function writeJson(file, value) {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(`${file}.tmp`, `${JSON.stringify(value, null, 2)}\n`);
  fs.renameSync(`${file}.tmp`, file);
}

function normalizeAccount(value) {
  let account = value.trim();
  if (/^https?:\/\//i.test(account)) {
    const url = new URL(account);
    if (!THREADS_HOSTS.includes(url.hostname)) {
      throw new Error(`不是 Threads 帳號網址：${value}`);
    }
    account = url.pathname.split('/')[1] || '';
  }
  account = account.replace(/^@/, '');
  if (!/^[A-Za-z0-9._]{1,30}$/.test(account)) throw new Error(`無效帳號：${value}`);
  return account.toLowerCase();
}

// A positional is a profile (@handle or profile URL) or a single post URL (/@handle/post/<code>).
function normalizeTarget(value) {
  const m = /^https?:\/\/[^/]+\/@([A-Za-z0-9._]{1,30})\/post\/([A-Za-z0-9_-]+)/i.exec(value.trim());
  if (m) {
    if (!THREADS_HOSTS.includes(new URL(value.trim()).hostname)) throw new Error(`不是 Threads 貼文網址：${value}`);
    const author = m[1].toLowerCase();
    return { kind: 'post', author, code: m[2], url: `https://www.threads.com/@${author}/post/${m[2]}` };
  }
  const account = normalizeAccount(value);
  return { kind: 'profile', account, url: `https://www.threads.com/@${account}` };
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
// account: keep only that author's posts; null keeps every author (search, home, replies).
function collectPosts(roots, account, into = new Map()) {
  const best = (m) => {
    let top;
    for (const c of m?.image_versions2?.candidates || []) if (c?.url && (!top || (c.width || 0) > (top.width || 0))) top = c;
    return top?.url;
  };
  const video = (m) => (Array.isArray(m?.video_versions) && m.video_versions.find((v) => v?.url)?.url) || undefined;
  const visit = (o) => {
    if (!o || typeof o !== 'object') return;
    if (Array.isArray(o)) { o.forEach(visit); return; }
    const author = o.user?.username;
    if (typeof o.code === 'string' && o.taken_at && 'caption' in o && author && (!account || author.toLowerCase() === account) && !into.has(o.code)) {
      const items = Array.isArray(o.carousel_media) && o.carousel_media.length ? o.carousel_media : [o];
      const t = o.text_post_app_info || {};
      const videos = [...new Set(items.map(video).filter(Boolean))];
      into.set(o.code, {
        code: o.code, author, url: `https://www.threads.com/@${author}/post/${o.code}`,
        text: o.caption?.text || '', published_at: new Date(o.taken_at * 1000).toISOString(),
        likes: o.like_count ?? null, replies: t.direct_reply_count ?? null, reposts: t.repost_count ?? null, quotes: t.quote_count ?? null,
        images: [...new Set(items.map(best).filter(Boolean))], has_video: videos.length > 0, videos,
        thread_length: t.self_thread_info?.self_thread_length || 1,
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
    log(`第 ${round + 1} 頁：+${posts.size - had} 篇`);
    // Step back up so the end-of-list marker leaves view; otherwise Threads chains pages.
    await page.evaluate(`() => scrollBy(0, -Math.round(innerHeight * 1.5))`);
    await sleep(randomMs(PACING.waitMin, PACING.waitMax));
  }
  return posts.size >= limit ? 'limit' : 'rounds';
}

async function download(url, dest, kind = 'image') {
  const host = new URL(url).hostname;
  if (!/(\.|^)(fbcdn\.net|cdninstagram\.com)$/.test(host)) throw new Error(`未知媒體來源 ${host}`);
  const response = await fetch(url, { signal: AbortSignal.timeout(kind === 'video' ? 180000 : 60000) });
  if ([401, 403, 429].includes(response.status)) throw new Error(`媒體 HTTP ${response.status}，停止批次`);
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  const type = response.headers.get('content-type') || '';
  if (!type.startsWith(`${kind}/`)) throw new Error(`回傳不是${kind === 'video' ? '影片' : '圖片'}`);
  fs.writeFileSync(`${dest}.part`, Buffer.from(await response.arrayBuffer()));
  fs.renameSync(`${dest}.part`, dest);
}

function markdown(post, captured_at, media, replies = []) {
  const title = post.text.split('\n').find((line) => line.trim())?.slice(0, 100) || post.code;
  const fields = { title, url: post.url, author: post.author, source_type: 'threads', site: 'threads.com', published_at: post.published_at, captured_at, likes: post.likes, replies: post.replies, reposts: post.reposts, quotes: post.quotes, has_video: post.has_video };
  let text = `---\n${Object.entries(fields).map(([k, v]) => `${k}: ${JSON.stringify(v)}`).join('\n')}\n---\n\n${post.text}\n`;
  if (post.has_video && !media.some((m) => m.kind === 'video' && !m.error)) text += '\n（含影片，未下載；圖片為影片封面）\n';
  const line = (m) => {
    if (m.error) return `- 下載失敗：${m.url}（${m.error}）`;
    return m.kind === 'video' ? `[貼文影片](${m.file})` : `![貼文圖片](${m.file})`;
  };
  if (media.length) text += `\n## 附件\n\n${media.map(line).join('\n\n')}\n`;
  if (replies.length) {
    const one = (r) => `- @${r.author}（讚 ${r.likes ?? '?'}、回覆 ${r.replies ?? '?'}${r.images.length ? `、${r.images.length} 張圖` : ''}）：${r.text.replace(/\s*\n\s*/g, ' ')}`;
    text += `\n## 留言\n\n頁面上的順序，作者自己的接續段也在裡面。\n\n${replies.map(one).join('\n')}\n`;
  }
  return text;
}

async function saveMedia(dir, post, withVideo) {
  const media = [];
  const tasks = [
    ...post.images.map((url, i) => ({ url, file: `assets/${i + 1}.jpg`, kind: 'image' })),
    ...(withVideo ? post.videos.map((url, i) => ({ url, file: `assets/video-${i + 1}.mp4`, kind: 'video' })) : []),
  ];
  if (tasks.length) fs.mkdirSync(path.join(dir, 'assets'), { recursive: true });
  for (const task of tasks) {
    try {
      if (!fs.existsSync(path.join(dir, task.file))) { await download(task.url, path.join(dir, task.file), task.kind); await sleep(randomMs(0.5, 1.5)); }
      media.push(task);
    } catch (error) { if (/停止批次/.test(error.message)) throw error; media.push({ ...task, error: error.message }); }
  }
  return media;
}

async function open(page, url) {
  await page.goto(url);
  await sleep(randomMs(3, 6));
  const wall = await page.evaluate(`() => ({ url: location.href })`);
  if (/\/(login|accounts)/.test(new URL(wall.url).pathname)) throw new Error('登入頁，請在該 Chrome 登入 Threads 後重跑');
  await page.evaluate(HOOK);
  return page.evaluate(EMBEDDED);
}

async function captureProfile(page, { account, url }, limit, out, withVideo) {
  const posts = collectPosts(await open(page, url), account);
  log(`@${account} 首批 ${posts.size} 篇`);
  if (!posts.size) throw new Error('頁面沒有貼文資料（未登入、帳號不存在或私人帳號）');
  const reason = posts.size >= limit ? 'limit' : await loadMore(page, account, posts, limit);
  const captured_at = new Date().toISOString();
  const all = [...posts.values()].sort((a, b) => b.published_at.localeCompare(a.published_at));
  const base = path.join(out, account);
  writeJson(path.join(base, 'index.json'), { account, source: url, captured_at, stop_reason: reason, posts: all });
  for (const post of all.slice(0, limit)) {
    const dir = path.join(base, post.code);
    fs.mkdirSync(dir, { recursive: true });
    fs.writeFileSync(path.join(dir, 'post.md'), markdown(post, captured_at, await saveMedia(dir, post, withVideo)));
  }
  log(`@${account}：讀到 ${all.length} 篇，存 ${Math.min(limit, all.length)} 篇，停止原因 ${reason} → ${base}`);
  console.log(base);
}

// Search and home: every author, page order kept, index only.
async function captureFeed(page, { name, url, query }, limit, out) {
  const posts = collectPosts(await open(page, url), null);
  log(`${name} 首批 ${posts.size} 篇`);
  if (!posts.size) throw new Error('頁面沒有貼文資料（未登入或沒有結果）');
  const reason = posts.size >= limit ? 'limit' : await loadMore(page, null, posts, limit);
  const captured_at = new Date().toISOString();
  const stamp = captured_at.slice(0, 16).replace(/[-:T]/g, '');
  const file = path.join(out, '_index', `${name.replace(/[\\/:*?"<>|\s]+/g, '_')}-${stamp}.json`);
  writeJson(file, { kind: query === undefined ? 'home' : 'search', query, source: url, captured_at, stop_reason: reason, posts: [...posts.values()] });
  log(`${name}：讀到 ${posts.size} 篇，停止原因 ${reason} → ${file}`);
  console.log(file);
}

// A self-thread's later parts load after the page does, before the hook is in. The page links each
// part by its own URL, right after the first one, so open those and join them into one post.
async function threadParts(page, post) {
  if (post.thread_length < 2) return [];
  const codes = await page.evaluate(`() => [...new Set([...document.querySelectorAll('a[href*="/post/"]')]
    .map((a) => new URL(a.href).pathname.match(/^\\/@([^/]+)\\/post\\/([^/]+)/))
    .filter((m) => m && m[1].toLowerCase() === ${JSON.stringify(post.author.toLowerCase())}).map((m) => m[2]))]`);
  const parts = [];
  for (const code of codes.filter((c) => c !== post.code).slice(0, post.thread_length - 1)) {
    await sleep(randomMs(2, 4));
    const part = collectPosts(await open(page, `https://www.threads.com/@${post.author}/post/${code}`), null).get(code);
    if (part) parts.push(part);
  }
  log(`串文 ${post.thread_length} 段，接上 ${parts.length} 段`);
  return parts;
}

function joinThread(post, parts) {
  if (!parts.length) return post;
  return { ...post, text: [post.text, ...parts.map((p) => p.text)].filter(Boolean).join('\n\n'),
    images: [...post.images, ...parts.flatMap((p) => p.images)], videos: [...post.videos, ...parts.flatMap((p) => p.videos)],
    has_video: post.has_video || parts.some((p) => p.has_video) };
}

// One post: the post itself (self-thread parts joined) plus the replies shown under it.
async function capturePost(page, { author, code, url }, limit, out, withVideo) {
  const posts = collectPosts(await open(page, url), null);
  if (!posts.has(code)) throw new Error('頁面沒有這則貼文（已刪除、私人帳號或未登入）');
  const reason = posts.size - 1 >= limit ? 'limit' : await loadMore(page, null, posts, limit + 1);
  const parts = await threadParts(page, posts.get(code));
  const captured_at = new Date().toISOString();
  const post = joinThread(posts.get(code), parts);
  const partCodes = new Set(parts.map((p) => p.code));
  const replies = [...posts.values()].filter((p) => p.code !== code && !partCodes.has(p.code)).slice(0, limit);
  const dir = path.join(out, post.author.toLowerCase(), code);
  fs.mkdirSync(dir, { recursive: true });
  fs.writeFileSync(path.join(dir, 'post.md'), markdown(post, captured_at, await saveMedia(dir, post, withVideo), replies));
  log(`@${author}/${code}：${replies.length} 則留言，停止原因 ${reason} → ${dir}`);
  console.log(dir);
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
    search: { type: 'string', multiple: true, default: [] }, home: { type: 'boolean' }, video: { type: 'boolean' },
  }, allowPositionals: true });
  if (values.help || (!positionals.length && !values.search.length && !values.home)) { console.log(fs.readFileSync(__filename, 'utf8').split(' */')[0].replace(/^.*\/\*\*\n/s, '').replace(/^ \* ?/gm, '')); return; }
  const limit = Number(values.limit);
  if (!Number.isInteger(limit) || limit < 1 || limit > 30) throw new Error('--limit 必須介於 1–30');
  const targets = [
    ...new Map(positionals.map(normalizeTarget).map((t) => [t.url, t])).values(),
    ...values.search.map((q) => ({ kind: 'feed', name: `search-${q.trim()}`, query: q.trim(), url: `https://www.threads.com/search?q=${encodeURIComponent(q.trim())}&serp_type=default` })),
    ...(values.home ? [{ kind: 'feed', name: 'home', url: 'https://www.threads.com/' }] : []),
  ];
  const out = path.resolve(values.out || path.join(__dirname, '..', 'output', 'threads'));
  const { BrowserBridge } = await import(pathToFileURL(path.join(OPENCLI_DIR, 'dist/src/browser/bridge.js')).href);
  const page = await new BrowserBridge().connect({ session: SESSION, surface: 'adapter', siteSession: 'persistent', windowMode: 'foreground', idleTimeout: 86400 });
  for (const signal of ['SIGINT', 'SIGTERM']) process.once(signal, () => { log('已中斷，保留瀏覽器與已完成資料'); process.exit(signal === 'SIGINT' ? 130 : 143); });
  const pace = pacer(path.join(__dirname, '..', 'output', 'threads'));
  const run = { profile: captureProfile, feed: captureFeed, post: capturePost };
  for (const target of targets) {
    await pace.before();
    try { await run[target.kind](page, target, limit, out, values.video); } finally { pace.after(); }
  }
}

module.exports = { normalizeAccount, normalizeTarget, collectPosts, markdown };
if (require.main === module) main().catch((error) => { log(error.message); process.exitCode = 1; });
