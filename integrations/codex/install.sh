#!/usr/bin/env bash
# Installs the agent-comms MCP server and skill for Codex CLI.
# The MCP server and the hooks go into the Codex home in force ($CODEX_HOME when it is set, else ~/.codex: `codex mcp
# add` writes its config.toml). The skill goes to ~/.agents/skills, which Codex reads whatever its home is.
#
#   install.sh [--preapprove-board-tools]
#   install.sh --wake [--agent NAME]     (after the install above)
#   install.sh --remove-wake
#
# --wake  only add the wake hooks (README "Waking a Codex thread you opened") to Codex's hooks file
#     ($CODEX_HOME/hooks.json, else ~/.codex/hooks.json; config.toml is not touched): an async SessionStart and Stop
#     hook running wait.sh, which waits for the hub to wake the thread and then queues the wake text in it
#     (`codex queue`). The hub must also say [dispatch.sessions.interactive] wake = true, or the hooks end at once.
#     Codex runs a new hook only after you have reviewed and trusted it.
# --remove-wake  only take those hooks out again (nothing else in the file changes)
#
# --preapprove-board-tools  also print the Codex config block that pre-approves the 27 agent-comms board tools
#     that dispatched runs get (every tool except board_resolve_attention, which stays opt-in) for EVERY Codex
#     session. Optional: dispatched runs (`board dispatch run`) already get these approvals for
#     that run only, from the -c overrides in the board.toml codex-cli runner. Use this only if you also want
#     interactive Codex sessions to skip approval for the board tools. Codex CLI 0.157 has no command that saves
#     tool approvals, so this prints the block for you to paste into ~/.codex/config.toml; it never edits that file.
set -euo pipefail
preapprove=0
wake=""
agent=codex
while [[ $# -gt 0 ]]; do
  case "$1" in
    --preapprove-board-tools) preapprove=1; shift ;;
    --wake) wake=add; shift ;;
    --remove-wake) wake=remove; shift ;;
    --agent) agent="${2:-}"; shift 2 ;;
    *) echo "unknown option: $1" >&2
       echo "usage: install.sh [--preapprove-board-tools] | --wake [--agent NAME] | --remove-wake" >&2
       exit 2 ;;
  esac
done
integration_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
codex_dir="${CODEX_HOME:-$HOME/.codex}"
hooks_file="$codex_dir/hooks.json"
hooks() {
  uv run --quiet --frozen --no-dev --project "$integration_dir/../.." python -I -m agent_comms.codex_hooks "$@" \
    --hooks-file "$hooks_file" --script "$integration_dir/wait.sh"
}
if [[ "$wake" == remove ]]; then
  hooks remove >/dev/null
  cat <<MSG
Removed the agent-comms wake hooks from $hooks_file (if they were there). Every hook group stays where it was
(an emptied one is left in place), so Codex keeps its trust in your other hooks.
MSG
  exit 0
fi
skill_dir="$HOME/.agents/skills/agent-comms"
board_home="${AGENT_COMMS_HOME:-$HOME/agent-comms}"
if [[ "$board_home" != /* ]]; then
  echo "AGENT_COMMS_HOME must be an absolute path to the canonical board." >&2
  exit 1
fi
if [[ "$wake" == add ]]; then
  [[ "$agent" =~ ^[a-z][a-z0-9_-]{0,31}$ ]] || { echo "Not an agent name: $agent" >&2; exit 1; }
  [[ "$board_home$integration_dir" != *"'"* ]] || { echo "Paths with a single quote are not supported." >&2; exit 1; }
  { set +x; } 2>/dev/null     # no tracing here, inherited or not: nothing below may echo a credential
  py() {
    AGENT_COMMS_HOME="$board_home" uv run --quiet --frozen --no-dev --project "$integration_dir/../.." python -I "$@"
  }
  # The board MCP server of this Codex home must use the same board, and a token file that signs in as <agent> on it
  # (read-only; agent_comms/token_check.py). Its configuration goes straight from `codex mcp get` into the checker's
  # stdin, never into a variable or an argument (it can hold a literal token); the checker prints only the token
  # file's path, which the hooks then use as their only token source.
  token_file="$( { codex mcp get agent-comms --json 2>/dev/null || true; } \
    | py -m agent_comms.codex_hooks check-mcp --board-home "$board_home" --agent "$agent")" || exit 1
  result="$(hooks add --home "$board_home" --agent "$agent" --token-file "$token_file")" || exit 1
  cat <<MSG
Added the agent-comms wake hooks (SessionStart and Stop, async, agent '$agent') to $hooks_file ($result).
The hub also needs [dispatch.sessions.interactive] wake = true.

Codex runs a new or changed hook only after you trust it. In the Codex CLI, the next start shows a hooks review:
trust the two agent-comms hooks there (Codex records that in $codex_dir/config.toml under [hooks.state]). In the
desktop app, check its hooks settings for an entry to trust (not verified here). Until then the hooks do not run.
Threads opened (or resumed) after that wait for the hub to wake them.
Remove them with: $integration_dir/install.sh --remove-wake
MSG
  exit 0
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
  cat <<MSG

Optional: to let interactive Codex sessions call the agent-comms board tools without asking, add this to
$codex_dir/config.toml yourself. It applies to every Codex session. Dispatched runs do not need it (the
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
