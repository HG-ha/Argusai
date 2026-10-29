#!/usr/bin/env bash
# Generate a macOS .icns app icon from the high-resolution PNG source.
#
# Requires macOS built-ins:
# - sips
# - iconutil

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

SOURCE="${REPO_ROOT}/assets/brand/argusai-icon-Transparency.png"
OUTPUT="${REPO_ROOT}/assets/brand/argusai-icon.icns"

usage() {
  cat <<'EOF'
Usage: bash scripts/create_icns_macos.sh [source.png] [output.icns]

Defaults:
  source: assets/brand/argusai-icon-Transparency.png
  output: assets/brand/argusai-icon.icns
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

if [[ $# -ge 1 ]]; then
  SOURCE="$1"
fi
if [[ $# -ge 2 ]]; then
  OUTPUT="$2"
fi
if [[ $# -gt 2 ]]; then
  usage >&2
  exit 2
fi

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "This script must run on macOS because it uses sips and iconutil." >&2
  exit 1
fi

if [[ ! -f "${SOURCE}" ]]; then
  echo "Source PNG not found: ${SOURCE}" >&2
  exit 1
fi

if ! command -v sips >/dev/null 2>&1; then
  echo "sips not found. This script requires macOS." >&2
  exit 1
fi
if ! command -v iconutil >/dev/null 2>&1; then
  echo "iconutil not found. This script requires macOS." >&2
  exit 1
fi

TMP_DIR="$(mktemp -d)"
ICONSET="${TMP_DIR}/argusai.iconset"
mkdir -p "${ICONSET}"

cleanup() {
  rm -rf "${TMP_DIR}"
}
trap cleanup EXIT

make_icon() {
  local size="$1"
  local scale="$2"
  local filename="$3"
  local pixels=$((size * scale))
  sips -z "${pixels}" "${pixels}" "${SOURCE}" --out "${ICONSET}/${filename}" >/dev/null
}

make_icon 16 1 "icon_16x16.png"
make_icon 16 2 "icon_16x16@2x.png"
make_icon 32 1 "icon_32x32.png"
make_icon 32 2 "icon_32x32@2x.png"
make_icon 128 1 "icon_128x128.png"
make_icon 128 2 "icon_128x128@2x.png"
make_icon 256 1 "icon_256x256.png"
make_icon 256 2 "icon_256x256@2x.png"
make_icon 512 1 "icon_512x512.png"
make_icon 512 2 "icon_512x512@2x.png"

mkdir -p "$(dirname "${OUTPUT}")"
iconutil -c icns "${ICONSET}" -o "${OUTPUT}"

echo "Generated ${OUTPUT}"
