# Optional notifier: load it with NOTIFIER=plugins/notify-mail.sh.

notify() {
    if command -v mail >/dev/null; then
        mail -s "backup.sh" "${MAILTO:-$USER}" <<<"$1"
    else
        warn "mail not installed; message was: $1"
    fi
}
