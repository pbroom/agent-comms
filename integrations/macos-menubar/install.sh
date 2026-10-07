#!/usr/bin/env bash
# Copy the AgentComms.app that build.sh made into ~/Applications. Run build.sh first.
# This is the only script that writes outside this folder, and it touches nothing but ~/Applications/AgentComms.app.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
src="$here/build/AgentComms.app"
dest_dir="$HOME/Applications"
dest="$dest_dir/AgentComms.app"

if [[ ! -d "$src" ]]; then
  echo "No $src yet. Run: bash \"$here/build.sh\"" >&2
  exit 1
fi

mkdir -p "$dest_dir"
if pgrep -f "$dest/Contents/MacOS/AgentComms" >/dev/null 2>&1; then
  echo "AgentComms is running from $dest; quit it from its menu first." >&2
  exit 1
fi
rm -rf "$dest"
cp -R "$src" "$dest"
echo "Installed $dest"
echo
echo "Start it:  open \"$dest\""
echo
echo "To start it when you log in, either:"
echo "  - choose \"Launch at Login\" in its menu, or"
echo "  - open System Settings > General > Login Items, click + under \"Open at Login\" and pick"
echo "    $dest"
