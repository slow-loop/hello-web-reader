/**
 * Rednote Like tab — browse the index, refresh the index, fetch chosen notes.
 *
 * THE SHAPE
 * Two decoupled stages. Building an index costs zero per-note requests, so it
 * runs often and covers everything. Fetching a note costs one page load, so it
 * runs only on the handful of notes actually picked for a video.
 *
 *   --list (default)  read the local index, print it. Never touches the network.
 *   --index           scroll the Like tab, snapshot what the page store holds.
 *   --fetch <id>...   open those notes, save body + comments + media.
 *
 * WHY WE READ THE STORE INSTEAD OF CALLING `rednote note|comments|download`
 * Those three commands each load the same note page again (3 page loads per
 * note) and each flattens its result to a handful of DOM-scraped fields. The
 * page's own store — __INITIAL_STATE__.note.noteDetailMap[<id>] — carries the
 * body, the first page of comments AND every image URL in one load, with the
 * publish time, share count, structured tags, image dimensions and comment
 * timestamps that the flattened output drops. So we drive the browser
 * ourselves and keep the raw object. `rednote download` is still used for
 * video notes: its video-URL extraction has fallbacks worth not reimplementing.
 *
 * TWO URL LIFETIMES — do not confuse them
 * - A note URL's xsec_token is long-lived: a token minted 54 days earlier still
 *   resolved. That is what makes "index now, fetch weeks later" work.
 * - A CDN media URL is not. Its path carries a minute-resolution timestamp
 *   (.../202608301309/...) and returns 403 a few minutes later. So media must
 *   be downloaded in the same run that read it, and the cover URLs kept in an
 *   index snapshot are dead weight for anything but that moment.
 *
 * DEPENDS ON
 * - OpenCLI installed at ~/.opencli, daemon + browser bridge connected
 * - The controlled Chrome profile logged into rednote.com
 *
 * LAYOUT (under output/rednote/liked/)
 *   _index/<timestamp>.json   one snapshot per --index run; write-once
 *   <note-id>/                note.json (raw store), note.md, media files
 *
 * SAFETY
 * - One note at a time, never concurrent.
 * - Random 30-90s between notes; every fifth note rests 3-5 minutes. A single
 *   note never pauses at all — there is nothing to pause between.
 * - Stops the whole run on the first risk-control/login-wall signal.
 *
 * USAGE
 *   node scripts/rednote_liked.js                          # print newest 60
 *   node scripts/rednote_liked.js --list-all
 *   node scripts/rednote_liked.js --list-limit 200
 *   node scripts/rednote_liked.js --index                  # 3 scrolls, ~40 notes
 *   node scripts/rednote_liked.js --index --scrolls 5
 *   node scripts/rednote_liked.js --index --scrolls all
 *   node scripts/rednote_liked.js --fetch <id> [<id>...]
 *   node scripts/rednote_liked.js --fetch <id> --force
 *   node scripts/rednote_liked.js --fetch <id> --comment-scrolls 2
 *   node scripts/rednote_liked.js --fetch <id> --video-low   # smallest rendition
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
const BASE_DIR = path.join(__dirname, '..', 'output', 'rednote', 'liked');
const INDEX_DIR = path.join(BASE_DIR, '_index');
const BROWSER_SESSION = 'rednote-liked';
const WEB_HOST = 'www.rednote.com';
const DEFAULT_LIST_LIMIT = 60;
const MAX_SCROLLS = 200;
const QUIET_ROUNDS_TO_STOP = 3;

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

// Fire-and-forget eval: `browser eval` echoes the expression's value as bare
// text, so anything returning a plain string is not valid JSON. Scrolling has
// no return value worth parsing — don't route it through opencliJson().
function browserRun(js) {
  const res = opencli(['browser', BROWSER_SESSION, 'eval', js]);
  if (res.status !== 0) {
    throw new Error(`browser eval failed: ${(res.stderr || res.stdout || '').trim()}`);
  }
}

function browserWait(seconds) {
  opencli(['browser', BROWSER_SESSION, 'wait', 'time', String(seconds)]);
}

function readJson(filePath, fallback) {
  try {
    return JSON.parse(fs.readFileSync(filePath, 'utf8'));
  } catch {
    return fallback;
  }
}

function sleepSeconds(seconds) {
  const waitBuffer = new SharedArrayBuffer(4);
  Atomics.wait(new Int32Array(waitBuffer), 0, 0, Math.round(seconds * 1000));
}

function toText(value) {
  return typeof value === 'string' ? value.trim() : value == null ? '' : String(value).trim();
}

function stamp() {
  return new Date().toISOString().slice(0, 19).replace(/:/g, '');
}

// -------------------------------------------------------------------- index

/**
 * Read every entry the Like tab has hydrated so far, raw.
 *
 * We deliberately do NOT narrow the entries here. The store holds the full
 * noteCard — cover URLs, type, the whole interactInfo, the author object — and
 * all of it costs nothing to keep. 390 notes of raw JSON is under a megabyte.
 */
const EXTRACT_LIKED_JS = `(() => {
  const raw = JSON.parse(JSON.stringify(window.__INITIAL_STATE__?.user?.notes?._value || []));
  const entries = raw.flat(Infinity).filter((item) => item && (item.noteCard || item.note_card));
  const root = document.documentElement;
  return {
    entries,
    atBottom: window.scrollY + window.innerHeight >= root.scrollHeight - 32,
  };
})()`;

function cardOf(raw) {
  return (raw && (raw.noteCard || raw.note_card)) || raw || {};
}

function idOf(raw) {
  const card = cardOf(raw);
  return toText(card.noteId || card.note_id || raw.id || raw.note_id || raw.noteId);
}

function tokenOf(raw) {
  const card = cardOf(raw);
  return toText(raw.xsecToken || raw.xsec_token || card.xsecToken || card.xsec_token);
}

function buildNoteUrl(id, token) {
  return `https://${WEB_HOST}/explore/${encodeURIComponent(id)}`
    + `?xsec_token=${encodeURIComponent(token)}&xsec_source=pc_user`;
}

function resolveUserId() {
  const me = opencliJson(
    ['rednote', 'whoami', '--format', 'json', '--site-session', 'persistent'],
    'rednote whoami',
  );
  if (!me.logged_in || !me.user_id) {
    throw new Error('The OpenCLI-controlled Chrome profile is not logged into rednote.com');
  }
  return me.user_id;
}

function cmdIndex(scrollsRaw) {
  const scrollAll = scrollsRaw === 'all';
  const scrolls = scrollAll ? MAX_SCROLLS : Number.parseInt(scrollsRaw, 10);
  if (!scrollAll && (!Number.isInteger(scrolls) || scrolls < 0)) {
    throw new Error(`--scrolls takes a non-negative integer or "all", got ${JSON.stringify(scrollsRaw)}`);
  }

  const userId = resolveUserId();
  const likedUrl = `https://${WEB_HOST}/user/profile/${userId}?tab=liked`;
  const known = new Set(mergeIndex().keys());

  const openRes = opencli(['browser', BROWSER_SESSION, 'open', likedUrl, '--window', 'background']);
  if (openRes.status !== 0) {
    throw new Error(`Could not open the Rednote Like tab: ${(openRes.stderr || '').trim()}`);
  }
  browserWait(3);

  const byId = new Map();
  let quietRounds = 0;
  try {
    // scrolls=N means N scrolls, so N+1 reads: the first screen plus each
    // batch a scroll pulls in. Measured: the Like tab hydrates 10 notes per
    // batch, so the default 3 covers the newest ~40.
    for (let round = 0; round <= scrolls; round += 1) {
      const snapshot = browserEval(EXTRACT_LIKED_JS);
      const before = byId.size;
      for (const entry of snapshot.entries || []) {
        const id = idOf(entry);
        if (id) byId.set(id, entry);
      }
      log(`Like tab: ${byId.size} notes loaded`);
      quietRounds = byId.size === before ? quietRounds + 1 : 0;
      if (round === scrolls) break;
      if (snapshot.atBottom && quietRounds >= QUIET_ROUNDS_TO_STOP) {
        log('Reached the bottom of the Like tab');
        break;
      }
      browserRun('window.scrollTo(0, document.documentElement.scrollHeight)');
      browserWait(2);
    }
  } finally {
    opencli(['browser', BROWSER_SESSION, 'close']);
  }

  const notes = [];
  for (const [id, raw] of byId) {
    const token = tokenOf(raw);
    if (!token) {
      log(`WARN [${id}] no xsec_token in the store entry; skipped`);
      continue;
    }
    notes.push({ id, url: buildNoteUrl(id, token), raw });
  }

  fs.mkdirSync(INDEX_DIR, { recursive: true });
  const outPath = path.join(INDEX_DIR, `${stamp()}.json`);
  fs.writeFileSync(outPath, JSON.stringify({
    captured_at: new Date().toISOString(),
    user_id: userId,
    scrolls: scrollAll ? 'all' : scrolls,
    notes,
  }, null, 2));

  const fresh = notes.filter((note) => !known.has(note.id)).length;
  log(`Snapshot: ${path.relative(process.cwd(), outPath)}`);
  process.stdout.write(`这次看到 ${notes.length} 篇，其中 ${fresh} 篇是索引里没有的\n`);
  if (!scrollAll && fresh === notes.length && notes.length > 0) {
    process.stdout.write('（全部都是新的 —— 可能卷得不够深，考虑 --scrolls 5 或 --scrolls all）\n');
  }
}

/**
 * Fold every snapshot into one view, newest snapshot wins.
 *
 * Snapshots are write-once and independent, so the merge happens here at read
 * time rather than in a mutable manifest file. Walking newest-first and
 * appending unseen ids preserves the Like tab's newest-liked-first order.
 */
function mergeIndex() {
  if (!fs.existsSync(INDEX_DIR)) return new Map();
  const files = fs.readdirSync(INDEX_DIR)
    .filter((name) => name.endsWith('.json'))
    .sort()
    .reverse();
  const merged = new Map();
  for (const name of files) {
    const snapshot = readJson(path.join(INDEX_DIR, name), null);
    for (const note of snapshot?.notes || []) {
      if (!note?.id || merged.has(note.id)) continue;
      merged.set(note.id, { ...note, snapshot: name });
    }
  }
  return merged;
}

// --------------------------------------------------------------------- list

function isFetched(id) {
  return fs.existsSync(path.join(BASE_DIR, id, 'note.md'));
}

function padDisplay(text, width) {
  // CJK glyphs occupy two terminal columns; pad against display width so the
  // title column does not shred on mixed Chinese/ASCII rows.
  let used = 0;
  let out = '';
  for (const ch of text) {
    const w = /[ᄀ-ᅟ⺀-꓏가-힣豈-﫿︰-﹯＀-｠￠-￦]/.test(ch) ? 2 : 1;
    if (used + w > width) return `${out}…`;
    out += ch;
    used += w;
  }
  return out + ' '.repeat(width - used);
}

function cmdList(limitRaw, listAll) {
  const merged = mergeIndex();
  if (merged.size === 0) {
    process.stdout.write('索引是空的，先跑 `node scripts/rednote_liked.js --index`\n');
    return;
  }
  const limit = listAll ? merged.size : (limitRaw ? Number.parseInt(limitRaw, 10) : DEFAULT_LIST_LIMIT);
  if (!Number.isInteger(limit) || limit < 1) {
    throw new Error(`--list-limit takes a positive integer, got ${JSON.stringify(limitRaw)}`);
  }

  const rows = [...merged.values()];
  const fetched = rows.filter((row) => isFetched(row.id)).length;

  process.stdout.write(`池子：${path.relative(process.cwd(), BASE_DIR)}/<note-id>/\n\n`);
  for (const row of rows.slice(0, limit)) {
    const card = cardOf(row.raw);
    const mark = isFetched(row.id) ? '✓' : '·';
    const likes = toText(card.interactInfo?.likedCount ?? card.interact_info?.liked_count) || '0';
    const type = toText(card.type) || '-';
    const title = toText(card.displayTitle ?? card.display_title ?? card.title) || '(无标题)';
    const author = toText(card.user?.nickname ?? card.user?.nick_name);
    process.stdout.write(
      `${mark} ${row.id}  ${likes.padStart(6)}  ${type.padEnd(6)}  ${padDisplay(title, 40)}  ${author}\n`,
    );
  }
  const shown = Math.min(limit, rows.length);
  process.stdout.write(
    `\n索引 ${rows.length} 篇 · 已抓 ${fetched} · 未抓 ${rows.length - fetched}`
    + `${shown < rows.length ? ` · 本次列出 ${shown}（--list-all 看全部）` : ''}\n`,
  );
}

// -------------------------------------------------------------------- fetch

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

// The comment list lives in a scroller inside the detail panel on some layouts
// and in the window on others. Query each candidate separately, in priority
// order: a comma-joined selector returns the first match in DOCUMENT order, so
// a non-scrolling #noteContainer that happens to come first silently wins and
// the scroll goes nowhere.
const SCROLL_COMMENTS_JS = `(() => {
  const el = document.querySelector('.note-scroller')
    || document.querySelector('.comments-container')
    || document.querySelector('#noteContainer');
  if (el && el.scrollHeight > el.clientHeight + 8) {
    el.scrollTop = el.scrollHeight;
  } else {
    window.scrollTo(0, document.documentElement.scrollHeight);
  }
})()`;

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

// The CDN is a different origin from rednote.com, so the site session cookie is
// not what gates it — the Referer is.
const CDN_HEADERS = {
  Referer: `https://${WEB_HOST}/`,
  'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'
    + ' (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
};

async function cdnGet(url, timeoutMs = 120000) {
  const res = await fetch(url, { headers: CDN_HEADERS, signal: AbortSignal.timeout(timeoutMs) });
  if (!res.ok) throw new Error(`${url.slice(0, 60)}… responded ${res.status}`);
  return res;
}

/**
 * Choose which rendition of a video note to download.
 *
 * note.video.media.stream is keyed by codec (EF4/EF5/…), each holding the
 * playback renditions the app itself streams — around 8-14MB for a two-minute
 * clip. What must NOT be used is video.consumer.originVideoKey: that is the
 * creator's original upload. OpenCLI's `rednote download` falls back to exactly
 * that key (it looks for a `h264` stream bucket that current pages no longer
 * use), and pulled 537MB for a clip whose real streams were 7.7-13.8MB.
 */
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

async function downloadMedia(note, noteDir, noteId, preferLow) {
  const saved = [];
  let index = 0;

  if (toText(note.type) === 'video') {
    const stream = pickVideoStream(note, preferLow);
    if (!stream) throw new Error('no playback stream in the store');
    index += 1;
    const fileName = `${noteId}_${index}.mp4`;
    log(`  video ${stream.width}x${stream.height} ~${((stream.size || 0) / 1048576).toFixed(1)}MB`);
    await downloadStream([stream.masterUrl, ...(stream.backupUrls || [])].filter(Boolean),
      path.join(noteDir, fileName));
    saved.push(fileName);
  }

  // Video notes carry their cover in imageList too, so this runs for both kinds.
  for (const image of note.imageList || []) {
    const url = pickImageUrl(image);
    if (!url) throw new Error(`image ${index + 1} has no usable URL`);
    const res = await cdnGet(url, 60000);
    index += 1;
    const fileName = `${noteId}_${index}${extensionFor(res.headers.get('content-type'), url)}`;
    fs.writeFileSync(path.join(noteDir, fileName), Buffer.from(await res.arrayBuffer()));
    saved.push(fileName);
  }
  return saved;
}

function flattenMediaIfNested(noteDir, id) {
  const nested = path.join(noteDir, id);
  if (!fs.existsSync(nested) || !fs.statSync(nested).isDirectory()) return;
  for (const fileName of fs.readdirSync(nested)) {
    fs.renameSync(path.join(nested, fileName), path.join(noteDir, fileName));
  }
  fs.rmdirSync(nested);
}

function listMedia(noteDir) {
  if (!fs.existsSync(noteDir)) return [];
  return fs.readdirSync(noteDir)
    .filter((fileName) => /\.(jpe?g|png|webp|gif|mp4|mov)$/i.test(fileName))
    .sort();
}

function downloadViaOpencli(entry, noteDir) {
  const res = opencli([
    'rednote', 'download', entry.url,
    '--output', BASE_DIR,
    '--site-session', 'persistent',
  ]);
  fs.writeFileSync(path.join(noteDir, 'download.log'), res.stdout || res.stderr || '');
  flattenMediaIfNested(noteDir, entry.id);
  return listMedia(noteDir);
}

function formatDate(ms) {
  if (!Number.isFinite(ms) || ms <= 0) return '';
  return new Date(ms).toISOString().slice(0, 10);
}

function renderComments(comments) {
  const list = Array.isArray(comments) ? comments : [];
  if (list.length === 0) return '(无评论 / 未抓到评论)';
  const lines = [];
  list.forEach((comment, index) => {
    const author = toText(comment.userInfo?.nickname);
    const when = formatDate(Number(comment.createTime));
    lines.push(`${index + 1}. **${author}**（${toText(comment.likeCount) || 0}赞，${when}）：${toText(comment.content)}`);
    for (const sub of comment.subComments || []) {
      const target = toText(sub.targetComment?.userInfo?.nickname);
      lines.push(`   ↳ 回复${target ? ` @${target}` : ''}：${toText(sub.content)}`);
    }
  });
  return lines.join('\n');
}

function generateMarkdown(entry, detail, mediaFiles) {
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

作者：${toText(note.user?.nickname)}　赞：${toText(interact.likedCount)}　收藏：${toText(interact.collectedCount)}　评论：${toText(interact.commentCount)}　转发：${toText(interact.shareCount)}
发布：${formatDate(Number(note.time))}　类型：${toText(note.type)}
标签：${tags}
原文链接：${entry.url}

## 媒体
${media}

## 正文
${toText(note.desc)}

## 评论
${renderComments(detail?.comments?.list)}
`;
}

async function fetchOne(entry, commentScrolls, videoLow) {
  const noteDir = path.join(BASE_DIR, entry.id);
  fs.mkdirSync(noteDir, { recursive: true });

  const openRes = opencli(['browser', BROWSER_SESSION, 'open', entry.url, '--window', 'background']);
  if (openRes.status !== 0) {
    throw new Error(`STOPPED: could not open [${entry.id}]: ${(openRes.stderr || '').trim()}`);
  }
  browserWait(2 + Math.random() * 3);

  const extractJs = buildNoteExtractJs(entry.id);
  let page = browserEval(extractJs);
  if (page.securityBlock) throw new Error(`STOPPED: risk control on [${entry.id}]`);
  if (page.loginWall) throw new Error(`STOPPED: login wall on [${entry.id}] — re-login to rednote.com`);
  if (page.notFound) throw new Error(`[${entry.id}] the note is gone (deleted or restricted)`);
  if (!page.detail?.note) throw new Error(`[${entry.id}] noteDetailMap was empty`);

  // Comments are deliberately NOT chased by default. Rednote comment threads
  // carry far less than a PTT/Reddit thread does, so they are not worth extra
  // page interaction — but whatever the first page load already put in the
  // store is free, so we keep that.
  for (let i = 0; i < commentScrolls; i += 1) {
    if (!page.detail?.comments?.hasMore) break;
    browserRun(SCROLL_COMMENTS_JS);
    // Measured: the next comment page needs ~3s to land in the store.
    browserWait(3);
    page = browserEval(extractJs);
    if (page.securityBlock) throw new Error(`STOPPED: risk control on [${entry.id}] while loading comments`);
  }

  const detail = page.detail;
  fs.writeFileSync(path.join(noteDir, 'note.json'), JSON.stringify(detail, null, 2));

  const note = detail.note || {};
  let media;
  try {
    media = await downloadMedia(note, noteDir, entry.id, videoLow);
  } catch (error) {
    // `rednote download` reloads the page and scrapes it, and for video it can
    // land on the origin upload instead of a stream — so it is the last resort,
    // not the default path.
    log(`WARN [${entry.id}] direct download failed (${error.message}); falling back to rednote download`);
    media = downloadViaOpencli(entry, noteDir);
  }

  // A note with no media at all is almost always a failed fetch, not a real
  // text-only post — and it is invisible afterwards, because the folder still
  // has note.json and note.md and reads as "already fetched". Six notes in the
  // pool sat broken this way until an audit caught them. Say so out loud.
  if (media.length === 0) {
    log(`WARN [${entry.id}] fetched but saved 0 media files — likely incomplete, re-run with --force`);
  }

  fs.writeFileSync(path.join(noteDir, 'note.md'), generateMarkdown(entry, detail, media));
  const bytes = media.reduce((sum, name) => sum + fs.statSync(path.join(noteDir, name)).size, 0);
  return { noteDir, media, bytes, comments: detail?.comments?.list?.length || 0 };
}

function clearMedia(noteDir, id) {
  if (!fs.existsSync(noteDir)) return;
  const pattern = new RegExp(`^${id}_\\d+\\.(jpe?g|png|webp|gif|mp4|mov)$`, 'i');
  for (const fileName of fs.readdirSync(noteDir)) {
    if (pattern.test(fileName)) fs.unlinkSync(path.join(noteDir, fileName));
  }
}

async function cmdFetch(ids, force, commentScrollsRaw, videoLow) {
  const commentScrolls = Number.parseInt(commentScrollsRaw, 10);
  if (!Number.isInteger(commentScrolls) || commentScrolls < 0) {
    throw new Error(`--comment-scrolls takes a non-negative integer, got ${JSON.stringify(commentScrollsRaw)}`);
  }

  const merged = mergeIndex();
  // Resolve every id up front. The index is the only place xsec_token lives,
  // and a note URL without one is always rejected — so a missing id is a
  // "refresh the index" problem, not something to discover halfway through.
  const missing = ids.filter((id) => !merged.has(id));
  if (missing.length > 0) {
    throw new Error(
      `not in the index: ${missing.join(', ')}\n`
      + 'Run `node scripts/rednote_liked.js --index` first (it mints the xsec_token these URLs need).',
    );
  }

  const queue = [];
  for (const id of ids) {
    if (isFetched(id) && !force) {
      process.stdout.write(`SKIP ${id} 已有 → ${path.relative(process.cwd(), path.join(BASE_DIR, id))}/\n`);
      continue;
    }
    if (force) clearMedia(path.join(BASE_DIR, id), id);
    queue.push(merged.get(id));
  }
  if (queue.length === 0) return;

  fs.mkdirSync(BASE_DIR, { recursive: true });
  log(`To fetch: ${queue.length}`);
  try {
    for (let index = 0; index < queue.length; index += 1) {
      const entry = queue[index];
      log(`Fetching [${entry.id}]`);
      const result = await fetchOne(entry, commentScrolls, videoLow);
      process.stdout.write(
        `OK ${entry.id} → ${path.relative(process.cwd(), result.noteDir)}/`
        + ` (media=${result.media.length} ${(result.bytes / 1048576).toFixed(1)}MB`
        + ` comments=${result.comments})\n`,
      );
      // Pause between notes only — a single-note run never sleeps.
      if (index === queue.length - 1) continue;
      const longBreak = (index + 1) % 5 === 0;
      const delay = longBreak ? 180 + Math.random() * 120 : 30 + Math.random() * 60;
      log(`Resting ${Math.round(delay)} seconds${longBreak ? ' (long break)' : ''}`);
      sleepSeconds(delay);
    }
  } finally {
    opencli(['browser', BROWSER_SESSION, 'close']);
  }
}

// --------------------------------------------------------------------- main

async function main() {
  const { values, positionals } = parseArgs({
    args: process.argv.slice(2),
    options: {
      'list-all': { type: 'boolean', default: false },
      'list-limit': { type: 'string' },
      index: { type: 'boolean', default: false },
      scrolls: { type: 'string', default: '3' },
      fetch: { type: 'boolean', default: false },
      force: { type: 'boolean', default: false },
      'comment-scrolls': { type: 'string', default: '0' },
      'video-low': { type: 'boolean', default: false },
    },
    allowPositionals: true,
  });

  if (values.index && values.fetch) {
    throw new Error('--index and --fetch are separate runs; pass one or the other');
  }
  if (!values.fetch && positionals.length > 0) {
    throw new Error(`unexpected argument: ${positionals[0]} (note ids belong to --fetch)`);
  }

  if (values.index) {
    cmdIndex(values.scrolls);
    return;
  }
  if (values.fetch) {
    if (positionals.length === 0) throw new Error('--fetch needs at least one note id');
    await cmdFetch(positionals, values.force, values['comment-scrolls'], values['video-low']);
    return;
  }
  cmdList(values['list-limit'], values['list-all']);
}

main().catch((error) => {
  process.stderr.write(`${error.message}\n`);
  process.exitCode = 1;
});
