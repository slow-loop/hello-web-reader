#!/usr/bin/env node
/**
 * Twitter/X: bounded account index -> selected posts + images. No replies.
 * Reuses the installed OpenCLI BrowserBridge and Twitter adapter helpers;
 * never launches another Chrome or closes the persistent adapter tab.
 * Requires ~/.opencli, its connected browser bridge, and an X login.
 *
 * node scripts/opencli_twitter.js --index --accounts dotey,godofprompt --limit 20
 * node scripts/opencli_twitter.js --list                       # offline
 * node scripts/opencli_twitter.js --fetch <id> [<id>...]        # images included
 * node scripts/opencli_twitter.js --fetch <id> --video         # also video/GIF
 *
 * output/twitter/_index/<stamp>.json (raw snapshot + filtered posts)
 * output/twitter/<handle>/<id>/post.md + assets/
 * --limit counts raw timeline items, not retained originals. Max 100 items,
 * three cursor pages per account. No scroll, thread/reply fetch or auto-refill.
 * Links are preserved and t.co destinations resolved, not crawled for full text.
 * Partial assets are retried on the next --fetch. Existing complete files skip.
 * Shared pacing persists between runs; SIGINT leaves Chrome and completed files.
 */
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const { pathToFileURL } = require('node:url');
const { parseArgs } = require('node:util');

const BASE_DIR = path.join(__dirname, '..', 'output', 'twitter');
const OPENCLI_DIR = path.join(os.homedir(), '.opencli/node_modules/@jackwener/opencli');
const SESSION = 'site:twitter';
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const randomMs = (min, max) => Math.round((min + Math.random() * (max - min)) * 1000);
const log = (msg) => process.stderr.write(`[${new Date().toISOString()}] ${msg}\n`);

function readJson(file, fallback) {
  if (!fs.existsSync(file)) return fallback;
  return JSON.parse(fs.readFileSync(file, 'utf8'));
}

function writeJson(file, value) {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(`${file}.tmp`, `${JSON.stringify(value, null, 2)}\n`);
  fs.renameSync(`${file}.tmp`, file);
}

function normalizeAccount(value) {
  let account = value.trim();
  if (/^https?:\/\//i.test(account)) {
    const url = new URL(account);
    if (!['x.com', 'www.x.com', 'twitter.com', 'www.twitter.com'].includes(url.hostname)) {
      throw new Error(`不是 X 帳號網址：${value}`);
    }
    account = url.pathname.replace(/^\//, '').replace(/\/$/, '');
  }
  account = account.replace(/^@/, '');
  if (!/^[A-Za-z0-9_]{1,15}$/.test(account)) throw new Error(`無效帳號：${value}`);
  return account.toLowerCase();
}

// Unlike `tweets`' formatted rows, retain reply facts and expanded link entities.
function parseTimeline(payload, account, seen, helpers) {
  if (payload?.error || payload?.errors?.length) throw new Error('X 回報請求錯誤／限流，停止批次');
  const user = payload?.data?.user?.result;
  const instructions = user?.timeline_v2?.timeline?.instructions || user?.timeline?.timeline?.instructions;
  if (!Array.isArray(instructions)) throw new Error('X 未提供可讀時間軸，可能需要登入或帳號無法存取');
  const posts = [];
  let cursor = null;
  const visit = (node) => {
    if (!node || typeof node !== 'object' || node.type === 'TimelinePinEntry') return;
    if ((node.cursorType === 'Bottom' || node.cursorType === 'ShowMore') && node.value) cursor = node.value;
    const result = node.tweet_results?.result;
    const tw = result?.tweet || result;
    if (tw?.rest_id && !seen.has(tw.rest_id)) {
      seen.add(tw.rest_id);
      const legacy = tw.legacy || {};
      const author = tw.core?.user_results?.result;
      const note = tw.note_tweet?.note_tweet_results?.result;
      const quoteResult = tw.quoted_status_result?.result || legacy.quoted_status_result?.result;
      const quote = quoteResult?.tweet || quoteResult;
      const quoteNote = quote?.note_tweet?.note_tweet_results?.result;
      posts.push({
        id: tw.rest_id, author: author?.core?.screen_name || author?.legacy?.screen_name || '',
        name: author?.core?.name || author?.legacy?.name || '',
        text: note?.text || legacy.full_text || '', created_at: legacy.created_at || '',
        url: `https://x.com/${account}/status/${tw.rest_id}`,
        is_retweet: Boolean(legacy.retweeted_status_result || tw.retweeted_status_result || legacy.full_text?.startsWith('RT @')),
        reply_to: legacy.in_reply_to_status_id_str || null,
        likes: legacy.favorite_count || 0, retweets: legacy.retweet_count || 0,
        replies: legacy.reply_count || 0, views: tw.views?.count ?? null,
        ...helpers.extractMedia(legacy), quoted_tweet: helpers.extractQuotedTweet(tw),
        links: [...(legacy.entities?.urls || []), ...(note?.entity_set?.urls || []),
          ...(quote?.legacy?.entities?.urls || []), ...(quoteNote?.entity_set?.urls || [])]
          .map((link) => ({ short_url: link.url, url: link.expanded_url || link.url }))
          .filter((link) => link.url),
      });
    }
    for (const child of Object.values(node)) if (child && typeof child === 'object') visit(child);
  };
  visit(instructions);
  return { posts, cursor };
}

function eligible(post, account) {
  return post.author.toLowerCase() === account && !post.is_retweet && !post.reply_to;
}

function acquireLock(base) {
  const file = path.join(base, '.lock');
  fs.mkdirSync(base, { recursive: true });
  if (fs.existsSync(file)) {
    const pid = Number(fs.readFileSync(file, 'utf8'));
    if (!Number.isInteger(pid) || pid <= 0) throw new Error(`鎖檔異常，請檢查 ${file}`);
    try { process.kill(pid, 0); throw new Error(`另一個 Twitter 腳本仍在執行（PID ${pid}）`); }
    catch (error) { if (error.code !== 'ESRCH') throw error; }
    fs.unlinkSync(file);
  }
  fs.writeFileSync(file, String(process.pid), { flag: 'wx' });
  const release = () => { if (fs.existsSync(file) && fs.readFileSync(file, 'utf8') === String(process.pid)) fs.unlinkSync(file); };
  process.once('exit', release);
  return release;
}

function pacer(base) {
  const file = path.join(base, '.pacing.json');
  let state = readJson(file, { tasks: 0, next_at: 0 });
  return {
    async before() {
      const remaining = state.next_at - Date.now();
      if (remaining > 0) { log(`休息 ${(remaining / 1000).toFixed(1)} 秒`); await sleep(remaining); }
      // Reserve the next slot even when interrupted or the request fails.
      state.tasks += 1;
      state.next_at = Date.now() + randomMs(...(state.tasks % 5 === 0 ? [180, 300] : [30, 90]));
      writeJson(file, state);
    },
    after() {
      state.next_at = Math.max(state.next_at, Date.now() + randomMs(...(state.tasks % 5 === 0 ? [180, 300] : [30, 90])));
      writeJson(file, state);
    },
  };
}

function mergedIndex(base) {
  const dir = path.join(base, '_index');
  const result = new Map();
  if (!fs.existsSync(dir)) return result;
  for (const file of fs.readdirSync(dir).filter((name) => name.endsWith('.json')).sort()) {
    for (const post of readJson(path.join(dir, file)).posts) result.set(post.id, post);
  }
  return result;
}

async function loadOpencli() {
  const load = (relative) => import(pathToFileURL(path.join(OPENCLI_DIR, relative)).href);
  const { BrowserBridge } = await load('dist/src/browser/bridge.js');
  const timeline = await load('clis/twitter/user-timeline.js');
  const helpers = await load('clis/twitter/shared.js');
  const bridge = new BrowserBridge();
  const page = await bridge.connect({ session: SESSION, surface: 'adapter', siteSession: 'persistent', windowMode: 'foreground', idleTimeout: 86400 });
  return { page, timeline, helpers }; // Deliberately no browser/tab close.
}

async function indexAccounts(base, accounts, limit, pace) {
  const { page, timeline, helpers } = await loadOpencli();
  let tab;
  for (const account of accounts) {
    await pace.before();
    try {
      await page.goto(`https://x.com/${account}`);
      // The same Page instance carries its target ID through all navigations.
      if (tab && tab !== page._page) throw new Error('分頁身分改變，停止批次以免另開瀏覽器');
      tab = page._page;
      log(`@${account} headed session=${SESSION} tab=${tab}`);
      await sleep(randomMs(3, 6));
      const wall = await page.evaluate(`() => ({ url: location.href, title: document.title, blocked: /unusual activity|verify you are human|rate limit exceeded|異常活動|验证身份|驗證身分/i.test(document.body.innerText) })`);
      if (wall.blocked || /\/(?:login|account\/access|i\/flow)/.test(wall.url)) throw new Error('登入／驗證／限流頁，請手動處理後重跑');
      const context = await timeline.resolveUserTimelineContext(page, account);
      const raw = [], payloads = [], seen = new Set(), cursors = new Set();
      let cursor = null, reason = 'page-cap';
      for (let round = 0; round < 3; round += 1) {
        if (round > 0) await sleep(randomMs(5, 8));
        const payload = await timeline.fetchUserTimelinePage(page, context, cursor, Math.min(20, limit - raw.length));
        payloads.push(payload);
        const batch = parseTimeline(payload, account, seen, helpers);
        raw.push(...batch.posts.slice(0, limit - raw.length));
        if (raw.length >= limit) { reason = 'limit'; break; }
        if (!batch.cursor) { reason = 'end'; break; }
        if (!batch.posts.length || cursors.has(batch.cursor)) { reason = 'no-progress'; break; }
        cursors.add(batch.cursor); cursor = batch.cursor;
      }
      const captured_at = new Date().toISOString();
      const posts = raw.filter((post) => eligible(post, account)).map((post) => ({ ...post, captured_at }));
      const stamp = captured_at.replace(/[:.]/g, '-');
      writeJson(path.join(base, '_index', `${stamp}-${account}.json`), { account, captured_at, limit, stop_reason: reason, payloads, raw, posts });
      log(`@${account}：讀取 ${raw.length} 筆，保留 ${posts.length} 篇，停止原因 ${reason}`);
    } finally { pace.after(); }
  }
}

function mediaTasks(post, video) {
  const tasks = [];
  for (const [owner, item] of [['post', post], ['quote', post.quoted_tweet]]) {
    for (const [index, url] of (item?.media_urls || []).entries()) {
      const host = new URL(url).hostname;
      const isVideo = host === 'video.twimg.com';
      const selected = isVideo && !video ? item.media_posters?.[index] : url;
      if (!selected) continue;
      const parsed = new URL(selected);
      if (!['pbs.twimg.com', 'video.twimg.com'].includes(parsed.hostname)) throw new Error('未知媒體來源，停止下載');
      const ext = parsed.searchParams.get('format') || path.extname(parsed.pathname).slice(1) || 'jpg';
      if (!['jpg', 'jpeg', 'png', 'webp', 'gif', 'mp4'].includes(ext)) throw new Error('未知媒體格式');
      tasks.push({ url: selected, source_url: url, file: `assets/${owner}-${index + 1}.${ext}` });
    }
  }
  return tasks;
}

async function resolveLinks(post) {
  const links = new Map((post.links || []).map((link) => [link.short_url, { ...link }]));
  for (const text of [post.text, post.quoted_tweet?.text || '']) {
    for (const url of text.match(/https?:\/\/[^\s<>]+/g) || []) if (!links.has(url)) links.set(url, { short_url: url, url });
  }
  for (const link of links.values()) {
    if (new URL(link.url).hostname !== 't.co') continue;
    await sleep(randomMs(1, 3));
    try {
      const response = await fetch(link.url, { method: 'HEAD', signal: AbortSignal.timeout(15000) });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      link.url = response.url;
    } catch (error) { link.error = error.message; }
  }
  return [...links.values()];
}

function markdown(post, media, links) {
  const title = post.text.split('\n').find((line) => line.trim())?.slice(0, 100) || post.id;
  const fields = { title, url: post.url, author: post.author, source_type: 'twitter', site: 'x.com', published_at: post.created_at, captured_at: post.captured_at, archived_at: new Date().toISOString(), likes: post.likes, retweets: post.retweets, views: post.views };
  let text = `---\n${Object.entries(fields).map(([key, value]) => `${key}: ${JSON.stringify(value)}`).join('\n')}\n---\n\n${post.text}\n`;
  if (post.quoted_tweet) text += `\n## 引用貼文\n\n來源：${post.quoted_tweet.url}\n\n${post.quoted_tweet.text}\n`;
  if (media.length) text += `\n## 附件\n\n${media.map((item) => {
    if (item.error) return `- 下載失敗：${item.source_url}（${item.error}）`;
    const local = item.file.endsWith('.mp4') ? `- [影片](${item.file})` : `![${item.file.startsWith('assets/quote') ? '引用貼文圖片' : '貼文圖片'}](${item.file})`;
    return `${local}\n\n媒體來源：${item.source_url}`;
  }).join('\n\n')}\n`;
  if (links.length) text += `\n## 連結\n\n${links.map((link) => `- ${link.short_url} → ${link.url}${link.error ? `（解析失敗：${link.error}）` : ''}`).join('\n')}\n`;
  return text;
}

async function fetchPosts(base, ids, video, pace) {
  const index = mergedIndex(base);
  for (const id of ids) if (!/^\d+$/.test(id) || !index.has(id)) throw new Error(`索引沒有推文 ${id}，先執行 --index`);
  let partial = false;
  for (const id of [...new Set(ids)]) {
    const post = index.get(id), dir = path.join(base, normalizeAccount(post.author), id);
    const tasks = mediaTasks(post, video);
    const saved = readJson(path.join(dir, 'archive.json'), null);
    if (saved && !saved.partial && saved.video === video && fs.existsSync(path.join(dir, 'post.md')) && tasks.every((item) => fs.existsSync(path.join(dir, item.file)))) { log(`跳過已存 ${id}`); continue; }
    await pace.before();
    try {
      fs.mkdirSync(path.join(dir, 'assets'), { recursive: true });
      // Preserve the text even if a subsequent media request stops the batch.
      if (!fs.existsSync(path.join(dir, 'post.md'))) fs.writeFileSync(path.join(dir, 'post.md'), markdown(post, [], post.links || []));
      for (const item of tasks) {
        const dest = path.join(dir, item.file);
        if (fs.existsSync(dest)) continue;
        await sleep(randomMs(1, 3));
        const response = await fetch(item.url, { signal: AbortSignal.timeout(120000) });
        if ([401, 403, 429].includes(response.status)) throw new Error(`媒體 HTTP ${response.status}，停止批次`);
        try {
          if (!response.ok) throw new Error(`HTTP ${response.status}`);
          if (!/^(image|video)\//.test(response.headers.get('content-type') || '')) throw new Error('回傳不是圖片／影片');
          const { Readable } = require('node:stream');
          const { pipeline } = require('node:stream/promises');
          await pipeline(Readable.fromWeb(response.body), fs.createWriteStream(`${dest}.part`));
          fs.renameSync(`${dest}.part`, dest);
        } catch (error) { item.error = error.message; }
      }
      const links = await resolveLinks(post);
      const incomplete = tasks.some((item) => item.error) || links.some((link) => link.error);
      fs.writeFileSync(path.join(dir, 'post.md'), markdown(post, tasks, links));
      writeJson(path.join(dir, 'archive.json'), { post, media: tasks, links, video, partial: incomplete });
      partial ||= incomplete;
      log(`已存 ${post.author}/${id}，附件 ${tasks.length}${incomplete ? '（部分未完成，下次重試）' : ''}`);
    } finally { pace.after(); }
  }
  if (partial) process.exitCode = 3;
}

async function main() {
  const { values, positionals } = parseArgs({ options: {
    index: { type: 'boolean' }, list: { type: 'boolean' }, fetch: { type: 'boolean' },
    accounts: { type: 'string' }, limit: { type: 'string', default: '20' }, video: { type: 'boolean' }, help: { type: 'boolean' },
  }, allowPositionals: true });
  if (values.help) { console.log(fs.readFileSync(__filename, 'utf8').split(' */')[0].replace(/^.*\/\*\*\n/s, '').replace(/^ \* ?/gm, '')); return; }
  if ([values.index, values.list, values.fetch].filter(Boolean).length > 1) throw new Error('--index、--list、--fetch 請分開執行');
  const limit = Number(values.limit);
  if (!Number.isInteger(limit) || limit < 1 || limit > 100) throw new Error('--limit 必須介於 1–100');
  if (values.accounts && !values.index) throw new Error('--accounts 只用於 --index');
  if (values.video && !values.fetch) throw new Error('--video 只用於 --fetch');
  if (positionals.length && !values.fetch) throw new Error('推文 ID 只用於 --fetch');
  if (!values.index && !values.fetch) {
    for (const post of mergedIndex(BASE_DIR).values()) console.log(`${post.id}\t@${post.author}\t${post.created_at}\t${post.text.replace(/\s+/g, ' ').slice(0, 100)}`);
    return;
  }
  const accounts = values.index ? [...new Set((values.accounts || '').split(',').map(normalizeAccount))] : [];
  if (values.fetch && !positionals.length) throw new Error('--fetch 請指定推文 ID');
  const release = acquireLock(BASE_DIR);
  for (const signal of ['SIGINT', 'SIGTERM']) process.once(signal, () => { log('已中斷，保留瀏覽器與已完成資料'); process.exit(signal === 'SIGINT' ? 130 : 143); });
  try {
    const pace = pacer(BASE_DIR);
    if (values.index) await indexAccounts(BASE_DIR, accounts, limit, pace);
    else await fetchPosts(BASE_DIR, positionals, Boolean(values.video), pace);
  } finally { release(); }
}

module.exports = { normalizeAccount, parseTimeline, eligible, mediaTasks, markdown, pacer, acquireLock };
if (require.main === module) main().catch((error) => { log(error.message); process.exitCode = 1; });
