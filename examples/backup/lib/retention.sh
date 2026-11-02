# Deleting old archives. The only place that removes files.

prune_old() {
    local file
    while IFS= read -r file; do
        delete_archive "$file"
    done < <(find "$DEST" -name '*.tar.gz' -mtime +"$KEEP_DAYS")
}

delete_archive() {
    rm -f -- "$1"
    log "deleted $(basename "$1")"
}
