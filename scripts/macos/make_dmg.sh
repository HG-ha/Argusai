#!/usr/bin/env bash
# Build a drag-to-Applications DMG (background + icon layout).
#
# Usage:
#   bash scripts/macos/make_dmg.sh <ArgusAI.app> <out.dmg> [volume name]
#
# Optional env:
#   ARGUSAI_DMG_BACKGROUND  PNG used as the Finder window background
#   ARGUSAI_DMG_README      text file copied onto the volume as Read Me.txt
#   ARGUSAI_DMG_ICNS        volume icon (.icns)

set -euo pipefail

if [[ "$(uname -s)" != "Darwin" ]]; then
    echo "make_dmg.sh must run on macOS (hdiutil + Finder)." >&2
    exit 1
fi

APP_PATH="${1:-}"
DMG_PATH="${2:-}"
VOL_NAME="${3:-ArgusAI}"
if [[ -z "$APP_PATH" || -z "$DMG_PATH" ]]; then
    echo "Usage: $0 <App.app> <out.dmg> [volume name]" >&2
    exit 2
fi
[[ -d "$APP_PATH" ]] || { echo "Missing app bundle: $APP_PATH" >&2; exit 1; }
command -v hdiutil >/dev/null 2>&1 || { echo "hdiutil not found" >&2; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BG_SRC="${ARGUSAI_DMG_BACKGROUND:-$SCRIPT_DIR/dmg-ui/background.png}"
README_SRC="${ARGUSAI_DMG_README:-$SCRIPT_DIR/../packaging/readme-dmg.txt}"
ICNS_SRC="${ARGUSAI_DMG_ICNS:-}"
if [[ -z "$ICNS_SRC" ]]; then
    for candidate in \
        "$SCRIPT_DIR/../../assets/brand/argusai-icon.icns" \
        "$APP_PATH/Contents/Resources/argusai-icon.icns"
    do
        if [[ -f "$candidate" ]]; then
            ICNS_SRC="$candidate"
            break
        fi
    done
fi

STAGE="$(mktemp -d "${TMPDIR:-/tmp}/argusai-dmg.XXXXXX")"
RW_DMG="$(mktemp "${TMPDIR:-/tmp}/argusai-dmg-rw.XXXXXX").dmg"
MOUNT=""
cleanup() {
    if [[ -n "$MOUNT" && -d "$MOUNT" ]]; then
        hdiutil detach "$MOUNT" -quiet >/dev/null 2>&1 || \
            hdiutil detach "$MOUNT" -force -quiet >/dev/null 2>&1 || true
    fi
    rm -rf "$STAGE" "$RW_DMG"
}
trap cleanup EXIT

mkdir -p "$STAGE/.background"
APP_NAME="$(basename "$APP_PATH")"
if ! cp -Rc "$APP_PATH" "$STAGE/$APP_NAME" 2>/dev/null; then
    cp -R "$APP_PATH" "$STAGE/$APP_NAME"
fi
ln -sf /Applications "$STAGE/Applications"
if [[ -f "$README_SRC" ]]; then
    cp "$README_SRC" "$STAGE/Read Me.txt"
fi
if [[ -f "$BG_SRC" ]]; then
    cp "$BG_SRC" "$STAGE/.background/background.png"
fi
if [[ -n "$ICNS_SRC" && -f "$ICNS_SRC" ]]; then
    cp "$ICNS_SRC" "$STAGE/.VolumeIcon.icns"
fi

# HFS+ keeps Finder background pictures more reliably than APFS disk images.
hdiutil create \
    -volname "$VOL_NAME" \
    -srcfolder "$STAGE" \
    -ov \
    -fs HFS+ \
    -format UDRW \
    "$RW_DMG" >/dev/null

# Detach a leftover volume with the same name (previous failed build).
if [[ -d "/Volumes/$VOL_NAME" ]]; then
    hdiutil detach "/Volumes/$VOL_NAME" -quiet >/dev/null 2>&1 || \
        hdiutil detach "/Volumes/$VOL_NAME" -force -quiet >/dev/null 2>&1 || true
fi

MOUNT="$(hdiutil attach -readwrite -noverify -noautoopen "$RW_DMG" \
    | awk '/\/Volumes\// { print $NF; exit }')"
[[ -n "$MOUNT" && -d "$MOUNT" ]] || { echo "failed to mount $RW_DMG" >&2; exit 1; }

if command -v SetFile >/dev/null 2>&1; then
    SetFile -a V "$MOUNT/.background" >/dev/null 2>&1 || true
    if [[ -f "$MOUNT/.VolumeIcon.icns" ]]; then
        SetFile -c icnC "$MOUNT/.VolumeIcon.icns" >/dev/null 2>&1 || true
        SetFile -a C "$MOUNT" >/dev/null 2>&1 || true
    fi
fi

# Finder writes .DS_Store only after the window is opened and positions settle.
osascript <<EOF
tell application "Finder"
    tell disk "$VOL_NAME"
        open
        set current view of container window to icon view
        set toolbar visible of container window to false
        set statusbar visible of container window to false
        set sidebar width of container window to 0
        set the bounds of container window to {220, 140, 880, 600}
        set theViewOptions to the icon view options of container window
        set arrangement of theViewOptions to not arranged
        set icon size of theViewOptions to 96
        set text size of theViewOptions to 13
        try
            set background picture of theViewOptions to file ".background:background.png"
        end try
        try
            set position of item "$APP_NAME" of container window to {140, 185}
        end try
        try
            set position of item "Applications" of container window to {520, 185}
        end try
        try
            set position of item "Read Me.txt" of container window to {330, 320}
        end try
        close
        open
        update without registering applications
        delay 2
    end tell
end tell
EOF

sync
if command -v bless >/dev/null 2>&1; then
    bless --folder "$MOUNT" --openfolder "$MOUNT" >/dev/null 2>&1 || true
fi

# Finder sometimes keeps the volume busy for a moment after AppleScript.
for _ in 1 2 3 4 5; do
    if hdiutil detach "$MOUNT" -quiet >/dev/null 2>&1; then
        MOUNT=""
        break
    fi
    sleep 1
done
if [[ -n "$MOUNT" ]]; then
    hdiutil detach "$MOUNT" -force -quiet >/dev/null 2>&1 || true
    MOUNT=""
fi

mkdir -p "$(dirname "$DMG_PATH")"
rm -f "$DMG_PATH"
hdiutil convert \
    "$RW_DMG" \
    -ov \
    -format UDZO \
    -imagekey zlib-level=9 \
    -o "$DMG_PATH" >/dev/null
