#!/usr/bin/env bash
# Claude Code MCP launcher. The token is read from ~/.config/agent-comms/<agent>.token (mode 600)
# by `board mcp --agent`; it never appears in argv or in Claude's config.
# AGENT_COMMS_CHANNEL=1 turns on channel push (counts of new posts addressed to this agent pushed
# into an idle session). Claude only accepts it when launched with
# --dangerously-load-development-channels server:agent-comms; see the README.
set -euo pipefail
args=(mcp --agent "${AGENT_COMMS_AGENT:-claude}")
if [[ "${AGENT_COMMS_CHANNEL:-0}" == 1 ]]; then args+=(--channel); fi
exec bash "$(dirname -- "${BASH_SOURCE[0]}")/board.sh" "${args[@]}"
