#!/usr/bin/env bash
# Stop and SessionStart hook with "asyncRewake": true (install.sh --wake). Claude Code runs it in the background;
# `board wait` long-polls the hub, and when a board post addressed to this agent should wake the session it exits 2
# with the hub's wake text, which Claude shows the woken session. Exit 2 is passed on only with text of that fixed
# form ("agent-comms: ..."): anything else that could exit 2 (uv, argparse, a broken install) must never wake the
# session, or each wake would end a turn that starts the hook that wakes it again. Every other outcome is exit 0.
trap 'exit 0' TERM HUP INT     # Claude ends the hook so at its timeout and when the session ends: no wake
here="$(dirname -- "${BASH_SOURCE[0]}")"
text="$(bash "$here/board.sh" wait --agent "${AGENT_COMMS_AGENT:-claude}" 2>&1 >/dev/null)"
rc=$?
if [[ $rc -eq 2 && "$text" =~ ^agent-comms:\ [[:print:]]+$ ]]; then
  printf '%s\n' "$text" >&2
  exit 2
fi
exit 0
