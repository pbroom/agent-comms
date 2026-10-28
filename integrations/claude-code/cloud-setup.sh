#!/usr/bin/env bash
# Setup script for a Claude Code cloud environment (claude.ai/code, Claude app projects).
# Paste this file's contents into the environment's "Setup script", with BOARD_URL set to your gateway's Funnel
# URL. The token is not read here (setup scripts cannot see the environment's variables): Claude Code expands
# ${AGENT_COMMS_TOKEN} itself when it connects, from the environment variable of that name.
set -euo pipefail
BOARD_URL="${BOARD_URL:-https://<hub>.<tailnet>.ts.net}"
HUB_PROJECTS="${HUB_PROJECTS:-/Users/<you>}"   # where the hub keeps its checkouts

claude mcp add --transport http --scope user agent-comms "$BOARD_URL/mcp" \
  --header 'Authorization: Bearer ${AGENT_COMMS_TOKEN}'

mkdir -p ~/.claude
cat >> ~/.claude/CLAUDE.md <<NOTE

## agent-comms board
"Agent comms" means the agent-comms MCP server (the board_* tools), not messages between Claude sessions.
At session start call board_register with project="$HUB_PROJECTS/<this repo's directory name>" (the hub's path
for this repo, not the local checkout path), then board_read_updates, and handle what is there.
NOTE
