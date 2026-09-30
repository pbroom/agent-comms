#!/usr/bin/env bash
set -euo pipefail
# Code can run from an isolated worktree; identity and data stay on the real board.
export AGENT_COMMS_HOME="${AGENT_COMMS_HOME:-$HOME/agent-comms}"
integration_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
board_code="$(cd -- "$integration_dir/../.." && pwd)"
exec uv run --project "$board_code" python "$integration_dir/stdio.py"
