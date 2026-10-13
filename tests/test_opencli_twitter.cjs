const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { normalizeAccount, parseTimeline, eligible, mediaTasks, markdown, pacer, acquireLock } = require('../scripts/opencli_twitter.js');

const helpers = { extractMedia: () => ({ media_urls: [], media_posters: [] }), extractQuotedTweet: () => null };
function tweet(id, legacy = {}, author = 'dotey') {
  return { tweet_results: { result: { rest_id: id, core: { user_results: { result: { core: { screen_name: author } } } }, legacy: { full_text: '原文', ...legacy } } } };
}
function payload(entries) { return { data: { user: { result: { timeline_v2: { timeline: { instructions: entries } } } } } }; }

test('accept handles/profile URLs, reject other hosts and paths', () => {
  assert.equal(normalizeAccount('https://x.com/Dotey?lang=en'), 'dotey');
  assert.equal(normalizeAccount('@GodOfPrompt'), 'godofprompt');
  assert.throws(() => normalizeAccount('https://example.com/dotey'));
  assert.throws(() => normalizeAccount('https://x.com/dotey/status/123'));
  assert.throws(() => normalizeAccount('../escape'));
});

test('timeline excludes reposts, replies, other authors and pinned entries; dedup is by ID', () => {
  const seen = new Set();
  const batch = parseTimeline(payload([
    tweet('1'), tweet('1'), tweet('2', { retweeted_status_result: {} }),
    tweet('3', { in_reply_to_status_id_str: '99' }), tweet('4', {}, 'other'),
    { type: 'TimelinePinEntry', content: tweet('5') },
    { cursorType: 'Bottom', value: 'next' },
  ]), 'dotey', seen, helpers);
  assert.deepEqual(batch.posts.filter((post) => eligible(post, 'dotey')).map((post) => post.id), ['1']);
  assert.equal(batch.cursor, 'next');
  assert.equal(parseTimeline(payload([tweet('1')]), 'dotey', seen, helpers).posts.length, 0);
});

test('long note text and expanded links survive; login/rate-limit responses fail', () => {
  const node = tweet('1', { entities: { urls: [{ url: 'https://t.co/abc', expanded_url: 'https://example.com/article' }] } });
  node.tweet_results.result.note_tweet = { note_tweet_results: { result: { text: '完整長文\n第二段' } } };
  const { posts } = parseTimeline(payload([node]), 'dotey', new Set(), helpers);
  assert.equal(posts[0].text, '完整長文\n第二段');
  assert.equal(posts[0].links[0].url, 'https://example.com/article');
  assert.throws(() => parseTimeline({ error: 429 }, 'dotey', new Set(), helpers));
  assert.throws(() => parseTimeline({ errors: [{ message: 'login' }] }, 'dotey', new Set(), helpers));
  assert.throws(() => parseTimeline({ data: {} }, 'dotey', new Set(), helpers));
});

test('default uses video poster; optional video and quote media have separate names', () => {
  const post = { media_urls: ['https://video.twimg.com/example.mp4'], media_posters: ['https://pbs.twimg.com/a.jpg'], quoted_tweet: { media_urls: ['https://pbs.twimg.com/b.jpg'] } };
  assert.deepEqual(mediaTasks(post, false).map((item) => item.file), ['assets/post-1.jpg', 'assets/quote-1.jpg']);
  assert.equal(mediaTasks(post, true)[0].file, 'assets/post-1.mp4');
  assert.throws(() => mediaTasks({ media_urls: ['https://example.com/a.jpg'] }, false));
});

test('markdown preserves original text and attributed quote, no comment section', () => {
  const md = markdown({ id: '1', text: '原文\n\n第二段', author: 'dotey', url: 'https://x.com/dotey/status/1', quoted_tweet: { text: '引用原文', url: 'https://x.com/other/status/2' } }, [], []);
  assert.ok(md.includes('原文\n\n第二段'));
  assert.ok(md.includes('來源：https://x.com/other/status/2'));
  assert.ok(!md.includes('## 留言'));
});

test('pacing persists across runs and fifth task reserves a long rest', async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'twitter-pacing-'));
  try {
    fs.writeFileSync(path.join(dir, '.pacing.json'), JSON.stringify({ tasks: 4, next_at: 0 }));
    const pace = pacer(dir);
    const before = Date.now();
    await pace.before(); pace.after();
    const state = JSON.parse(fs.readFileSync(path.join(dir, '.pacing.json')));
    assert.equal(state.tasks, 5);
    assert.ok(state.next_at - before >= 180000 && state.next_at - Date.now() <= 300000);
    assert.ok(fs.existsSync(path.join(dir, '.pacing.json')));
  } finally { fs.rmSync(dir, { recursive: true, force: true }); }
});

test('second script cannot take the active lock', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'twitter-lock-'));
  try {
    const release = acquireLock(dir);
    assert.throws(() => acquireLock(dir), /仍在執行/);
    release();
    assert.ok(!fs.existsSync(path.join(dir, '.lock')));
  } finally { fs.rmSync(dir, { recursive: true, force: true }); }
});
