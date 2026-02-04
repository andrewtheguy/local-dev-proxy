#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

SESSION_NAME="caddy"
LAYOUT="layouts/caddy.kdl"

# Check if session exists and its status
SESSION_INFO=$(zellij list-sessions -n 2>/dev/null | grep "^${SESSION_NAME} " || true)

if [[ -n "$SESSION_INFO" ]]; then
    if echo "$SESSION_INFO" | grep -q "EXITED"; then
        # Session exists but is exited, delete and recreate
        zellij delete-session "$SESSION_NAME"
        exec zellij -s "$SESSION_NAME" -n "$LAYOUT"
    else
        # Session exists and is active, attach to it
        exec zellij attach "$SESSION_NAME"
    fi
else
    # Session doesn't exist, create it with the layout
    exec zellij -s "$SESSION_NAME" -n "$LAYOUT"
fi
