#!/usr/bin/env bash
# Claude Code MCP launcher. The token is read from ~/.config/agent-comms/<agent>.token (mode 600)
# by `board mcp --agent`; it never appears in argv or in Claude's config.
set -euo pipefail
exec bash "$(dirname -- "${BASH_SOURCE[0]}")/board.sh" mcp --agent "${AGENT_COMMS_AGENT:-claude}"
