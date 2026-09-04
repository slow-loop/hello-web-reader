/**
 * Reddit subreddit sweep — browse the index, refresh the index, fetch chosen posts.
 *
 * THE SHAPE
 * Same two decoupled stages as opencli_rednote_liked.js. Listing a subreddit
 * costs one request per subreddit and covers everything on it, so it runs on a
 * schedule. Fetching a post costs another request and downloads its media, so
 * it runs only on the handful of posts actually picked for a video.
 *
 *   --list (default)  read the local index, print it. Never touches the network.
 *   --index           list every configured subreddit, snapshot what came back.
 *   --fetch <id>...   read those posts with their comments, save body + media.
 *
 * ONLY --fetch WRITES INTO THE POOL
 * The index is a scratch layer that scripts/sources.py never sees; a post
 * becomes a source the moment it is fetched. That is deliberate: a weekly sweep
 * puts ~75 titles in front of you, and the ten you pick are the ten that should
 * show up as `unjudged`, not all seventy-five.
 *
 * WHY THE OPENCLI ADAPTER AND NOT reddit's .json
 * www.reddit.com/*.json answers 403 Blocked to a plain httpx/curl client
 * (measured 2026-09-04, which is what makes web_reader's own readers/reddit.py
 * a dead end). The OpenCLI adapter drives the real signed-in Chrome, so it
 * gets the same response a person does.
 *
 * TIME WINDOWS ARE PER SUBREDDIT, NOT GLOBAL
 * The sweep runs weekly, but r/InternetIsBeautiful only produces a handful of
 * posts a week and everything past the top three is at zero — a 'week' window
 * there returns filler. It gets 'month' instead. Frequency is a property of the
 * run; the window is a property of the subreddit.
 *
 * DEPENDS ON
 * - OpenCLI installed at ~/.opencli, daemon + browser bridge connected
 *   (check: node ~/.opencli/node_modules/@jackwener/opencli/dist/src/main.js doctor)
 * - The controlled Chrome signed in to reddit.com
 *
 * LAYOUT (under output/reddit/)
 *   _index/<timestamp>.json          one snapshot per --index run; write-once
 *   <post-date>/<id>-<slug>.md       one fetched post (frontmatter + body + comments)
 *   <post-date>/<id>-<slug>.assets/  its images
 *
 * The date directory is the post's own publish date, not the fetch date, so a
 * post always lands in the same place no matter when it is fetched. That plus
 * the `<id>-` filename prefix is what makes re-fetching idempotent.
 *
 * VIDEO IS NOT DOWNLOADED
 * A v.redd.it post is DASH: separate audio and video streams that need
 * muxing. The URL is recorded in the note instead — pick it up at step 2 of
 * the workflow, when you already know the video is going in.
 *
 * USAGE
 *   node scripts/opencli_reddit_subs.js                        # print the index
 *   node scripts/opencli_reddit_subs.js --list-all
 *   node scripts/opencli_reddit_subs.js --list-limit 200
 *   node scripts/opencli_reddit_subs.js --index                # sweep the configured subs
 *   node scripts/opencli_reddit_subs.js --index --subs ClaudeAI,aivideo
 *   node scripts/opencli_reddit_subs.js --index --subs SideProject --limit 40   # dig past the weekly cut
 *   node scripts/opencli_reddit_subs.js --index --subs InternetIsBeautiful --time week
 *   node scripts/opencli_reddit_subs.js --fetch <id> [<id>...]
 *   node scripts/opencli_reddit_subs.js --fetch <id> --force
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
const BASE_DIR = path.join(__dirname, '..', 'output', 'reddit');
const INDEX_DIR = path.join(BASE_DIR, '_index');
const DEFAULT_LIST_LIMIT = 80;

/**
 * The weekly sweep. Chosen 2026-09-04 by reading a full top-of-week from nine
 * candidates; the reasoning per subreddit lives in
 * hello-video/channels/use-ai-shorts-lab/notes/2026-09-04-reddit-source-and-topic-scan.md
 *
 * Commented-out entries are not rejects — they are the ones that do not earn a
 * weekly slot but are worth a manual `--subs` run when you want what they hold.
 */
const SUBS = [
  { name: 'SideProject', time: 'week', limit: 15 },
  { name: 'LocalLLaMA', time: 'week', limit: 15 },
  { name: 'StableDiffusion', time: 'week', limit: 15 },
  { name: 'singularity', time: 'week', limit: 15 },
  // A handful of posts a week, everything past the top three at zero score.
  // A month's worth is one screen, and that screen is worth reading.
  { name: 'InternetIsBeautiful', time: 'month', limit: 15 },

  // --subs ClaudeAI — highest scores of any candidate, but nine posts in ten
  // are memes, quota complaints and screenshots. Mine it when a reality-check
  // topic needs material, not on a schedule.
  // { name: 'ClaudeAI', time: 'week', limit: 15 },

  // --subs aivideo — 25 of 25 were hosted video, so the material cost is
  // near zero, but not one post says how it was made. Sweep it when building a
  // showcase roundup; it cannot carry a how-it-works piece.
  // { name: 'aivideo', time: 'week', limit: 15 },

  // Dropped after the 2026-09-04 read, kept here so nobody re-adds them blind:
  // - ChatGPTCoding: top of week peaked at 75 points, fifth place at 11.
  // - opensource: peaked at 56, mostly Linux/PDF/GTK tooling, visually flat.
];

// Reddit is far more tolerant than rednote, but this still drives one real
// browser: keep it to one request at a time with a human-sized gap.
const GAP_SECONDS = [3, 8];
const FETCH_GAP_SECONDS = [5, 15];

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

function readJson(filePath, fallback) {
  try {
    return JSON.parse(fs.readFileSync(filePath, 'utf8'));
  } catch {
    return fallback;
  }
}

function sleepSeconds(seconds) {
  return new Promise((resolve) => { setTimeout(resolve, seconds * 1000); });
}

function randomBetween([min, max]) {
  return min + Math.random() * (max - min);
}

function toText(value) {
  return value === undefined || value === null ? '' : String(value);
}

function stamp() {
  return new Date().toISOString().replace(/[-:]/g, '').replace(/\..+/, '');
}

function dateOf(createdUtc) {
  const seconds = Number(createdUtc);
  if (!Number.isFinite(seconds) || seconds <= 0) return '0000-00-00';
  return new Date(seconds * 1000).toISOString().slice(0, 10);
}

/**
 * Filename-safe, readable, short. The filename becomes part of the source id
 * that sources.py records a verdict against, so it has to be stable: cut at a
 * word boundary rather than mid-word, or a one-character title edit upstream
 * would rewrite the id.
 */
function slugify(title) {
  const cleaned = toText(title)
    .replace(/['\u2018\u2019]/g, '')      // don't turn "I'm" into "I-m"
    .replace(/[^A-Za-z0-9]+/g, ' ')
    .trim()
    .replace(/\s+/g, '-');
  if (!cleaned) return 'untitled';
  if (cleaned.length <= 60) return cleaned;
  const cut = cleaned.slice(0, 60);
  const lastDash = cut.lastIndexOf('-');
  return lastDash > 20 ? cut.slice(0, lastDash) : cut;
}

// -------------------------------------------------------------------- index

async function cmdIndex(subsRaw, limitRaw, timeRaw) {
  // A deeper sweep is what you reach for when a topic you already know about
  // sits below the weekly cut - good posts are not always high-scoring ones.
  const limit = limitRaw === undefined ? undefined : Number.parseInt(limitRaw, 10);
  if (limitRaw !== undefined && (!Number.isInteger(limit) || limit < 1 || limit > 100)) {
    throw new Error(`--limit takes 1-100, got ${JSON.stringify(limitRaw)}`);
  }
  // A named sub keeps whatever window SUBS already decided for it - asking for
  // r/InternetIsBeautiful by name and silently getting 'week' would hand back the
  // filler that its 'month' setting exists to avoid. A sub not in SUBS (including
  // the commented-out ones) falls back to week/15.
  const base = subsRaw
    ? subsRaw.split(',').map((raw) => raw.trim()).filter(Boolean).map((name) => {
      const known = SUBS.find((sub) => sub.name.toLowerCase() === name.toLowerCase());
      return known || { name, time: 'week', limit: 15 };
    })
    : SUBS;
  const WINDOWS = ['hour', 'day', 'week', 'month', 'year', 'all'];
  if (timeRaw !== undefined && !WINDOWS.includes(timeRaw)) {
    throw new Error(`--time takes one of ${WINDOWS.join(', ')}, got ${JSON.stringify(timeRaw)}`);
  }
  const subs = base.map((sub) => ({
    ...sub,
    ...(limit === undefined ? {} : { limit }),
    ...(timeRaw === undefined ? {} : { time: timeRaw }),
  }));
  if (subs.length === 0) throw new Error('--subs was given but parsed to nothing');

  const known = new Set(mergeIndex().keys());
  const posts = [];
  const failed = [];

  for (const [position, sub] of subs.entries()) {
    if (position > 0) {
      const gap = randomBetween(GAP_SECONDS);
      log(`resting ${gap.toFixed(1)}s`);
      await sleepSeconds(gap);
    }
    const args = [
      'reddit', 'subreddit', sub.name,
      '--sort', 'top', '--time', sub.time, '--limit', String(sub.limit),
      '-f', 'json',
    ];
    let rows;
    try {
      rows = opencliJson(args, `r/${sub.name}`);
    } catch (error) {
      log(`WARN r/${sub.name}: ${error.message}`);
      failed.push(sub.name);
      continue;
    }
    for (const row of Array.isArray(rows) ? rows : []) {
      if (!row?.id) continue;
      posts.push({ ...row, window: sub.time });
    }
    log(`r/${sub.name} (top/${sub.time}): ${Array.isArray(rows) ? rows.length : 0} posts`);
  }

  if (posts.length === 0) {
    throw new Error('every subreddit came back empty — check `opencli doctor` before believing this');
  }

  fs.mkdirSync(INDEX_DIR, { recursive: true });
  const outPath = path.join(INDEX_DIR, `${stamp()}.json`);
  fs.writeFileSync(outPath, JSON.stringify({
    captured_at: new Date().toISOString(),
    subs: subs.map((sub) => `r/${sub.name} top/${sub.time} x${sub.limit}`),
    failed,
    posts,
  }, null, 2));

  const fresh = posts.filter((post) => !known.has(post.id)).length;
  log(`Snapshot: ${path.relative(process.cwd(), outPath)}`);
  process.stdout.write(`swept ${posts.length} posts, ${fresh} of them new to the index\n`);
  if (failed.length) {
    process.stdout.write(`failed: ${failed.map((name) => `r/${name}`).join(', ')}\n`);
  }
}

/**
 * Fold every snapshot into one view, newest snapshot wins.
 *
 * Snapshots are write-once and independent, so the merge happens here at read
 * time rather than in a mutable manifest. A post that stays on top of week for
 * three weeks appears once, carrying its newest score.
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
    for (const post of snapshot?.posts || []) {
      if (!post?.id || merged.has(post.id)) continue;
      merged.set(post.id, { ...post, snapshot: name });
    }
  }
  return merged;
}

// --------------------------------------------------------------------- list

/**
 * A fetched post is `<date>/<id>-<slug>.md`. The date directory comes from the
 * post's own publish time, so this could be computed — but a post whose
 * created_utc was missing lands in 0000-00-00, and one fetched before a slug
 * rule changed keeps its old name. Scanning for the id prefix finds both.
 */
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

/** Every fetched id, in one directory walk — cmdList would otherwise re-scan per row. */
function fetchedIds() {
  const ids = new Set();
  if (!fs.existsSync(BASE_DIR)) return ids;
  for (const dir of fs.readdirSync(BASE_DIR)) {
    if (dir.startsWith('_') || dir.startsWith('.')) continue;
    const dirPath = path.join(BASE_DIR, dir);
    if (!fs.statSync(dirPath).isDirectory()) continue;
    for (const file of fs.readdirSync(dirPath)) {
      if (file.endsWith('.md')) ids.add(file.split('-')[0]);
    }
  }
  return ids;
}

function cmdList(limitRaw, listAll) {
  const merged = mergeIndex();
  if (merged.size === 0) {
    process.stdout.write('index is empty — run `node scripts/opencli_reddit_subs.js --index` first\n');
    return;
  }
  const limit = listAll ? merged.size : (limitRaw ? Number.parseInt(limitRaw, 10) : DEFAULT_LIST_LIMIT);
  if (!Number.isInteger(limit) || limit < 1) {
    throw new Error(`--list-limit takes a positive integer, got ${JSON.stringify(limitRaw)}`);
  }

  const rows = [...merged.values()].sort((a, b) => Number(b.upvotes || 0) - Number(a.upvotes || 0));
  const done = fetchedIds();
  const fetched = rows.filter((row) => done.has(row.id)).length;

  process.stdout.write(`pool: ${path.relative(process.cwd(), BASE_DIR)}/<post-date>/<id>-<slug>.md\n\n`);
  for (const row of rows.slice(0, limit)) {
    const mark = done.has(row.id) ? '✓' : '·';
    const hint = toText(row.post_hint) || (Number(row.gallery_urls?.length) > 0 ? 'gallery' : '-');
    const title = toText(row.title).slice(0, 78);
    process.stdout.write(
      `${mark} ${row.id.padEnd(8)} ${String(row.upvotes ?? 0).padStart(6)}↑ ${String(row.comments ?? 0).padStart(5)}c  `
      + `${toText(row.subreddit).padEnd(22)} ${hint.padEnd(13)} ${title}\n`,
    );
  }
  const shown = Math.min(limit, rows.length);
  process.stdout.write(
    `\n${rows.length} indexed · ${fetched} fetched · ${rows.length - fetched} not fetched`
    + `${shown < rows.length ? ` · showing ${shown} (--list-all for everything)` : ''}\n`,
  );
}

// -------------------------------------------------------------------- fetch

const IMAGE_HOSTS = /^(i|preview|external-preview)\.redd\.it$/;

function hostOf(url) {
  try {
    return new URL(url).hostname;
  } catch {
    return '';
  }
}

function imageUrlsOf(entry) {
  const urls = [];
  const dest = toText(entry.url_overridden_by_dest);
  if (dest && IMAGE_HOSTS.test(hostOf(dest))) urls.push(dest);
  for (const url of entry.gallery_urls || []) {
    if (url && !urls.includes(url)) urls.push(url);
  }
  const preview = toText(entry.preview_image_url);
  // The preview is a resized copy of the same picture, so it is only worth
  // keeping when nothing full-size was found — a link post's preview is the
  // only image it has.
  if (urls.length === 0 && preview) urls.push(preview);
  return urls;
}

function extensionFor(contentType, url) {
  const fromType = {
    'image/jpeg': '.jpg', 'image/png': '.png', 'image/gif': '.gif',
    'image/webp': '.webp', 'image/avif': '.avif',
  }[(contentType || '').split(';')[0].trim().toLowerCase()];
  if (fromType) return fromType;
  const fromUrl = path.extname(hostOf(url) ? new URL(url).pathname : url).toLowerCase();
  return /^\.(jpg|jpeg|png|gif|webp|avif)$/.test(fromUrl) ? fromUrl : '.jpg';
}

async function downloadImages(urls, assetsDir) {
  if (urls.length === 0) return [];
  fs.mkdirSync(assetsDir, { recursive: true });
  const saved = [];
  for (const [position, url] of urls.entries()) {
    try {
      const res = await fetch(url, {
        headers: { 'User-Agent': 'Mozilla/5.0', Accept: 'image/*,*/*' },
        signal: AbortSignal.timeout(60000),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const name = `${String(position + 1).padStart(2, '0')}${extensionFor(res.headers.get('content-type'), url)}`;
      await pipeline(Readable.fromWeb(res.body), fs.createWriteStream(path.join(assetsDir, name)));
      saved.push(name);
    } catch (error) {
      log(`WARN image ${position + 1}: ${error.message}`);
    }
  }
  return saved;
}

function renderComments(rows) {
  if (rows.length === 0) return '(no comments captured)';
  return rows.map((row) => {
    const depth = Math.max(0, Number((toText(row.type).match(/^L(\d+)$/) || [])[1] || 0));
    const indent = '  '.repeat(depth);
    const body = toText(row.text).trim().replace(/^>\s*/, '').replace(/\n/g, `\n${indent}  `);
    return `${indent}- **${toText(row.author) || '[deleted]'}** (${toText(row.score) || 0}): ${body}`;
  }).join('\n');
}

/**
 * Frontmatter is what scripts/sources.py's meta() reads, so every value goes
 * through JSON.stringify — a title holding a colon is otherwise unparseable
 * YAML and the whole source silently loses its title on the board.
 */
function generateMarkdown(entry, post, comments, mediaFiles) {
  const title = toText(entry.title) || toText(post?.text).split('\n')[0];
  const body = toText(post?.text).startsWith(title)
    ? toText(post.text).slice(title.length).trim()
    : (toText(post?.text) || toText(entry.selftext));
  const video = toText(entry.url_overridden_by_dest).includes('v.redd.it')
    ? toText(entry.url_overridden_by_dest)
    : '';
  const link = toText(entry.url_overridden_by_dest) && !video && !imageUrlsOf(entry).includes(entry.url_overridden_by_dest)
    ? toText(entry.url_overridden_by_dest)
    : '';

  const media = [
    ...mediaFiles.map((name, index) => `![image${index + 1}](./${path.basename(entry.assetsRel)}/${name})`),
    video ? `- video (not downloaded, DASH): ${video}` : '',
    link ? `- links out to: ${link}` : '',
  ].filter(Boolean).join('\n') || '(no media)';

  return `---
title: ${JSON.stringify(title)}
url: ${JSON.stringify(toText(entry.url))}
author: ${JSON.stringify(toText(entry.author))}
subreddit: ${JSON.stringify(toText(entry.subreddit))}
score: ${Number(entry.upvotes ?? 0)}
comments: ${Number(entry.comments ?? 0)}
post_hint: ${JSON.stringify(toText(entry.post_hint))}
posted: ${JSON.stringify(dateOf(entry.created_utc))}
fetched: ${JSON.stringify(new Date().toISOString().slice(0, 10))}
---

# ${title}

## Media
${media}

## Body
${body || '(link post, no body text)'}

## Comments
${renderComments(comments)}
`;
}

async function fetchOne(entry, force) {
  const existing = findFetched(entry.id);
  if (existing && !force) {
    log(`skip ${entry.id} (already fetched: ${path.relative(BASE_DIR, existing)})`);
    return false;
  }

  const rows = opencliJson(['reddit', 'read', entry.id, '-f', 'json'], `read ${entry.id}`);
  const list = Array.isArray(rows) ? rows : [];
  const post = list.find((row) => toText(row.type).toUpperCase() === 'POST') || list[0];
  const comments = list.filter((row) => /^L\d+$/.test(toText(row.type)));

  const dir = path.join(BASE_DIR, dateOf(entry.created_utc));
  const base = `${entry.id}-${slugify(entry.title)}`;
  fs.mkdirSync(dir, { recursive: true });

  entry.assetsRel = `${base}.assets`;
  const saved = await downloadImages(imageUrlsOf(entry), path.join(dir, entry.assetsRel));
  if (saved.length === 0) fs.rmSync(path.join(dir, entry.assetsRel), { recursive: true, force: true });

  // A re-fetch whose slug changed leaves the old note — and its assets folder —
  // behind under the previous name. Take both, or the pool grows orphans.
  if (existing && existing !== path.join(dir, `${base}.md`)) {
    fs.rmSync(existing);
    fs.rmSync(existing.replace(/\.md$/, '.assets'), { recursive: true, force: true });
  }
  fs.writeFileSync(path.join(dir, `${base}.md`), generateMarkdown(entry, post, comments, saved));
  log(`saved ${path.relative(BASE_DIR, path.join(dir, `${base}.md`))} (${saved.length} images, ${comments.length} comments)`);
  return true;
}

async function cmdFetch(ids, force) {
  const merged = mergeIndex();
  const missing = ids.filter((id) => !merged.has(id));
  if (missing.length) {
    throw new Error(`not in the index: ${missing.join(', ')}\nRun --index first, or check the id against --list`);
  }

  let done = 0;
  for (const [position, id] of ids.entries()) {
    if (position > 0) {
      const gap = randomBetween(FETCH_GAP_SECONDS);
      log(`resting ${gap.toFixed(1)}s`);
      await sleepSeconds(gap);
    }
    if (await fetchOne(merged.get(id), force)) done += 1;
  }
  process.stdout.write(`fetched ${done} of ${ids.length}\n`);
}

// --------------------------------------------------------------------- main

async function main() {
  const { values, positionals } = parseArgs({
    args: process.argv.slice(2),
    options: {
      'list-all': { type: 'boolean', default: false },
      'list-limit': { type: 'string' },
      index: { type: 'boolean', default: false },
      subs: { type: 'string' },
      limit: { type: 'string' },
      time: { type: 'string' },
      fetch: { type: 'boolean', default: false },
      force: { type: 'boolean', default: false },
    },
    allowPositionals: true,
  });

  if (values.index && values.fetch) {
    throw new Error('--index and --fetch are separate runs; pass one or the other');
  }
  if (!values.fetch && positionals.length > 0) {
    throw new Error(`unexpected argument: ${positionals[0]} (post ids belong to --fetch)`);
  }

  if (values.index) {
    await cmdIndex(values.subs, values.limit, values.time);
    return;
  }
  if (values.fetch) {
    if (positionals.length === 0) throw new Error('--fetch needs at least one post id');
    await cmdFetch(positionals, values.force);
    return;
  }
  cmdList(values['list-limit'], values['list-all']);
}

main().catch((error) => {
  process.stderr.write(`${error.message}\n`);
  process.exitCode = 1;
});
