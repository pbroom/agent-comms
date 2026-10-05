#!/usr/bin/env bash
# UserPromptSubmit hook. Prints one line of counts when something new is addressed to this agent
# since the last time it was reported in this Claude Code session, and nothing otherwise.
# State is keyed by the session_id in the hook's JSON on stdin (see `board brief --session-from-stdin`).
# Never fails or blocks the prompt: any error (board missing, no token, bad stdin) is silent.
cd "${CLAUDE_PROJECT_DIR:-$PWD}" 2>/dev/null || exit 0
bash "$(dirname -- "${BASH_SOURCE[0]}")/board.sh" brief --agent "${AGENT_COMMS_AGENT:-claude}" \
  --session-from-stdin 2>/dev/null || true
exit 0
