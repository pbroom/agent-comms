#!/bin/sh
# Private OpenAI MCP tunnel by default; explicit --transport cloudflare for public fallback.
# Foreground supervisor. In another terminal: scripts/chatgpt-tunnel.sh stop
set -eu
umask 077
project_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
exec uv run --project "$project_dir" python "$project_dir/integrations/chatgpt/tunnel.py" "$@"
