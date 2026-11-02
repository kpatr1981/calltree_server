#!/usr/bin/env bash
# backup.sh - archive a directory, list archives, restore one, prune old ones.
#
#   ./backup.sh backup  SRC        create DEST/<name>-<timestamp>.tar.gz
#   ./backup.sh list               show archives in DEST
#   ./backup.sh restore ARCHIVE    extract ARCHIVE into ./restore
#   ./backup.sh prune              delete archives older than KEEP_DAYS
#
# DEST defaults to ./archives, KEEP_DAYS to 7. Set NOTIFIER to a plugin file
# (for example plugins/notify-mail.sh) to get a message when a job finishes.
set -euo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
source "$HERE/lib/log.sh"
source "$HERE/lib/config.sh"
source "$HERE/lib/archive.sh"
source "$HERE/lib/retention.sh"

NOTIFIER=${NOTIFIER:-}
if [[ -n $NOTIFIER ]]; then
    source "$NOTIFIER"
else
    notify() { :; }
fi

usage() {
    sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'
}

cleanup() {
    local status=$1
    rm -f "${WORK_FILE:-}"
    if (( status != 0 )); then
        warn "finished with status $status"
    fi
}

run_step() {
    local title=$1; shift
    log "$title"
    "$@" || die "$title failed"
}

main() {
    [[ $# -ge 1 ]] || { usage; exit 2; }
    load_config
    trap 'cleanup $?' EXIT

    case $1 in
        backup)
            [[ $# -eq 2 ]] || die "backup needs a source directory"
            run_step "Checking tools" require_tools tar gzip
            run_step "Archiving $2" make_archive "$2"
            run_step "Verifying" verify_archive "$LAST_ARCHIVE"
            notify "backup of $2 done: $LAST_ARCHIVE"
            ;;
        list)
            list_archives
            ;;
        restore)
            [[ $# -eq 2 ]] || die "restore needs an archive"
            verify_archive "$2" && restore_archive "$2"
            ;;
        prune)
            prune_old
            notify "pruned archives older than $KEEP_DAYS days"
            ;;
        -h|--help)
            usage
            ;;
        *)
            die "unknown command: $1"
            ;;
    esac
}

main "$@"
