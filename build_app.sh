#!/bin/sh
# Builds "Pack Panel.app" next to this script. Re-run after editing packpanel.py, ui/ or native/.
# Uses the native Swift panel when swiftc is available, else a plain launcher that opens a browser window.
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
APP="$HERE/Pack Panel.app"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$HERE/packpanel.py" "$APP/Contents/Resources/"
cp -R "$HERE/ui" "$APP/Contents/Resources/ui"

if command -v swiftc >/dev/null 2>&1; then
  swiftc -O -target "$(uname -m)-apple-macos12.0" -framework AppKit -framework WebKit \
    -o "$APP/Contents/MacOS/PackPanel" "$HERE/native/PackPanel.swift"
  KIND="native panel"
else
  cat > "$APP/Contents/MacOS/PackPanel" <<'SH'
#!/bin/sh
DIR="$(cd "$(dirname "$0")/../Resources" && pwd)"
for P in /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3; do
  if [ -x "$P" ]; then exec "$P" -I "$DIR/packpanel.py"; fi
done
/usr/bin/osascript -e 'display alert "Pack Panel needs Python 3" message "Open Terminal and run: xcode-select --install"'
SH
  chmod +x "$APP/Contents/MacOS/PackPanel"
  KIND="browser launcher (swiftc not found)"
fi

cat > "$APP/Contents/Info.plist" <<'PL'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>Pack Panel</string>
  <key>CFBundleDisplayName</key><string>Pack Panel</string>
  <key>CFBundleIdentifier</key><string>local.packpanel</string>
  <key>CFBundleVersion</key><string>1.5.0</string>
  <key>CFBundleShortVersionString</key><string>1.5.0</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleExecutable</key><string>PackPanel</string>
  <key>CFBundleIconFile</key><string>PackPanel</string>
  <key>LSMinimumSystemVersion</key><string>12.0</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSAppTransportSecurity</key><dict><key>NSAllowsLocalNetworking</key><true/></dict>
</dict></plist>
PL

# Icon from ui/icon.svg (skipped quietly if this macOS can't rasterise SVG)
TMP="$(mktemp -d)"; mkdir "$TMP/PackPanel.iconset"
if /usr/bin/sips -s format png -z 1024 1024 "$HERE/ui/icon.svg" --out "$TMP/icon.png" >/dev/null 2>&1; then
  for s in 16 32 128 256 512; do
    /usr/bin/sips -z $s $s "$TMP/icon.png" --out "$TMP/PackPanel.iconset/icon_${s}x${s}.png" >/dev/null
    d=$((s*2)); /usr/bin/sips -z $d $d "$TMP/icon.png" --out "$TMP/PackPanel.iconset/icon_${s}x${s}@2x.png" >/dev/null
  done
  /usr/bin/iconutil -c icns "$TMP/PackPanel.iconset" -o "$APP/Contents/Resources/PackPanel.icns" || true
fi
rm -rf "$TMP"
/usr/bin/codesign --force --deep --sign - "$APP" >/dev/null 2>&1 || true
echo "Built $APP ($KIND)"
