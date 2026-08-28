const fs = require('fs');
const path = require('path');
const { spawnSync } = require('child_process');

const OPENCLI_BIN = path.join(process.env.HOME, '.opencli/node_modules/@jackwener/opencli/dist/src/main.js');
const BROWSER_SESSION = 'rednote-liked-export';
const OUTPUT_FILE = path.join(__dirname, '..', 'output', 'rednote', 'liked_cards.json');

function opencli(args) {
  return spawnSync('node', [OPENCLI_BIN, ...args], { encoding: 'utf8', maxBuffer: 1024 * 1024 * 30 });
}

function opencliJson(args) {
  const res = opencli(args);
  if (res.status !== 0) throw new Error(res.stderr || res.stdout);
  return JSON.parse(res.stdout);
}

const me = opencliJson(['rednote', 'whoami', '--format', 'json', '--site-session', 'persistent']);
if (!me.logged_in || !me.user_id) throw new Error('Not logged in');

const likedUrl = `https://www.rednote.com/user/profile/${me.user_id}?tab=liked`;
opencli(['browser', BROWSER_SESSION, 'open', likedUrl, '--window', 'background']);
opencli(['browser', BROWSER_SESSION, 'wait', 'time', '3']);

let prevCount = 0;
let quiet = 0;

for (let round = 1; round <= 80; round++) {
  const res = opencliJson(['browser', BROWSER_SESSION, 'eval', `(() => {
    const raw = JSON.parse(JSON.stringify(window.__INITIAL_STATE__?.user?.notes?._value || []));
    const cards = raw.flat(Infinity).filter(item => item && item.noteCard);
    return cards.map(item => ({
      id: item.noteCard.noteId || item.id,
      title: item.noteCard.displayTitle || '',
      author: item.noteCard.user?.nickname || item.noteCard.user?.nickName || '',
      likes: item.noteCard.interactInfo?.likedCount || '',
      token: item.noteCard.xsecToken || item.xsecToken || '',
      url: 'https://www.rednote.com/explore/' + (item.noteCard.noteId || item.id) + '?xsec_token=' + encodeURIComponent(item.noteCard.xsecToken || item.xsecToken || '') + '&xsec_source=pc_user'
    }));
  })()`]);

  console.log(`[Round ${round}] Loaded ${res.length} notes`);
  if (res.length === prevCount) {
    quiet++;
    if (quiet >= 3) {
      fs.writeFileSync(OUTPUT_FILE, JSON.stringify(res, null, 2));
      console.log(`Exported ${res.length} notes to ${OUTPUT_FILE}`);
      break;
    }
  } else {
    quiet = 0;
    prevCount = res.length;
    fs.writeFileSync(OUTPUT_FILE, JSON.stringify(res, null, 2));
  }

  opencli(['browser', BROWSER_SESSION, 'scroll', 'down', 'window', '3000']);
  opencli(['browser', BROWSER_SESSION, 'wait', 'time', '2']);
}
