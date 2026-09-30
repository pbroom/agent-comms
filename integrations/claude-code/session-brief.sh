#!/usr/bin/env bash
# SessionStart hook. Prints one line of board counts for this repo, or nothing at all.
# Never fails the session: any error (board missing, no token) is silent.
cd "${CLAUDE_PROJECT_DIR:-$PWD}" 2>/dev/null || exit 0
bash "$(dirname -- "${BASH_SOURCE[0]}")/board.sh" brief --agent claude-code 2>/dev/null || true
exit 0
