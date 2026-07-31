#!/usr/bin/env bash
# Incrementally back up the output/ archive to Google Drive.
#
# Default destination:
#   ~/Library/CloudStorage/GoogleDrive-w121211@gmail.com/My Drive/backup/hello-web-reader-archive/
#
# The archive is append-only: files are written once and never modified. So
# instead of re-uploading a full snapshot each time, every run packs ONLY the
# files added since the last successful run into a new dated part-set under
# parts/. Old part-sets are never touched and never need pruning — total
# backup size stays ~equal to the archive itself. Restoring = extracting all
# part-sets in chronological order (see RESTORE.md, rewritten each run).
#
# The first run (no LAST_BACKUP stamp at the destination) is automatically a
# full pack. Use --full to re-baseline later — e.g. after a bulk rename inside
# output/, which would otherwise leave stale paths behind from older parts.
#
# Never archived: .env, cookies files (credentials don't belong in cloud
# storage); .DS_Store noise.
set -euo pipefail

usage() {
  cat >&2 <<'EOF'
usage: scripts/backup_archive.sh [options]

Options:
  --dest-root DIR   Backup destination. Default: Google Drive
                    backup/hello-web-reader-archive/.
  --part-size SIZE  split(1) part size. Default: 1000m.
  --full            Pack the whole archive, ignoring the LAST_BACKUP stamp
                    (re-baseline; part-set folder gets a -full suffix).
  --dry-run         Show what would be packed; write nothing.
  -h, --help        Show this help.

Environment overrides: DEST_ROOT, PART_SIZE

Examples:
  scripts/backup_archive.sh --dry-run
  scripts/backup_archive.sh
  scripts/backup_archive.sh --full
EOF
}

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUTPUT_DIR="$REPO_ROOT/output"

DEFAULT_DEST_ROOT="$HOME/Library/CloudStorage/GoogleDrive-w121211@gmail.com/My Drive/backup/hello-web-reader-archive"
DEST_ROOT="${DEST_ROOT:-$DEFAULT_DEST_ROOT}"
PART_SIZE="${PART_SIZE:-1000m}"
FULL=0
DRY_RUN=0

while [ "$#" -gt 0 ]; do
  case "$1" in
    --dest-root)
      DEST_ROOT="${2:-}"
      [ -n "$DEST_ROOT" ] || { echo "missing value for --dest-root" >&2; exit 2; }
      shift 2
      ;;
    --part-size)
      PART_SIZE="${2:-}"
      [ -n "$PART_SIZE" ] || { echo "missing value for --part-size" >&2; exit 2; }
      shift 2
      ;;
    --full) FULL=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage; exit 2 ;;
  esac
done

[ -d "$OUTPUT_DIR" ] || { echo "output/ not found: $OUTPUT_DIR" >&2; exit 1; }

STAMP_FILE="$DEST_ROOT/LAST_BACKUP.txt"
MODE="incremental"
if [ "$FULL" = "1" ] || [ ! -f "$STAMP_FILE" ]; then
  MODE="full"
fi

# Select files, paths relative to the repo root ("output/...").
select_files() {
  if [ "$MODE" = "full" ]; then
    find output -type f ! -name ".DS_Store" ! -name "*.tmp"
  else
    find output -type f ! -name ".DS_Store" ! -name "*.tmp" -newer "$STAMP_FILE"
  fi
}

FILELIST="$(mktemp)"
trap 'rm -f "$FILELIST"' EXIT
( cd "$REPO_ROOT" && select_files | LC_ALL=C sort ) > "$FILELIST"

n_files="$(wc -l < "$FILELIST" | tr -d ' ')"
total_bytes=0
if [ "$n_files" != "0" ]; then
  total_bytes="$(cd "$REPO_ROOT" && tr '\n' '\0' < "$FILELIST" | xargs -0 stat -f %z | awk '{s+=$1} END {print s}')"
fi
human_size="$(awk -v b="$total_bytes" 'BEGIN {
  split("B KB MB GB TB", u, " "); i=1
  while (b >= 1024 && i < 5) { b/=1024; i++ }
  printf "%.1f%s", b, u[i] }')"

window_note="everything (full)"
if [ "$MODE" = "incremental" ]; then
  window_note="files newer than $(tail -1 "$STAMP_FILE" 2>/dev/null || echo "?")"
fi

echo "archive:      $OUTPUT_DIR"
echo "destination:  $DEST_ROOT"
echo "mode:         $MODE ($window_note)"
echo "to pack:      $n_files file(s), $human_size"

if [ "$n_files" = "0" ]; then
  echo "nothing new since last backup — no part-set written."
  exit 0
fi

if [ "$DRY_RUN" = "1" ]; then
  echo "dry run:      no files written. First files:"
  head -10 "$FILELIST" | sed 's/^/  /'
  [ "$n_files" -gt 10 ] && echo "  … and $((n_files - 10)) more"
  exit 0
fi

SET_NAME="$(date +%Y-%m-%d-%H%M)"
[ "$MODE" = "full" ] && SET_NAME="$SET_NAME-full"
SET_DIR="$DEST_ROOT/parts/$SET_NAME"
if [ -e "$SET_DIR" ]; then
  echo "part-set already exists: $SET_DIR" >&2
  exit 1
fi
mkdir -p "$SET_DIR"

cp "$FILELIST" "$SET_DIR/FILELIST.txt"

{
  echo "hello-web-reader archive backup part-set"
  echo
  echo "set_name: $SET_NAME"
  echo "created_at: $(date '+%Y-%m-%d %H:%M:%S %Z')"
  echo "mode: $MODE"
  echo "window: $window_note"
  echo "file_count: $n_files"
  echo "total_size: $human_size"
  echo "part_size: $PART_SIZE"
  echo "archive_format: split uncompressed tar, paths relative to repo root"
} > "$SET_DIR/MANIFEST.txt"

echo "creating split tar parts..."
COPYFILE_DISABLE=1 tar -cf - -C "$REPO_ROOT" -T "$FILELIST" \
  | split -b "$PART_SIZE" - "$SET_DIR/archive.tar.part-"

echo "writing checksums..."
( cd "$SET_DIR" && shasum -a 256 archive.tar.part-* > SHA256SUMS.txt )

# Stamp only after the part-set is fully written: a failed run leaves the old
# stamp in place, so the next run simply re-packs the same window (harmless —
# extraction just overwrites identical content).
printf '%s\n' "last successful backup (this file's mtime is the incremental cutoff)" \
  "$(date '+%Y-%m-%d %H:%M:%S %Z')" > "$STAMP_FILE"

cat > "$DEST_ROOT/RESTORE.md" <<EOF
# Restore the hello-web-reader output/ archive

Part-sets under \`parts/\` are cumulative and append-only. To restore, extract
ALL of them in chronological (lexical) folder order into the repo root:

\`\`\`sh
cd "/path/to/hello-web-reader"
for d in "$DEST_ROOT/parts"/*/; do
  cat "\$d"archive.tar.part-* | tar -xf - -C .
done
\`\`\`

Verify any single part-set first:

\`\`\`sh
cd "$DEST_ROOT/parts/<set>" && shasum -a 256 -c SHA256SUMS.txt
\`\`\`

Notes:
- A \`-full\` set is a re-baseline; sets older than the NEWEST \`-full\` set are
  superseded (extracting them first is harmless but unnecessary).
- .env and cookies files are never archived — recreate them locally.
EOF

echo "done: $SET_DIR"
du -sh "$SET_DIR"
echo "verify: (cd \"$SET_DIR\" && shasum -a 256 -c SHA256SUMS.txt)"
