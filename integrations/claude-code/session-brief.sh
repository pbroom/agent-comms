#!/usr/bin/env bash
# SessionStart hook. Prints one line of board counts for this repo, or nothing at all.
# Never fails the session: any error (board missing, no token) is silent.
# --seed records this session's high-water mark (the session_id comes from the hook's JSON on stdin)
# so prompt-check.sh does not repeat what this line already announced.
cd "${CLAUDE_PROJECT_DIR:-$PWD}" 2>/dev/null || exit 0
bash "$(dirname -- "${BASH_SOURCE[0]}")/board.sh" brief --agent "${AGENT_COMMS_AGENT:-claude}" \
  --session-from-stdin --seed 2>/dev/null || true
exit 0
