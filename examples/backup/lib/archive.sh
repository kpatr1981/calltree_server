# Creating, checking, listing and restoring archives.

make_archive() {
    local src=$1 name
    [[ -d $src ]] || die "not a directory: $src"
    name=$(basename "$src")-$(date +%Y%m%d-%H%M%S).tar.gz
    WORK_FILE=$DEST/.$name.part
    tar -czf "$WORK_FILE" -C "$(dirname "$src")" "$(basename "$src")"
    mv "$WORK_FILE" "$DEST/$name"
    LAST_ARCHIVE=$DEST/$name
    log "wrote $LAST_ARCHIVE"
}

verify_archive() {
    local file=$1
    [[ -f $file ]] || die "no such archive: $file"
    tar -tzf "$file" >/dev/null || { warn "$file is damaged"; return 1; }
}

list_archives() {
    local file
    for file in "$DEST"/*.tar.gz; do
        [[ -e $file ]] || { log "no archives yet"; return 0; }
        printf '%s\t%s\n' "$(du -h "$file" | cut -f1)" "$(basename "$file")"
    done
}

restore_archive() {
    local file=$1
    mkdir -p ./restore
    tar -xzf "$file" -C ./restore
    log "restored $(basename "$file") into ./restore"
}
