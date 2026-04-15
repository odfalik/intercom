#!/bin/bash
set -euo pipefail

STATE_DIR="${HOME}/.config/intercom/sessions"
mkdir -p "$STATE_DIR"

INPUT=$(cat)
SESSION_ID=$(echo "$INPUT" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('session_id',''))" 2>/dev/null)

if [ -z "$SESSION_ID" ]; then
    exit 0
fi

echo "{\"conversation_id\": \"$SESSION_ID\"}" > "$STATE_DIR/$PPID.json"
