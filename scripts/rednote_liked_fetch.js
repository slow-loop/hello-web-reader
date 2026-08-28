/**
 * Fetch full content for notes in the signed-in rednote.com account's Like tab.
 *
 * The browser page is the queue. There is no liked_notes.json and no manifest:
 * a valid output/rednote/liked/<note-id>/note.md is the durable state.
 *
 * DEPENDS ON
 * - OpenCLI installed at ~/.opencli
 * - OpenCLI daemon + browser bridge connected
 * - The controlled Chrome profile logged into rednote.com
 *
 * OUTPUT (per note, under output/rednote/liked/<note-id>/)
 * - note.json
 * - comments.json
 * - downloaded images/videos
 * - note.md (the agent-readable source)
 *
 * SAFETY
 * - One note at a time.
 * - Stops on the first risk-control/interstitial signal.
 * - Random 30-90s delay between notes; every fifth note rests 3-5 minutes.
 * - Existing valid folders are reused and note.md is regenerated in place.
 *
 * USAGE
 *   node scripts/rednote_liked_fetch.js
 *   node scripts/rednote_liked_fetch.js --list-only
 *   node scripts/rednote_liked_fetch.js --only <note-id>
 */
const fs = require('fs');
const path = require('path');
const { spawnSync } = require('child_process');

const OPENCLI_BIN = path.join(
  process.env.HOME,
  '.opencli/node_modules/@jackwener/opencli/dist/src/main.js',
);
const BASE_DIR = path.join(__dirname, '..', 'output', 'rednote', 'liked');
const BROWSER_SESSION = 'rednote-liked-fetch';
const MAX_SCROLLS = 80;
const QUIET_ROUNDS_TO_STOP = 3;
// Stop scrolling once this many consecutive rounds load only already-fetched notes.
// The Like tab is ordered newest-liked-first, so hitting known territory means
// everything below is already in the pool. --list-only still scans the full tab.
const KNOWN_ROUNDS_TO_STOP = 2;

function parseArgs(argv) {
  const options = { listOnly: false, onlyId: null };
  for (let i = 0; i < argv.length; i += 1) {
    if (argv[i] === '--list-only') {
      options.listOnly = true;
    } else if (argv[i] === '--only') {
      options.onlyId = argv[i + 1] || null;
      i += 1;
    } else {
      throw new Error(`Unknown argument: ${argv[i]}`);
    }
  }
  if (argv.includes('--only') && !options.onlyId) {
    throw new Error('--only requires a note id');
  }
  return options;
}

function opencli(args) {
  return spawnSync('node', [OPENCLI_BIN, ...args], {
    encoding: 'utf8',
    maxBuffer: 1024 * 1024 * 30,
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

function fieldValue(rows, field) {
  const row = Array.isArray(rows) ? rows.find((item) => item.field === field) : null;
  return row ? String(row.value ?? '') : '';
}

function isNoteBlocked(rows) {
  if (!Array.isArray(rows) || rows.length === 0) return true;
  const title = fieldValue(rows, 'title').trim();
  const author = fieldValue(rows, 'author').trim();
  const content = fieldValue(rows, 'content').trim();
  return title === 'Security Verification' || (!author && !content);
}

function resolveTitle(rows, expectedTitle) {
  const title = fieldValue(rows, 'title').trim();
  if (expectedTitle && title !== expectedTitle.trim()) {
    return { title: expectedTitle.trim(), corrected: true, rawTitle: title };
  }
  return { title, corrected: false, rawTitle: title };
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
    .filter((fileName) => /\.(jpg|jpeg|png|webp|mp4|mov)$/i.test(fileName))
    .sort();
}

function renderComments(comments) {
  if (!Array.isArray(comments) || comments.length === 0) {
    return '(无评论 / 未抓到评论)';
  }
  const lines = [];
  let number = 0;
  for (const comment of comments) {
    if (!comment.is_reply) {
      number += 1;
      lines.push(
        `${number}. **${comment.author}**（${comment.likes}赞，${comment.time}）：${comment.text}`,
      );
    } else {
      lines.push(`   ↳ 回复${comment.reply_to ? ` @${comment.reply_to}` : ''}：${comment.text}`);
    }
  }
  return lines.join('\n');
}

function generateMarkdown(entry, noteRows, comments, mediaFiles) {
  const { title } = resolveTitle(noteRows, entry.title);
  const author = fieldValue(noteRows, 'author') || entry.author;
  const content = fieldValue(noteRows, 'content');
  const likes = fieldValue(noteRows, 'likes') || entry.likes;
  const collects = fieldValue(noteRows, 'collects');
  const commentsCount = fieldValue(noteRows, 'comments');
  const tags = fieldValue(noteRows, 'tags');
  const mediaSection = mediaFiles.length
    ? mediaFiles.map((fileName, index) => (
      /\.(mp4|mov)$/i.test(fileName)
        ? `[视频${index + 1}](./${fileName})`
        : `![图${index + 1}](./${fileName})`
    )).join('\n')
    : '(无媒体文件)';

  return `# ${title}

作者：${author}　赞：${likes}　收藏：${collects}　评论：${commentsCount}
标签：${tags}
原文链接：${entry.url}

## 媒体
${mediaSection}

## 正文
${content}

## 评论
${renderComments(comments)}
`;
}

function validateExisting(entry) {
  const noteDir = path.join(BASE_DIR, entry.id);
  const noteJsonPath = path.join(noteDir, 'note.json');
  if (!fs.existsSync(noteJsonPath)) return null;
  const rows = readJson(noteJsonPath, null);
  if (isNoteBlocked(rows)) return null;
  const comments = readJson(path.join(noteDir, 'comments.json'), []);
  const expectedComments = Number.parseInt(
    fieldValue(rows, 'comments').replace(/[^\d]/g, ''),
    10,
  ) || 0;
  if (expectedComments > 0 && comments.length === 0) return null;
  return { rows, comments };
}

function browserEval(js) {
  return opencliJson(['browser', BROWSER_SESSION, 'eval', js], 'browser eval');
}

function sleepSeconds(seconds) {
  const waitBuffer = new SharedArrayBuffer(4);
  Atomics.wait(new Int32Array(waitBuffer), 0, 0, Math.round(seconds * 1000));
}

function collectLikedNotes(targetId = null, fullScan = false) {
  const me = opencliJson(
    ['rednote', 'whoami', '--format', 'json', '--site-session', 'persistent'],
    'rednote whoami',
  );
  if (!me.logged_in || !me.user_id) {
    throw new Error('The OpenCLI-controlled Chrome profile is not logged into rednote.com');
  }

  const likedUrl = `https://www.rednote.com/user/profile/${me.user_id}?tab=liked`;
  const openRes = opencli(['browser', BROWSER_SESSION, 'open', likedUrl, '--window', 'background']);
  if (openRes.status !== 0) {
    throw new Error(`Could not open the Rednote Like tab: ${openRes.stderr.trim()}`);
  }
  opencli(['browser', BROWSER_SESSION, 'wait', 'time', '3']);

  const extractJs = `(() => {
    const raw = JSON.parse(JSON.stringify(window.__INITIAL_STATE__?.user?.notes?._value || []));
    const cards = raw.flat(Infinity).filter((item) => item && item.noteCard);
    const entries = cards.map((item) => {
      const card = item.noteCard;
      const id = card.noteId || item.id;
      const token = card.xsecToken || item.xsecToken || '';
      return {
        id,
        title: card.displayTitle || '',
        author: card.user?.nickname || '',
        likes: card.interactInfo?.likedCount || '',
        url: 'https://www.rednote.com/explore/' + id
          + '?xsec_token=' + encodeURIComponent(token)
          + '&xsec_source=pc_user',
      };
    });
    const root = document.documentElement;
    return {
      entries,
      scrollHeight: root.scrollHeight,
      atBottom: window.scrollY + window.innerHeight >= root.scrollHeight - 32,
    };
  })()`;

  const byId = new Map();
  let quietRounds = 0;
  let knownRounds = 0;
  try {
    for (let round = 0; round < MAX_SCROLLS; round += 1) {
      const snapshot = browserEval(extractJs);
      const before = byId.size;
      let freshThisRound = 0;
      for (const entry of snapshot.entries || []) {
        if (!entry.id) continue;
        if (!byId.has(entry.id)
          && !fs.existsSync(path.join(BASE_DIR, entry.id, 'note.md'))) {
          freshThisRound += 1;
        }
        byId.set(entry.id, entry);
      }
      quietRounds = byId.size === before ? quietRounds + 1 : 0;
      if (byId.size > before) {
        knownRounds = freshThisRound === 0 ? knownRounds + 1 : 0;
      }
      log(`Like tab: ${byId.size} notes loaded`);
      if (targetId && byId.has(targetId)) break;
      if (!fullScan && !targetId && knownRounds >= KNOWN_ROUNDS_TO_STOP) {
        log('Reached already-fetched territory; stopping the scroll early');
        break;
      }
      if (snapshot.atBottom && quietRounds >= QUIET_ROUNDS_TO_STOP) break;
      browserEval('window.scrollTo(0, document.documentElement.scrollHeight)');
      opencli(['browser', BROWSER_SESSION, 'wait', 'time', '2']);
    }
  } finally {
    opencli(['browser', BROWSER_SESSION, 'close']);
  }
  return [...byId.values()];
}

function fetchOne(entry) {
  const noteDir = path.join(BASE_DIR, entry.id);
  fs.mkdirSync(noteDir, { recursive: true });
  log(`Fetching [${entry.id}] ${entry.title}`);

  const noteRes = opencli([
    'rednote', 'note', entry.url,
    '--format', 'json',
    '--site-session', 'persistent',
  ]);
  let noteRows;
  try {
    noteRows = JSON.parse(noteRes.stdout);
  } catch {
    noteRows = null;
  }
  if (isNoteBlocked(noteRows)) {
    throw new Error(`STOPPED: risk-control response while fetching [${entry.id}] ${entry.title}`);
  }

  const titleCheck = resolveTitle(noteRows, entry.title);
  if (titleCheck.corrected) {
    log(`Corrected transient title "${titleCheck.rawTitle}" to "${titleCheck.title}"`);
  }
  fs.writeFileSync(path.join(noteDir, 'note.json'), JSON.stringify(noteRows, null, 2));

  const commentsRes = opencli([
    'rednote', 'comments', entry.url,
    '--limit', '50',
    '--with-replies',
    '--format', 'json',
    '--site-session', 'persistent',
  ]);
  let comments;
  try {
    comments = JSON.parse(commentsRes.stdout);
  } catch {
    comments = [];
  }
  const expectedComments = Number.parseInt(
    fieldValue(noteRows, 'comments').replace(/[^\d]/g, ''),
    10,
  ) || 0;
  if (expectedComments > 0 && comments.length === 0) {
    throw new Error(
      `STOPPED: comments mismatch for [${entry.id}] ${entry.title} `
      + `(expected ${expectedComments}, got 0)`,
    );
  }
  fs.writeFileSync(path.join(noteDir, 'comments.json'), JSON.stringify(comments, null, 2));

  const downloadRes = opencli([
    'rednote', 'download', entry.url,
    '--output', BASE_DIR,
    '--site-session', 'persistent',
  ]);
  fs.writeFileSync(path.join(noteDir, 'download.log'), downloadRes.stdout || downloadRes.stderr || '');
  flattenMediaIfNested(noteDir, entry.id);
  const media = listMedia(noteDir);
  fs.writeFileSync(
    path.join(noteDir, 'note.md'),
    generateMarkdown(entry, noteRows, comments, media),
  );
  log(`OK [${entry.id}] media=${media.length} comments=${comments.length}`);
}

function main() {
  const options = parseArgs(process.argv.slice(2));
  fs.mkdirSync(BASE_DIR, { recursive: true });
  let entries = collectLikedNotes(options.onlyId, options.listOnly);

  if (options.onlyId) {
    entries = entries.filter((entry) => entry.id === options.onlyId);
    if (entries.length === 0) {
      throw new Error(`Note ${options.onlyId} was not found in the signed-in account's Like tab`);
    }
  }

  if (options.listOnly) {
    for (const entry of entries) {
      process.stdout.write(`${entry.id}\t${entry.title}\n`);
    }
    return;
  }

  const queue = [];
  for (const entry of entries) {
    const noteDir = path.join(BASE_DIR, entry.id);
    flattenMediaIfNested(noteDir, entry.id);
    const existing = validateExisting(entry);
    if (existing) {
      const media = listMedia(noteDir);
      fs.writeFileSync(
        path.join(noteDir, 'note.md'),
        generateMarkdown(entry, existing.rows, existing.comments, media),
      );
    } else {
      queue.push(entry);
    }
  }
  log(`Ready: ${entries.length - queue.length}; to fetch: ${queue.length}`);

  for (let index = 0; index < queue.length; index += 1) {
    fetchOne(queue[index]);
    if (index === queue.length - 1) continue;
    const processed = index + 1;
    const longBreak = processed % 5 === 0;
    const delay = longBreak
      ? 180 + Math.random() * 120
      : 30 + Math.random() * 60;
    log(`Resting ${Math.round(delay)} seconds${longBreak ? ' (long break)' : ''}`);
    sleepSeconds(delay);
  }
}

function log(message) {
  process.stderr.write(`[${new Date().toISOString()}] ${message}\n`);
}

try {
  main();
} catch (error) {
  process.stderr.write(`${error.message}\n`);
  process.exitCode = 1;
}
