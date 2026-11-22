#!/usr/bin/env bash
set -euo pipefail
# Code can run from an isolated worktree; identity and data stay on the real board.
export AGENT_COMMS_HOME="${AGENT_COMMS_HOME:-$HOME/agent-comms}"
# This script's real location, through any symlinks to it (bash keeps the path it was given in BASH_SOURCE), so that
# stdio.py and the checkout are found beside the real file wherever the launcher is linked from.
src="${BASH_SOURCE[0]}"
while [[ -L "$src" ]]; do
  dir="$(cd -P -- "$(dirname -- "$src")" && pwd)"
  src="$(readlink -- "$src")"
  [[ "$src" == /* ]] || src="$dir/$src"
done
integration_dir="$(cd -P -- "$(dirname -- "$src")" && pwd)"
board_code="$(cd -- "$integration_dir/../.." && pwd)"
exec uv run --no-dev --project "$board_code" python "$integration_dir/stdio.py"
