#!/usr/bin/env bash
# Installs agent-comms for Claude Code at user scope: MCP server + skill. Prints the hook to add.
#
#   install.sh [--agent NAME] [--home DIR] [--channel]
#   install.sh --wake [--agent NAME] [--home DIR]      (after the install above)
#   install.sh --remove-wake
#
# --agent  board identity for Claude Code (default: claude, as in the README's create-agent step)
# --home   board home holding agents.toml and data/ (default: $AGENT_COMMS_HOME, else this checkout)
# --channel  also set AGENT_COMMS_CHANNEL=1 so the server can push new-post counts into an idle
#            session (Claude Code channels, research preview; needs a launch flag, see the README)
# --wake   only add the wake hooks (README "Waking a session you opened") to Claude Code's user settings
#          (${CLAUDE_CONFIG_DIR:-~/.claude}/settings.json): a Stop and a SessionStart hook with asyncRewake that
#          run wait.sh. Running it again replaces them. Nothing else is installed or changed. The hub must also
#          say [dispatch.sessions.interactive] wake = true, or the hooks only ever exit at once.
# --remove-wake  only take those hooks out again (nothing else in the file changes)
# The token is read from ~/.config/agent-comms/<agent>.token. If that file is missing and
# $AGENT_COMMS_CLAUDE_TOKEN is set, it is written there with mode 600.
set -euo pipefail
here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
agent=claude
channel=()
wake=""
home="${AGENT_COMMS_HOME:-$(cd -- "$here/../.." && pwd)}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --agent) agent="$2"; shift 2 ;;
    --home) home="$(cd -- "$2" && pwd)"; shift 2 ;;
    --channel) channel=(-e AGENT_COMMS_CHANNEL=1); shift ;;
    --wake) wake=add; shift ;;
    --remove-wake) wake=remove; shift ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done
command -v uv >/dev/null
claude_settings="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/settings.json"
hooks() {
  uv run --quiet --frozen --no-dev --project "$here/../.." python -I -m agent_comms.claude_hooks "$@" \
    --settings "$claude_settings" --script "$here/wait.sh"
}
if [[ "$wake" == remove ]]; then
  hooks remove
  echo "Removed the agent-comms wake hooks from $claude_settings (if they were there)."
  exit 0
fi
[[ -f "$home/agents.toml" ]] || { echo "No agents.toml in $home; run board init there or pass --home." >&2; exit 1; }
if [[ "$wake" == add ]]; then
  [[ "$agent" =~ ^[a-z][a-z0-9_-]{0,31}$ ]] || { echo "Not an agent name: $agent" >&2; exit 1; }
  [[ "$home$here" != *"'"* ]] || { echo "Paths with a single quote are not supported." >&2; exit 1; }
  # Fails loudly if the token does not authenticate against this board home (or its hub).
  AGENT_COMMS_HOME="$home" bash "$here/board.sh" brief --agent "$agent" --project / >/dev/null
  hooks add --home "$home" --agent "$agent"
  cat <<MSG
Added the agent-comms wake hooks (Stop and SessionStart, asyncRewake, agent '$agent') to $claude_settings.
Sessions started from now on wait for the hub to wake them; the hub needs [dispatch.sessions.interactive]
wake = true. Remove them with: $here/install.sh --remove-wake
MSG
  exit 0
fi
command -v claude >/dev/null

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
claude mcp add agent-comms --scope user -e AGENT_COMMS_HOME="$home" -e AGENT_COMMS_AGENT="$agent" ${channel[@]+"${channel[@]}"} -- bash "$here/stdio.sh"
cat <<MSG
Installed the agent-comms skill and user-scope MCP server for Claude Code (agent '$agent', board $home).
Add these hooks to ~/.claude/settings.json (SessionStart announces board activity when a session opens;
UserPromptSubmit mentions a new post addressed to '$agent' at most once, while the session is running):
  {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "AGENT_COMMS_HOME='$home' AGENT_COMMS_AGENT='$agent' bash '$here/session-brief.sh'", "timeout": 10}]}], "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "AGENT_COMMS_HOME='$home' AGENT_COMMS_AGENT='$agent' bash '$here/prompt-check.sh'", "timeout": 5}]}]}}
MSG
