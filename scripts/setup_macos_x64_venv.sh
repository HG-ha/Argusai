#!/usr/bin/env bash
# Create a Rosetta x86_64 virtualenv for cross-packaging ArgusAI on Apple Silicon.
#
# Nuitka's official guidance (https://nuitka.net/info/macos-cross-compile.html):
#   1) Make the app run under `arch -x86_64 python ...` first
#   2) Use a dedicated x86_64 venv with `arch -x86_64 python -m pip install ...`
#
# Usage (on arm64 Mac):
#   bash scripts/setup_macos_x64_venv.sh
#   arch -x86_64 .venv-x64/bin/python build.py --arch x86_64 --dmg
#
# Optional:
#   PYTHON_X64=/path/to/x86_64/python3.12 bash scripts/setup_macos_x64_venv.sh
#   VENV_DIR=.venv-x64 bash scripts/setup_macos_x64_venv.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "$REPO_ROOT"

VENV_DIR="${VENV_DIR:-.venv-x64}"
PYTHON_X64="${PYTHON_X64:-}"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "This script is for macOS only." >&2
  exit 1
fi

HOST_ARCH="$(uname -m)"
if [[ "$HOST_ARCH" != "arm64" && "$HOST_ARCH" != "aarch64" ]]; then
  echo "Host is already $HOST_ARCH; use a normal venv + python build.py instead." >&2
  exit 1
fi

# Rosetta is required to run / build x86_64 on Apple Silicon.
if ! arch -x86_64 /usr/bin/true >/dev/null 2>&1; then
  echo "==> Installing Rosetta (required for x86_64)..."
  softwareupdate --install-rosetta --agree-to-license \
    || { echo "Failed to install Rosetta. Install it from System Settings and retry." >&2; exit 1; }
fi

pick_x64_python() {
  if [[ -n "$PYTHON_X64" ]]; then
    printf '%s\n' "$PYTHON_X64"
    return
  fi
  # Prefer an explicitly installed uv-managed macOS x86_64 CPython when present.
  if command -v uv >/dev/null 2>&1; then
    local found=""
    found="$(
      arch -x86_64 uv python find 3.12 2>/dev/null || true
    )"
    if [[ -n "$found" && -x "$found" ]]; then
      if arch -x86_64 "$found" -c 'import platform; raise SystemExit(0 if platform.machine()=="x86_64" else 1)' 2>/dev/null; then
        printf '%s\n' "$found"
        return
      fi
    fi
    echo "==> Ensuring a uv-managed CPython 3.12 is available under Rosetta..."
    arch -x86_64 uv python install 3.12 >/dev/null
    found="$(arch -x86_64 uv python find 3.12)"
    if [[ -n "$found" && -x "$found" ]]; then
      printf '%s\n' "$found"
      return
    fi
  fi
  # Fallbacks: Intel Homebrew / Framework Python under Rosetta.
  local cand
  for cand in \
    /usr/local/bin/python3.12 \
    /usr/local/bin/python3 \
    /Library/Frameworks/Python.framework/Versions/3.12/bin/python3.12 \
    /Library/Frameworks/Python.framework/Versions/3.12/bin/python3
  do
    if [[ -x "$cand" ]] && arch -x86_64 "$cand" -c 'import platform; raise SystemExit(0 if platform.machine()=="x86_64" else 1)' 2>/dev/null; then
      printf '%s\n' "$cand"
      return
    fi
  done
  return 1
}

PY="$(pick_x64_python)" || {
  echo "Could not find an x86_64 Python 3.12+." >&2
  echo "Install one of:" >&2
  echo "  - uv python install 3.12   (then re-run this script under Rosetta)" >&2
  echo "  - python.org universal/x86_64 installer" >&2
  echo "  - Intel Homebrew python under /usr/local" >&2
  echo "Or set PYTHON_X64=/path/to/x86_64/python3.12" >&2
  exit 1
}

echo "==> Using x86_64 Python: $PY"
arch -x86_64 "$PY" - <<'PY'
import platform, sys
print(f"    executable={sys.executable}")
print(f"    version={sys.version.split()[0]}")
print(f"    machine={platform.machine()}")
if platform.machine() != "x86_64":
    raise SystemExit("Python is not running as x86_64 under Rosetta")
if sys.version_info < (3, 12):
    raise SystemExit("Python >= 3.12 is required")
PY

echo "==> Creating venv at ${VENV_DIR}"
rm -rf "$VENV_DIR"
arch -x86_64 "$PY" -m venv "$VENV_DIR"

VENV_PY="$VENV_DIR/bin/python"
[[ -x "$VENV_PY" ]] || { echo "venv python missing: $VENV_PY" >&2; exit 1; }

echo "==> Installing project into x86_64 venv (this downloads x86_64 wheels)..."
arch -x86_64 "$VENV_PY" -m pip install -U pip setuptools wheel >/dev/null
arch -x86_64 "$VENV_PY" -m pip install -e .
arch -x86_64 "$VENV_PY" -m pip install "nuitka>=4.1,<4.2" "ordered-set" "zstandard" "pytest>=8.0" >/dev/null

echo "==> Verifying native extensions are x86_64..."
arch -x86_64 "$VENV_PY" - <<'PY'
import importlib
import platform
import subprocess
import sys
from pathlib import Path

assert platform.machine() == "x86_64", platform.machine()

def arch_of(path: Path) -> str:
    return subprocess.check_output(["file", "-b", str(path)], text=True).strip()

def check(label: str, path: Path) -> None:
    info = arch_of(path)
    print(f"  {label}: {path.name} -> {info}")
    if "x86_64" not in info and "universal" not in info.lower():
        raise SystemExit(f"{label} is not x86_64/universal: {info}")

import cryptography.hazmat.bindings._rust as rust
check("cryptography._rust", Path(rust.__file__))

try:
    imaging = importlib.import_module("PIL._imaging")
    check("PIL._imaging", Path(imaging.__file__))
except Exception as exc:
    print(f"  WARNING: Pillow check skipped: {exc}", file=sys.stderr)

print("Native module arch check OK.")
PY

echo
echo "Done."
echo "  venv:   $VENV_DIR"
echo "  python: $VENV_PY"
echo
echo "Smoke-test under Rosetta:"
echo "  arch -x86_64 $VENV_PY -c 'import argusai; print(argusai.__version__)'"
echo
echo "Package x86_64 build:"
echo "  arch -x86_64 .venv-x64/bin/python build.py --arch x86_64 --dmg"
echo "  # or: arch -x86_64 $VENV_PY build.py --arch x86_64 --dmg"
