#!/usr/bin/env bash
# The smallest useful example: one file, three functions.
set -euo pipefail

greet() {
    local name=$1
    if [[ -z $name ]]; then
        shout "nobody to greet"
        return 1
    fi
    echo "Hello, $name"
}

shout() {
    echo "$1" | tr "[:lower:]" "[:upper:]" >&2
}

main() {
    for name in "$@"; do
        greet "$name" || shout "skipped an empty name"
    done
}

main "$@"
