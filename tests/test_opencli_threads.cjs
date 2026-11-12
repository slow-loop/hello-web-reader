const { test } = require('node:test');
const assert = require('node:assert/strict');
const { normalizeTarget, collectPosts, markdown } = require('../scripts/opencli_threads.js');

const post = (code, username, extra = {}) => ({ code, taken_at: 1762900000, caption: { text: `text ${code}` }, user: { username }, like_count: 5, text_post_app_info: { direct_reply_count: 1 }, ...extra });

test('positionals: profile handles/URLs and single post URLs', () => {
  assert.deepEqual(normalizeTarget('@WBFW_GW'), { kind: 'profile', account: 'wbfw_gw', url: 'https://www.threads.com/@wbfw_gw' });
  assert.deepEqual(normalizeTarget('https://www.threads.net/@wbfw_gw/post/DfeEi-skx0X?xmt=1'),
    { kind: 'post', author: 'wbfw_gw', code: 'DfeEi-skx0X', url: 'https://www.threads.com/@wbfw_gw/post/DfeEi-skx0X' });
  assert.throws(() => normalizeTarget('https://example.com/@a/post/B'));
  assert.throws(() => normalizeTarget('../escape'));
});

test('account filter keeps one author; null keeps every author; video URLs are kept', () => {
  const roots = { a: [post('A', 'wbfw_gw'), post('B', 'other', { video_versions: [{ url: 'https://x.fbcdn.net/v.mp4' }] })] };
  assert.deepEqual([...collectPosts(roots, 'wbfw_gw').keys()], ['A']);
  const all = collectPosts(roots, null);
  assert.deepEqual([...all.keys()], ['A', 'B']);
  assert.equal(all.get('B').has_video, true);
  assert.deepEqual(all.get('B').videos, ['https://x.fbcdn.net/v.mp4']);
});

test('markdown lists replies and marks a missing video', () => {
  const all = collectPosts([post('A', 'me', { video_versions: [{ url: 'https://x.fbcdn.net/v.mp4' }] }), post('R', 'you')], null);
  const md = markdown(all.get('A'), 'now', [], [all.get('R')]);
  assert.match(md, /含影片，未下載/);
  assert.match(md, /## 留言\n[\s\S]*- @you（讚 5、回覆 1）：text R/);
});
