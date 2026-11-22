#!/usr/bin/env bash
# SessionStart and Stop hook with "async": true in $CODEX_HOME/hooks.json (install.sh --wake). Codex runs it in the
# background with the hook's JSON on stdin (session_id is the thread id, cwd the thread's directory). `board wait
# --client codex` long-polls the hub, and when a board post addressed to this agent should wake the thread it puts the
# hub's wake text in the thread's queue (`codex queue`, run with the binary of the Codex process that ran this hook, as
# the OS reports it); the Codex process that has the thread loaded starts a turn from it. This script always exits 0:
# an async hook's exit code wakes nothing. Codex kills its process group when the thread is unloaded and at its
# timeout; `board wait` also ends when that Codex process is gone. A run the dispatcher, a node or the session host
# started (AGENT_COMMS_MANAGED=1) never waits: only a thread you opened yourself does. Its stdout stays empty (Codex
# reads a hook's stdout); a one-line note, when it cannot queue, goes to stderr.
[[ "${AGENT_COMMS_MANAGED:-}" == 1 ]] && exit 0
# The checkout beside this script's real file, through any symlinks to it (as stdio.sh does).
src="${BASH_SOURCE[0]}"
while [[ -L "$src" ]]; do
  dir="$(cd -P -- "$(dirname -- "$src")" && pwd)" || exit 0
  src="$(readlink -- "$src")" || exit 0
  [[ "$src" == /* ]] || src="$dir/$src"
done
board_code="$(cd -P -- "$(dirname -- "$src")/../.." && pwd)" || exit 0
export AGENT_COMMS_HOME="${AGENT_COMMS_HOME:-$HOME/agent-comms}"
# $1: the token file install.sh --wake checked against this Codex home's board MCP server, the waiter's only token
# source (an argument in the hook's own command line, so nothing in the environment can stand in for it).
uv run --quiet --frozen --no-dev --project "$board_code" board wait --client codex \
  --agent "${AGENT_COMMS_AGENT:-codex}" ${1:+--token-file "$1"} >/dev/null
exit 0
