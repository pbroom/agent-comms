#!/usr/bin/env bash
# Run `board` from this checkout against the one real board, whatever directory the agent is in.
set -euo pipefail
export AGENT_COMMS_HOME="${AGENT_COMMS_HOME:-$HOME/agent-comms}"
board_code="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
exec uv run --quiet --frozen --project "$board_code" board "$@"
