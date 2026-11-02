# Settings and preflight checks.

load_config() {
    DEST=${DEST:-./archives}
    KEEP_DAYS=${KEEP_DAYS:-7}
    mkdir -p "$DEST" || die "cannot create $DEST"
    log "archives go to $DEST, kept for $KEEP_DAYS days"
}

require_tools() {
    local tool missing=()
    for tool in "$@"; do
        command -v "$tool" >/dev/null || missing+=("$tool")
    done
    if (( ${#missing[@]} )); then
        die "missing tools: ${missing[*]}"
    fi
}
