#!/usr/bin/env bash
# 把整個 hello-web-reader repo 備份進 restic repository（預設放 Google Drive 同步資料夾）。
#
# 取代舊的 backup_store.sh（tar 分卷增量，只備份 output/）。改用 restic 的理由：
#
#   - 舊做法假設 output/ 是 append-only。實際上檔案會被改、被刪、被整批搬（archive
#     layout migration 就做過一次），而 tar 增量記不了刪除，只能定期 --full 重打
#     整包 22 GB 重設基準。restic 每次都存一份完整檔案樹，刪除與改名天然被記錄。
#   - 備份整個 repo 而不只是 output/：程式碼多出來的量不到 1 MB（而且 git 早就有
#     了），真正多保護到的是 .env 和 cookies 這些憑證。restic 全程加密，所以憑證
#     放進來是安全的——這正是舊腳本必須排除它們的原因（明文丟雲端）。
#
# 首次使用：
#   1. 把密碼存進 Keychain（互動輸入，不會留在 shell history）：
#        security add-generic-password -s hello-web-reader-restic -a "$USER" -w
#      密碼弄丟＝資料永久取不回來，restic 沒有後門。請同時存進密碼管理器。
#   2. scripts/backup_repo.sh --init
#   3. scripts/backup_repo.sh
#
# 之後每次就是 scripts/backup_repo.sh。
#
# ─── 換電腦／災難還原 ────────────────────────────────────────────────────
#
# 情境：新電腦，手上只有從 GitHub clone 下來的這個 repo，本機沒有任何舊資料。
# 以下是寫給那個時候的人（或替他操作的 agent）看的，不需要任何本機殘留。
#
# 需要三樣東西：
#
#   1. restic        brew install restic
#
#   2. repository    在 Google Drive 上，路徑見下方 DEFAULT_REPO。
#                    先安裝 Google Drive 桌面版、登入同一個帳號，等它同步出
#                    backup/hello-web-reader-restic/ 這個資料夾（裡面有 config、
#                    data/、index/、snapshots/、keys/）。
#
#   3. 密碼          **不在這個 repo 裡，也永遠不該在。** 它在使用者的密碼管理器
#                    （macOS「密碼」App／1Password 之類）。hello-video、
#                    hello-web-reader、hello-note 三個 repo 共用同一個密碼。
#                    restic 沒有後門也沒有救援機制——密碼遺失＝備份永久打不開，
#                    資料無法用任何方式取回。agent 不該嘗試破解或繞過，唯一的
#                    正解是請使用者去密碼管理器查。
#
# 還原步驟（R= 上面第 2 點那個資料夾的完整路徑）：
#
#   # 看有哪些時間點（會問密碼）
#   restic -r "$R" snapshots
#
#   # 還原最新的一份
#   restic -r "$R" restore latest --target ~/restored
#
#   # 只還原 podcast 音訊（這個 repo 的主體，約 22 GB）
#   restic -r "$R" restore latest --target ~/restored --include "*/output/podcast/*"
#
#   # 還原到某個特定時間點（snapshot id 從上面 snapshots 列表取得）
#   restic -r "$R" restore <snapshot-id> --target ~/restored
#
# 這個 repository 也包含 .env 和 cookies 檔（restic 全程加密，所以放得安全）。
# 還原之後這些憑證會一起回來，不需要重新申請。
#
# 檔案放回定位之後，把密碼存進新機器的 Keychain 就能繼續用這支腳本備份：
#
#   security add-generic-password -s hello-web-reader-restic -a "$USER" -w
#
# ────────────────────────────────────────────────────────────────────────
set -euo pipefail

usage() {
  cat >&2 <<'EOF'
usage: scripts/backup_repo.sh [options]

Options:
  --repo DIR     restic repository 位置。預設見 DEFAULT_REPO。
  --init         建立新的 repository（只有第一次要用）。
  --dry-run      只列出這次會備份什麼，不寫入。
  --no-prune     跳過保留策略與 prune（備份完就結束）。
  --no-check     跳過完整性檢查。
  -h, --help     顯示這段說明。

Environment overrides: RESTIC_REPOSITORY, KEYCHAIN_SERVICE

Examples:
  scripts/backup_repo.sh --init      # 第一次
  scripts/backup_repo.sh --dry-run
  scripts/backup_repo.sh
  restic -r "<repo>" snapshots       # 看有哪些時間點
EOF
}

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

DEFAULT_REPO="$HOME/Library/CloudStorage/GoogleDrive-w121211@gmail.com/My Drive/backup/hello-web-reader-restic"
RESTIC_REPOSITORY="${RESTIC_REPOSITORY:-$DEFAULT_REPO}"
KEYCHAIN_SERVICE="${KEYCHAIN_SERVICE:-hello-web-reader-restic}"

DO_INIT=0
DRY_RUN=0
DO_PRUNE=1
DO_CHECK=1

while [ "$#" -gt 0 ]; do
  case "$1" in
    --repo)
      RESTIC_REPOSITORY="${2:-}"
      [ -n "$RESTIC_REPOSITORY" ] || { echo "missing value for --repo" >&2; exit 2; }
      shift 2
      ;;
    --init) DO_INIT=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    --no-prune) DO_PRUNE=0; shift ;;
    --no-check) DO_CHECK=0; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage; exit 2 ;;
  esac
done

command -v restic >/dev/null 2>&1 || {
  echo "restic 未安裝：brew install restic" >&2
  exit 1
}

# 密碼只放 Keychain，腳本與 repo 裡都不留明文。
if ! security find-generic-password -s "$KEYCHAIN_SERVICE" -w >/dev/null 2>&1; then
  cat >&2 <<EOF
Keychain 裡找不到密碼（service: $KEYCHAIN_SERVICE）。先建立：

  security add-generic-password -s $KEYCHAIN_SERVICE -a "\$USER" -w

會互動式問密碼，不會留在 shell history。
密碼弄丟＝備份永久取不回來，請同時存進密碼管理器。
EOF
  exit 1
fi

export RESTIC_REPOSITORY
export RESTIC_PASSWORD_COMMAND="security find-generic-password -s $KEYCHAIN_SERVICE -w"

# 排除的都是「刪了能重建」的東西。output/ 下的 podcast 音訊與逐字稿是這個 repo 的
# 主體（約 22 GB），全部要備份。
EXCLUDES=(
  --exclude node_modules
  --exclude .venv
  --exclude venv
  --exclude __pycache__
  --exclude "*.pyc"
  --exclude .pytest_cache
  --exclude tmp
  --exclude .next
  --exclude .turbo
  --exclude .DS_Store
)

if [ "$DO_INIT" = "1" ]; then
  echo "建立 repository: $RESTIC_REPOSITORY"
  restic init
  echo "完成。接著跑 scripts/backup_repo.sh 做第一次備份。"
  exit 0
fi

if ! restic cat config >/dev/null 2>&1; then
  echo "repository 不存在或讀不到: $RESTIC_REPOSITORY" >&2
  echo "第一次使用請先跑: scripts/backup_repo.sh --init" >&2
  exit 1
fi

echo "repo:         $REPO_ROOT"
echo "destination:  $RESTIC_REPOSITORY"
echo

if [ "$DRY_RUN" = "1" ]; then
  restic backup "$REPO_ROOT" "${EXCLUDES[@]}" --dry-run --verbose
  exit 0
fi

# --pack-size 64：預設 pack 較小，22 GB 的 repo 會產生上千個檔案，Google Drive
# 同步大量小檔很慢。調大讓檔案數降到幾百個。
backup_out=$(restic backup "$REPO_ROOT" "${EXCLUDES[@]}" --pack-size 64 2>&1) || true
printf '%s\n' "$backup_out"

# 掃到 0 個檔案幾乎一定是權限問題（排程執行時 TCC 沒授權給 restic，或 homebrew 升級
# restic 後路徑變了、舊授權失效）。restic 這種情況不會報錯，會照樣存一個空 snapshot
# ——那比直接失敗危險得多，因為保留策略會把空的當成「今天最新」，真正有資料的那份反而
# 被輪替掉。所以掃到 0 就把剛存的空 snapshot 撤掉，然後失敗。
processed=$(printf '%s' "$backup_out" | grep -oE 'processed [0-9]+ files' | grep -oE '[0-9]+' | head -1)
if [ "${processed:-0}" -eq 0 ]; then
  snap=$(printf '%s' "$backup_out" | grep -oE 'snapshot [0-9a-f]+ saved' | grep -oE '[0-9a-f]{8}' | head -1)
  if [ -n "$snap" ]; then
    restic forget "$snap" >/dev/null 2>&1 && echo "已撤銷剛才存下的空 snapshot $snap" >&2
  fi
  echo "掃到 0 個檔案 — restic 讀不到 repo 內容。" >&2
  echo "多半是 TCC 權限：restic 需要「完全取用磁碟」授權（它是第三方二進位檔，" >&2
  echo "TCC 對它單獨判定，授權給 /bin/bash 不會生效）。" >&2
  exit 1
fi

if [ "$DO_PRUNE" = "1" ]; then
  echo
  echo "套用保留策略..."
  restic forget \
    --keep-daily 7 \
    --keep-weekly 4 \
    --keep-monthly 12 \
    --prune \
    --max-repack-size 2G
fi

if [ "$DO_CHECK" = "1" ]; then
  echo
  echo "完整性檢查..."
  restic check
fi

echo
echo "done."
echo "看有哪些時間點:  restic -r \"$RESTIC_REPOSITORY\" snapshots"
echo "還原某個時間點:  restic -r \"$RESTIC_REPOSITORY\" restore <snapshot-id> --target /path/to/restore"
