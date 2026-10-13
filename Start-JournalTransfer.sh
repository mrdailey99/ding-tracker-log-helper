#!/usr/bin/env sh
# Linux/Steam Deck source launcher; Python 3 and Tk are required.
cd -- "$(dirname -- "$0")" || exit 1
exec python3 journal_transfer.py "$@"
