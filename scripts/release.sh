#!/bin/sh
# dist/mcp-secret-<version>.zip: the app, signed and notarized, for colleagues to unzip and open.
# The Python core is not in it, only its wheel and pins (Contents/Resources): on first launch the
# app shows one `uv tool install` command for them (CLI.installCommand). Needs a "Developer ID
# Application" identity in the keychain and, once, `xcrun notarytool store-credentials mcp-secret`.
# SIGN_ID=- NOTARIZE=0 ./scripts/release.sh: ad-hoc signed, for checking the bundle on this Mac.
set -eu
cd "$(dirname "$0")/.."

version=$(sed -n 's/^version = "\(.*\)"/\1/p' pyproject.toml)
sign=${SIGN_ID:-$(security find-identity -v -p codesigning | sed -n 's/.*"\(Developer ID Application:[^"]*\)".*/\1/p' | head -1)}
[ -n "$sign" ] || { echo "no Developer ID Application identity in the keychain: set SIGN_ID" >&2; exit 1; }
profile=${NOTARY_PROFILE:-mcp-secret}
if [ "${NOTARIZE:-1}" = 1 ]; then
    xcrun notarytool history --keychain-profile "$profile" >/dev/null 2>&1 ||
        { echo "no notarytool profile '$profile': xcrun notarytool store-credentials $profile" >&2; exit 1; }
fi

app=dist/mcp-secret.app
rm -rf dist
mkdir -p $app/Contents/MacOS $app/Contents/Resources

uv build --wheel --out-dir $app/Contents/Resources
rm -f $app/Contents/Resources/.gitignore  # uv build drops one in its output folder
# the versions the detector was tuned on: uv tool install ignores uv.lock
uv export --frozen --no-dev --no-hashes --no-emit-project -o $app/Contents/Resources/constraints.txt

archs="--arch arm64 --arch x86_64"  # universal: the same app opens on Apple Silicon and Intel
swift build -c release --package-path apps/macos $archs
bin=$(swift build -c release --package-path apps/macos $archs --show-bin-path)
cp $bin/MCPSecret $app/Contents/MacOS/MCPSecret
cp -R $bin/MCPSecret_MCPSecret.bundle $app/Contents/Resources/
cp apps/macos/Sources/MCPSecret/Resources/AppIcon.icns $app/Contents/Resources/

cat > $app/Contents/Info.plist <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>CFBundleIdentifier</key><string>it.clait.mcp-secret</string>
	<key>CFBundleName</key><string>mcp-secret</string>
	<key>CFBundleExecutable</key><string>MCPSecret</string>
	<key>CFBundlePackageType</key><string>APPL</string>
	<key>CFBundleShortVersionString</key><string>$version</string>
	<key>CFBundleVersion</key><string>$version</string>
	<key>LSMinimumSystemVersion</key><string>15.0</string>
	<key>NSPrincipalClass</key><string>NSApplication</string>
	<key>CFBundleDevelopmentRegion</key><string>en</string>
	<key>CFBundleLocalizations</key><array><string>en</string><string>it</string></array>
	<key>CFBundleIconFile</key><string>AppIcon</string>
</dict>
</plist>
EOF

stamp=--timestamp
[ "$sign" = - ] && stamp=--timestamp=none
codesign --force --options runtime $stamp --sign "$sign" $app
codesign --verify --strict $app

if [ "${NOTARIZE:-1}" = 1 ]; then
    ditto -c -k --keepParent $app dist/notarize.zip
    xcrun notarytool submit dist/notarize.zip --keychain-profile "$profile" --wait
    xcrun stapler staple $app
    rm dist/notarize.zip
    spctl --assess --type execute -vv $app
fi
ditto -c -k --keepParent $app dist/mcp-secret-$version.zip
echo "dist/mcp-secret-$version.zip"
