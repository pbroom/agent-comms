#!/usr/bin/env bash
# Installs the agent-comms MCP server and skill for Codex CLI.
#
#   install.sh [--preapprove-board-tools]
#
# --preapprove-board-tools  also print the Codex config block that pre-approves the 8 agent-comms board tools.
#     The dispatcher (`board dispatch run`) needs it: `codex exec` cannot ask for approval, so unapproved MCP tool
#     calls fail. Codex CLI 0.157 has no command that saves tool approvals, so this prints the block for you to
#     paste into ~/.codex/config.toml. It never edits that file. The approval applies to every Codex session.
set -euo pipefail
preapprove=0
for arg in "$@"; do
  case "$arg" in
    --preapprove-board-tools) preapprove=1 ;;
    *) echo "unknown option: $arg (usage: install.sh [--preapprove-board-tools])" >&2; exit 2 ;;
  esac
done
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
if [[ "$preapprove" == 1 ]]; then
  cat <<'MSG'

To let dispatched (non-interactive) Codex runs use the board, add this to ~/.codex/config.toml yourself.
It pre-approves only the agent-comms board tools, for every Codex session. This script does not edit the file.

MSG
  for tool in board_register board_read_updates board_post board_claim_task board_update_task \
              board_release_task board_set_summary board_list_threads; do
    printf '[mcp_servers.agent-comms.tools.%s]\napproval_mode = "approve"\n\n' "$tool"
  done
fi
