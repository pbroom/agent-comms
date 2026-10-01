#!/usr/bin/env bash
# Installs agent-comms for Claude Code at user scope: MCP server + skill. Prints the hook to add.
#
#   install.sh [--agent NAME] [--home DIR]
#
# --agent  board identity for Claude Code (default: claude, as in the README's create-agent step)
# --home   board home holding agents.toml and data/ (default: $AGENT_COMMS_HOME, else this checkout)
# The token is read from ~/.config/agent-comms/<agent>.token. If that file is missing and
# $AGENT_COMMS_CLAUDE_TOKEN is set, it is written there with mode 600.
set -euo pipefail
here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
agent=claude
home="${AGENT_COMMS_HOME:-$(cd -- "$here/../.." && pwd)}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --agent) agent="$2"; shift 2 ;;
    --home) home="$(cd -- "$2" && pwd)"; shift 2 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done
command -v claude >/dev/null
command -v uv >/dev/null
[[ -f "$home/agents.toml" ]] || { echo "No agents.toml in $home; run board init there or pass --home." >&2; exit 1; }

token_dir="$HOME/.config/agent-comms"
token_file="$token_dir/$agent.token"
if [[ ! -f "$token_file" && -n "${AGENT_COMMS_CLAUDE_TOKEN:-}" ]]; then
  mkdir -p "$token_dir" && chmod 700 "$token_dir"
  (umask 077 && printf '%s\n' "$AGENT_COMMS_CLAUDE_TOKEN" > "$token_file")
fi
[[ -f "$token_file" ]] || { echo "Missing $token_file (mode 600) holding the '$agent' token." >&2; exit 1; }
# Fails loudly if the token does not authenticate against this board home.
AGENT_COMMS_HOME="$home" bash "$here/board.sh" brief --agent "$agent" --project / >/dev/null

skill_dir="$HOME/.claude/skills/agent-comms"
if [[ -e "$skill_dir" ]] && ! diff -q "$here/skill/SKILL.md" "$skill_dir/SKILL.md" >/dev/null 2>&1; then
  echo "Existing skill at $skill_dir differs; reconcile it first." >&2; exit 1
fi
mkdir -p "$skill_dir"
cp "$here/skill/SKILL.md" "$skill_dir/SKILL.md"

if claude mcp get agent-comms >/dev/null 2>&1; then
  echo "An 'agent-comms' MCP server is already configured; remove it first: claude mcp remove -s user agent-comms" >&2
  exit 1
fi
claude mcp add --scope user -e AGENT_COMMS_HOME="$home" -e AGENT_COMMS_AGENT="$agent" agent-comms -- bash "$here/stdio.sh"
cat <<MSG
Installed the agent-comms skill and user-scope MCP server for Claude Code (agent '$agent', board $home).
Add this SessionStart hook to ~/.claude/settings.json:
  {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "AGENT_COMMS_HOME='$home' AGENT_COMMS_AGENT='$agent' bash '$here/session-brief.sh'", "timeout": 10}]}]}}
MSG
