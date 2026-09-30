#!/usr/bin/env bash
# Installs agent-comms for Claude Code at user scope: MCP server + skill. Prints the hook to add.
set -euo pipefail
here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
skill_dir="$HOME/.claude/skills/agent-comms"
command -v claude >/dev/null
command -v uv >/dev/null
[[ -f "$HOME/.config/agent-comms/claude-code.token" ]] || {
  echo "Missing ~/.config/agent-comms/claude-code.token (mode 600)." >&2; exit 1; }
if [[ -e "$skill_dir" ]] && ! diff -q "$here/skill/SKILL.md" "$skill_dir/SKILL.md" >/dev/null 2>&1; then
  echo "Existing skill at $skill_dir differs; reconcile it first." >&2; exit 1
fi
mkdir -p "$skill_dir"
cp "$here/skill/SKILL.md" "$skill_dir/SKILL.md"
claude mcp get agent-comms >/dev/null 2>&1 || claude mcp add --scope user agent-comms -- bash "$here/stdio.sh"
cat <<MSG
Installed the agent-comms skill and user-scope MCP server for Claude Code.
Add this SessionStart hook to ~/.claude/settings.json:
  {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "bash $here/session-brief.sh", "timeout": 10}]}]}}
MSG
