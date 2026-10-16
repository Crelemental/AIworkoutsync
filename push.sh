#!/usr/bin/env bash
# Push the year JSONL files to the Obsidian vault on the target PC.
# Safe to run often: it does nothing if nothing changed since the last successful push,
# and if the PC is off it just logs one line and tries again on the next run.
#
# Config comes from .env (see .env.example): PUSH_USER, PUSH_HOST, PUSH_DEST
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
OUT="$REPO_DIR/output"
STAMP="$OUT/.last_push"

# Read only the PUSH_* keys from .env (not sourced, so other values like passwords can't break the shell)
env_get() { grep -E "^$1=" "$REPO_DIR/.env" | tail -n1 | cut -d= -f2- | sed -e 's/^["'\'']//' -e 's/["'\'']$//'; }
PUSH_USER="$(env_get PUSH_USER)"
PUSH_HOST="$(env_get PUSH_HOST)"
PUSH_DEST="$(env_get PUSH_DEST)"

if [[ -z "$PUSH_USER" || -z "$PUSH_HOST" || -z "$PUSH_DEST" ]]; then
    echo "$(date '+%F %T') [push] PUSH_USER, PUSH_HOST and PUSH_DEST must be set in .env" >&2
    exit 1
fi

shopt -s nullglob
files=("$OUT"/*.jsonl)
(( ${#files[@]} )) || exit 0

# Skip if no file is newer than the last successful push
if [[ -f "$STAMP" ]] && [[ -z "$(find "$OUT" -maxdepth 1 -name '*.jsonl' -newer "$STAMP" -print -quit)" ]]; then
    exit 0
fi

# Mark the time before copying, so a write that lands mid-push still triggers the next push
touch "$STAMP.pending"

if scp -q -o BatchMode=yes -o ConnectTimeout=10 "${files[@]}" "$PUSH_USER@$PUSH_HOST:$PUSH_DEST"; then
    mv "$STAMP.pending" "$STAMP"
    echo "$(date '+%F %T') [push] pushed ${#files[@]} file(s) to $PUSH_HOST"
else
    rm -f "$STAMP.pending"
    echo "$(date '+%F %T') [push] $PUSH_HOST unreachable or scp failed, will retry next run"
fi
