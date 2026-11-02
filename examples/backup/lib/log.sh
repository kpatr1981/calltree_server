# Logging helpers. Everything goes to stderr so stdout stays clean.

log() {
    printf '%s  %s\n' "$(date +%H:%M:%S)" "$*" >&2
}

warn() {
    log "WARNING: $*"
}

die() {
    log "ERROR: $*"
    exit 1
}
