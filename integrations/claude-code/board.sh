#!/usr/bin/env bash
# Run `board` from this checkout against the board home, whatever directory the agent is in.
# AGENT_COMMS_HOME defaults to this checkout, matching `board init` (install.sh records it explicitly).
set -euo pipefail
board_code="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
export AGENT_COMMS_HOME="${AGENT_COMMS_HOME:-$board_code}"
exec uv run --quiet --frozen --project "$board_code" board "$@"
