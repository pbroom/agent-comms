#!/usr/bin/env bash
# Build AgentComms.app into ./build next to this script. Installs nothing: see install.sh for that.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$here"

swift build -c release --product AgentCommsMenuBar
bin="$(swift build -c release --product AgentCommsMenuBar --show-bin-path)/AgentCommsMenuBar"

app="$here/build/AgentComms.app"
rm -rf "$app"
mkdir -p "$app/Contents/MacOS" "$app/Contents/Resources"
cp "$bin" "$app/Contents/MacOS/AgentComms"
cp "$here/Resources/Info.plist" "$app/Contents/Info.plist"
printf 'APPL????' > "$app/Contents/PkgInfo"

plutil -lint "$app/Contents/Info.plist"

# Ad-hoc signature so the bundle (and its Info.plist) is sealed; Launch at Login needs a signed app.
if command -v codesign >/dev/null 2>&1; then
  codesign --force --sign - --identifier dev.agentcomms.menubar "$app"
  codesign --verify --strict "$app"
fi

echo "Built $app"
echo "Run it once:  open \"$app\""
echo "Install it:   bash \"$here/install.sh\"   (copies it to ~/Applications)"
