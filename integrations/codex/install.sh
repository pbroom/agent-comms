#!/usr/bin/env bash
set -euo pipefail
integration_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
skill_dir="$HOME/.agents/skills/agent-comms"
board_home="${AGENT_COMMS_HOME:-$HOME/agent-comms}"
if [[ "$board_home" != /* ]]; then
  echo "AGENT_COMMS_HOME must be an absolute path to the canonical board." >&2
  exit 1
fi
command -v codex >/dev/null
command -v uv >/dev/null
# Preserve any differently authored skill. Re-running with identical content is safe.
if [[ -e "$skill_dir" ]]; then
  if ! diff -qr "$integration_dir/skill" "$skill_dir" >/dev/null; then
    echo "Existing skill differs: $skill_dir. Preserve or reconcile it before installation." >&2
    exit 1
  fi
fi
# The CLI changes only this MCP entry; token values never appear in argv/config.
codex mcp add agent-comms --env "AGENT_COMMS_HOME=$board_home" -- bash "$integration_dir/stdio.sh"
mkdir -p "$skill_dir/agents"
cp "$integration_dir/skill/SKILL.md" "$skill_dir/SKILL.md"
cp "$integration_dir/skill/agents/openai.yaml" "$skill_dir/agents/openai.yaml"
echo "Installed agent-comms MCP and skill. Start a new Codex session to load the server."
