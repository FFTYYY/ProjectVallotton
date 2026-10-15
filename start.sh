#!/bin/sh
# Vallotton on macOS / Linux: ./start.sh
#
# Everything lives in .runtime/ inside this folder: a virtual environment with
# the backend's dependencies, and -- only if this computer has no usable
# Python 3.12 or newer -- a downloaded CPython next to it (uv, a small
# single-file tool, fetches it). Nothing is installed anywhere else and no
# shell profile or environment variable is changed; delete .runtime/ and it
# is as if nothing happened.
set -e
cd "$(dirname "$0")"
PY=".runtime/venv/bin/python"

if [ ! -x "$PY" ]; then
  echo "[Vallotton] First run: preparing a project-local Python under .runtime/ ..."
  mkdir -p .runtime

  # a Python already on this computer, 3.12 or newer, that can make a venv
  # with pip in it (Debian and Ubuntu ship python3 without that unless the
  # python3-venv package is installed, so this is checked, not assumed)
  BASE=""
  for c in python3.13 python3.12 python3 python; do
    if command -v "$c" >/dev/null 2>&1 \
       && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' 2>/dev/null \
       && "$c" -c 'import venv, ensurepip' 2>/dev/null; then
      BASE="$(command -v "$c")"
      break
    fi
  done

  if [ -n "$BASE" ]; then
    echo "[Vallotton] Using the Python already installed: $BASE"
    "$BASE" -m venv .runtime/venv
    "$PY" -m pip install -q --disable-pip-version-check -r backend/requirements.txt rich
  else
    echo "[Vallotton] No Python 3.12+ found; downloading one into .runtime/ (about 40 MB) ..."
    UV=".runtime/uv/uv"
    if [ ! -x "$UV" ]; then
      curl -LsSf https://astral.sh/uv/install.sh \
        | env UV_INSTALL_DIR="$PWD/.runtime/uv" UV_NO_MODIFY_PATH=1 INSTALLER_NO_MODIFY_PATH=1 sh
    fi
    export UV_PYTHON_INSTALL_DIR="$PWD/.runtime/python"
    export UV_CACHE_DIR="$PWD/.runtime/cache"
    "$UV" python install 3.12
    "$UV" venv .runtime/venv --python 3.12 --seed
    "$UV" pip install --python "$PY" -r backend/requirements.txt rich
  fi
fi
exec "$PY" quickstart.py "$@"
