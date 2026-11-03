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

WORK="$(mktemp -d "${TMPDIR:-/tmp}/argusai-dmg.XXXXXX")"
WORK="$(cd "$WORK" && pwd -P)"
STAGE="$WORK/stage"
RW_DMG="$WORK/working.dmg"
FINAL_DMG="$WORK/compressed.dmg"
MOUNT="$WORK/mount"
ATTACH_STARTED=0

image_attached() {
    if ! hdiutil info >"$WORK/image-info.txt"; then
        echo "Cannot verify whether the temporary image is attached: $RW_DMG" >&2
        return 2
    fi
    awk -v image="$RW_DMG" '
        /^[[:space:]]*image-path[[:space:]]*:/ {
            sub(/^[[:space:]]*image-path[[:space:]]*:[[:space:]]*/, "")
            if ($0 == image) found = 1
        }
        END { exit !found }
    ' "$WORK/image-info.txt"
}

mountpoint_active() {
    if ! mount >"$WORK/mount-info.txt"; then
        echo "Cannot inspect the temporary mount point: $MOUNT" >&2
        return 2
    fi
    grep -Fq " on $MOUNT (" "$WORK/mount-info.txt"
}

image_detached() {
    local status
    if image_attached; then
        return 1
    else
        status=$?
        [[ "$status" == 1 ]] || return 2
    fi
    if mountpoint_active; then
        return 1
    else
        status=$?
        [[ "$status" == 1 ]] || return 2
    fi
}

close_finder_window() {
    osascript - "$MOUNT" <<'EOF'
on run argv
    tell application "Finder"
        try
            close folder (POSIX file (item 1 of argv) as alias)
        end try
    end tell
end run
EOF
}

unmount_image() {
    local attempt status detach_status=1
    for attempt in 1 2 3 4 5 6; do
        if image_detached; then
            return 0
        else
            status=$?
            [[ "$status" == 1 ]] || return "$status"
        fi
        # Never resolve a volume by its user-visible name, or force-detach a
        # directory which is no longer this build's private mount point.
        if ! image_attached || ! mountpoint_active; then
            echo "Cannot safely detach the temporary image at $MOUNT" >&2
            return 1
        fi
        if ! close_finder_window; then
            echo "Finder did not close its temporary image window; attempting detach." >&2
        fi
        if [[ "$attempt" == 6 ]]; then
            if hdiutil detach "$MOUNT" -force -quiet 2>"$WORK/detach.stderr"; then
                detach_status=0
            else
                detach_status=$?
                cat "$WORK/detach.stderr" >&2
            fi
        else
            if hdiutil detach "$MOUNT" -quiet 2>"$WORK/detach.stderr"; then
                detach_status=0
            else
                detach_status=$?
                cat "$WORK/detach.stderr" >&2
            fi
        fi
        if image_detached; then
            return 0
        else
            status=$?
            [[ "$status" == 1 ]] || return "$status"
        fi
        if [[ "$attempt" != 6 ]]; then
            sleep 1
        fi
    done
    echo "Temporary image is still attached; conversion is unsafe: $RW_DMG" >&2
    [[ "$detach_status" != 0 ]] || detach_status=1
    return "$detach_status"
}

cleanup() {
    local status=$?
    trap - EXIT
    if [[ "$ATTACH_STARTED" == 1 ]] && ! unmount_image; then
        echo "Keeping temporary files because detach could not be confirmed: $WORK" >&2
        [[ "$status" != 0 ]] || status=1
    else
        if ! rm -rf -- "$WORK"; then
            echo "Could not remove the temporary image files: $WORK" >&2
            [[ "$status" != 0 ]] || status=1
        fi
    fi
    exit "$status"
}
trap cleanup EXIT

mkdir -p "$STAGE/.background" "$MOUNT"
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

# A private mount point also lets simultaneous builds use the same volume name.
ATTACH_STARTED=1
hdiutil attach -readwrite -noverify -noautoopen -mountpoint "$MOUNT" "$RW_DMG" >"$WORK/attach.stdout"
image_attached && mountpoint_active || { echo "failed to mount $RW_DMG at $MOUNT" >&2; exit 1; }

if command -v SetFile >/dev/null 2>&1; then
    SetFile -a V "$MOUNT/.background" >/dev/null 2>&1 || true
    if [[ -f "$MOUNT/.VolumeIcon.icns" ]]; then
        SetFile -c icnC "$MOUNT/.VolumeIcon.icns" >/dev/null 2>&1 || true
        SetFile -a C "$MOUNT" >/dev/null 2>&1 || true
    fi
fi

# Finder writes .DS_Store only after the window is opened and positions settle.
osascript - "$MOUNT" "$APP_NAME" <<'EOF'
on run argv
    set mountPath to item 1 of argv
    set appName to item 2 of argv
    tell application "Finder"
    tell folder (POSIX file mountPath as alias)
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
            set position of item appName of container window to {140, 185}
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
        close
    end tell
    end tell
end run
EOF

sync
if command -v bless >/dev/null 2>&1; then
    bless --folder "$MOUNT" --openfolder "$MOUNT" >/dev/null 2>&1 || true
fi

# A successful command is insufficient: confirm neither image nor mount remains.
unmount_image
ATTACH_STARTED=0

mkdir -p "$(dirname "$DMG_PATH")"
for attempt in 1 2 3; do
    if LC_ALL=C hdiutil convert "$RW_DMG" -ov -format UDZO \
        -imagekey zlib-level=9 -o "$FINAL_DMG" >"$WORK/convert.stdout" 2>"$WORK/convert.stderr"; then
        [[ -s "$FINAL_DMG" ]] || { echo "hdiutil produced no complete compressed image." >&2; exit 1; }
        mv -f "$FINAL_DMG" "$DMG_PATH"
        exit 0
    else
        status=$?
    fi
    cat "$WORK/convert.stderr" >&2
    # Retry only recognized transient contention, never parameter, permission,
    # or space errors (even if a secondary message also mentions contention).
    if [[ "$attempt" == 3 ]] || \
        grep -Eiq 'No space left|not enough space|invalid argument|invalid option|unrecognized option|permission denied|read-only file system' "$WORK/convert.stderr" || \
        ! grep -Eiq 'Resource temporarily unavailable|Resource busy|Device or resource busy' "$WORK/convert.stderr"; then
        exit "$status"
    fi
    echo "Temporary image conversion contention; retrying ($attempt/3)." >&2
    sleep "$attempt"
done
