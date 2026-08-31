/**
 * One-off backfill: notes fetched through the old fallback download path
 * (rednote_liked.js's downloadViaOpencli, before the listMedia() fix) got
 * their "## 媒体" links alphabetically sorted instead of numeric — `_10`
 * before `_2`. Files on disk are fine; only the note.md labels/order were
 * scrambled. This rewrites just that section, using the same numeric sort
 * listMedia() now uses, and touches nothing else in the file.
 *
 * Safety: a note is only rewritten if every line in its "## 媒体" block
 * matches the exact machine-generated pattern (`![图N](./file)` /
 * `[视频N](./file)`). A note whose media block was hand-annotated (seen at
 * least once, a note explaining a deleted original upload) won't match and
 * is left untouched — don't guess at preserving free text, just skip it.
 *
 * Usage:
 *   node scripts/migrate_rednote_media_order.js          # apply
 *   node scripts/migrate_rednote_media_order.js --dry-run
 */
const fs = require('fs');
const path = require('path');

const BASE_DIR = path.join(__dirname, '..', 'output', 'rednote', 'liked');
const MEDIA_LINE_RE = /^(?:!\[图(\d+)\]|\[视频(\d+)\])\(\.\/(.+)\)$/;

function correctOrder(noteDir) {
  return fs.readdirSync(noteDir)
    .filter((f) => /\.(jpe?g|png|webp|gif|mp4|mov)$/i.test(f))
    .sort((a, b) => {
      const numA = Number.parseInt(/_(\d+)\.[^.]+$/.exec(a)?.[1] ?? '0', 10);
      const numB = Number.parseInt(/_(\d+)\.[^.]+$/.exec(b)?.[1] ?? '0', 10);
      return numA - numB || a.localeCompare(b);
    });
}

function buildMediaBlock(files) {
  if (files.length === 0) return '(无媒体文件)';
  return files.map((fileName, index) => (
    /\.(mp4|mov)$/i.test(fileName)
      ? `[视频${index + 1}](./${fileName})`
      : `![图${index + 1}](./${fileName})`
  )).join('\n');
}

function fixNoteMd(noteDir, dryRun) {
  const mdPath = path.join(noteDir, 'note.md');
  if (!fs.existsSync(mdPath)) return 'no-note-md';

  const text = fs.readFileSync(mdPath, 'utf8');
  const lines = text.split('\n');
  const startIdx = lines.findIndex((l) => l.trim() === '## 媒体');
  if (startIdx === -1) return 'no-media-heading';
  let endIdx = lines.findIndex((l, i) => i > startIdx && l.startsWith('## '));
  if (endIdx === -1) endIdx = lines.length;

  const blockLines = lines.slice(startIdx + 1, endIdx).filter((l) => l.trim() !== '');
  if (blockLines.length === 0) return 'no-media-files';
  if (!blockLines.every((l) => MEDIA_LINE_RE.test(l))) return 'skipped-custom-content';

  const currentFiles = blockLines.map((l) => MEDIA_LINE_RE.exec(l)[3]);
  const correctFiles = correctOrder(noteDir);

  // Directory listing can include files note.md doesn't reference (or vice
  // versa, if media went missing) — only reorder when it's the same set.
  const sameSet = currentFiles.length === correctFiles.length
    && [...currentFiles].sort().every((f, i) => f === [...correctFiles].sort()[i]);
  if (!sameSet) return 'file-set-mismatch';

  if (currentFiles.join('|') === correctFiles.join('|')) return 'already-correct';

  const newBlock = buildMediaBlock(correctFiles);
  const before = lines.slice(0, startIdx + 1).join('\n');
  const after = lines.slice(endIdx).join('\n');
  const rebuilt = `${before}\n${newBlock}\n\n${after}`;

  if (!dryRun) fs.writeFileSync(mdPath, rebuilt);
  return 'fixed';
}

function main() {
  const dryRun = process.argv.includes('--dry-run');
  const tally = {};
  const fixedIds = [];

  for (const id of fs.readdirSync(BASE_DIR)) {
    if (id === '_index') continue;
    const noteDir = path.join(BASE_DIR, id);
    if (!fs.statSync(noteDir).isDirectory()) continue;
    if (!fs.existsSync(path.join(noteDir, 'download.log'))) continue; // only the fallback path had this bug

    const result = fixNoteMd(noteDir, dryRun);
    tally[result] = (tally[result] || 0) + 1;
    if (result === 'fixed') fixedIds.push(id);
  }

  if (fixedIds.length) {
    process.stdout.write(`${dryRun ? 'Would fix' : 'Fixed'}:\n${fixedIds.map((id) => `  ${id}`).join('\n')}\n\n`);
  }
  process.stdout.write(`${JSON.stringify(tally, null, 2)}\n`);
}

main();
