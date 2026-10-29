#!/usr/bin/env bash
# Initialize the retained macOS vendor assets. Python/Node sandbox runtimes are
# downloaded on demand into the user's data_root and are never written here.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
FORCE=0
SKIP_WABT=0
SKIP_ICON=0

usage() {
  cat <<'EOF'
Usage: scripts/init_vendor_macos.sh [options]

Options:
  --force       Re-download retained vendor assets.
  --skip-wabt   Skip WABT maintenance.
  --skip-icon   Skip macOS .icns generation.
  -h, --help    Show this help.

Python, Node.js, webcrack, and restringer are installed on demand from the
ArgusAI Settings > Sandbox runtimes panel.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --force) FORCE=1; shift ;;
    --skip-wabt) SKIP_WABT=1; shift ;;
    --skip-icon) SKIP_ICON=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

[[ "$(uname -s)" == "Darwin" ]] || {
  echo "This script is intended for macOS." >&2
  exit 1
}

cd "${REPO_ROOT}"
if command -v uv >/dev/null 2>&1; then
  PY=(uv run python)
elif command -v python3 >/dev/null 2>&1; then
  PY=(python3)
else
  echo "Python 3 is required for repository maintenance." >&2
  exit 1
fi

ARGS=()
[[ "${FORCE}" -eq 0 ]] || ARGS+=(--force)
[[ "${SKIP_WABT}" -eq 0 ]] || ARGS+=(--skip-wabt)

echo "==> Maintaining retained vendor assets"
# innoextract has no official macOS portable build — skip bundling it.
# Use Homebrew for installer_unpack: brew install innoextract
VENDOR_ARGS=("${ARGS[@]}")
VENDOR_ARGS+=(--skip-innoextract)
"${PY[@]}" scripts/vendor_runtime.py "${VENDOR_ARGS[@]}" || true

if ! command -v innoextract >/dev/null 2>&1; then
  echo "==> Note: innoextract not on PATH (optional)."
  echo "    For Inno Setup unpacking: brew install innoextract"
fi

if [[ "${SKIP_ICON}" -eq 0 ]]; then
  echo "==> Generating macOS app icon"
  bash scripts/create_icns_macos.sh
fi

echo "Done. Sandbox runtimes are managed on demand under data_root/tools/runtimes."
