#!/usr/bin/env bash
# Installs the agent-comms MCP server and skill for Codex CLI.
#
#   install.sh [--preapprove-board-tools]
#
# --preapprove-board-tools  also print the Codex config block that pre-approves the 27 agent-comms board tools
#     that dispatched runs get (every tool except board_resolve_attention, which stays opt-in) for EVERY Codex
#     session. Optional: dispatched runs (`board dispatch run`) already get these approvals for
#     that run only, from the -c overrides in the board.toml codex-cli runner. Use this only if you also want
#     interactive Codex sessions to skip approval for the board tools. Codex CLI 0.157 has no command that saves
#     tool approvals, so this prints the block for you to paste into ~/.codex/config.toml; it never edits that file.
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

Optional: to let interactive Codex sessions call the agent-comms board tools without asking, add this to
~/.codex/config.toml yourself. It applies to every Codex session. Dispatched runs do not need it (the
board.toml codex-cli runner approves the board tools per run). This script does not edit the file.

MSG
  for tool in board_register board_read_updates board_post board_claim_task \
              board_update_task board_release_task board_set_summary board_list_threads \
              board_list_issues board_get_issue board_create_issue board_link_issue \
              board_comment_issue board_request_progress board_request_history board_register_capabilities \
              board_route_request board_recover_request_owner board_repost_request board_configuration_status \
              board_refresh_configuration board_bind_browser_request board_browser_begin_probe board_browser_probe \
              board_browser_failure board_browser_reconnect board_browser_status; do
    printf '[mcp_servers.agent-comms.tools.%s]\napproval_mode = "approve"\n\n' "$tool"
  done
fi
