#!/bin/sh
# Synthetic runner used by unit tests; it never starts a real Wine prefix.
#
# It records each invocation as "<program> <args> (WINEPREFIX=<prefix>)" in
# $FAKE_WINE_LOG. `wineboot -u` deliberately creates the two paths the prefix
# lifecycle cares about: the initialization marker (`system.reg`) and Wine's
# Z: host-root link (`dosdevices/z:`). That lets the idempotence and Z:-strip
# tests start from the same state a real prefix would have.
set -eu

name=${0##*/}
program=$name
if [ "$name" = "wine" ] && [ "$#" -gt 0 ]; then
    program=$1
    shift
fi

if [ -n "${FAKE_WINE_LOG:-}" ]; then
    printf '%s %s (WINEPREFIX=%s)\n' "$program" "$*" "${WINEPREFIX:-}" >>"$FAKE_WINE_LOG"
fi

if [ "$program" = "wineboot" ]; then
    for arg in "$@"; do
        if [ "$arg" = "-u" ] || [ "$arg" = "--update" ]; then
            if [ -n "${WINEPREFIX:-}" ]; then
                mkdir -p "$WINEPREFIX/dosdevices"
                : >"$WINEPREFIX/system.reg"
                ln -sfn / "$WINEPREFIX/dosdevices/z:"
            fi
        fi
    done
fi

exit 0
