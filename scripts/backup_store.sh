#!/usr/bin/env bash
# Incrementally back up the output/ store to a local backup destination
# (e.g. a Google Drive desktop sync folder).
#
# No default destination — set BACKUP_DIR in .env (gitignored) or pass
# --backup-dir explicitly. See .env.example.
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
usage: scripts/backup_store.sh [options]

Options:
  --backup-dir DIR  Backup destination. Required (via this flag, BACKUP_DIR
                    in .env, or BACKUP_DIR in the environment).
  --part-size SIZE  split(1) part size. Default: 1000m.
  --full            Pack the whole archive, ignoring the LAST_BACKUP stamp
                    (re-baseline; part-set folder gets a -full suffix).
  --dry-run         Show what would be packed; write nothing.
  -h, --help        Show this help.

Environment overrides: BACKUP_DIR, PART_SIZE

Examples:
  scripts/backup_store.sh --dry-run
  scripts/backup_store.sh
  scripts/backup_store.sh --full
EOF
}

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUTPUT_DIR="$REPO_ROOT/output"

if [ -f "$REPO_ROOT/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  source "$REPO_ROOT/.env"
  set +a
fi

BACKUP_DIR="${BACKUP_DIR:-}"
PART_SIZE="${PART_SIZE:-1000m}"
FULL=0
DRY_RUN=0

while [ "$#" -gt 0 ]; do
  case "$1" in
    --backup-dir)
      BACKUP_DIR="${2:-}"
      [ -n "$BACKUP_DIR" ] || { echo "missing value for --backup-dir" >&2; exit 2; }
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

[ -n "$BACKUP_DIR" ] || { echo "BACKUP_DIR not set — pass --backup-dir, or set BACKUP_DIR in .env or the environment" >&2; exit 2; }
[ -d "$OUTPUT_DIR" ] || { echo "output/ not found: $OUTPUT_DIR" >&2; exit 1; }

STAMP_FILE="$BACKUP_DIR/LAST_BACKUP.txt"
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

echo "store:        $OUTPUT_DIR"
echo "destination:  $BACKUP_DIR"
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
SET_DIR="$BACKUP_DIR/parts/$SET_NAME"
if [ -e "$SET_DIR" ]; then
  echo "part-set already exists: $SET_DIR" >&2
  exit 1
fi
mkdir -p "$SET_DIR"

cp "$FILELIST" "$SET_DIR/FILELIST.txt"

{
  echo "hello-web-reader store backup part-set"
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

cat > "$BACKUP_DIR/RESTORE.md" <<EOF
# Restore the hello-web-reader output/ store

Part-sets under \`parts/\` are cumulative and append-only. To restore, extract
ALL of them in chronological (lexical) folder order into the repo root:

\`\`\`sh
cd "/path/to/hello-web-reader"
for d in "$BACKUP_DIR/parts"/*/; do
  cat "\$d"archive.tar.part-* | tar -xf - -C .
done
\`\`\`

Verify any single part-set first:

\`\`\`sh
cd "$BACKUP_DIR/parts/<set>" && shasum -a 256 -c SHA256SUMS.txt
\`\`\`

Notes:
- A \`-full\` set is a re-baseline; sets older than the NEWEST \`-full\` set are
  superseded (extracting them first is harmless but unnecessary).
- .env and cookies files are never archived — recreate them locally.
EOF

echo "done: $SET_DIR"
du -sh "$SET_DIR"
echo "verify: (cd \"$SET_DIR\" && shasum -a 256 -c SHA256SUMS.txt)"
