#!/bin/sh
# Vallotton on macOS / Linux: ./start.sh
#
# Everything lives in .runtime/ inside this folder: a virtual environment with
# the backend's dependencies, and -- only if this computer has no Python 3.12
# or newer that can set that environment up -- a downloaded CPython next to
# it (uv, a small single-file tool, fetches it). Nothing is installed anywhere
# else and no shell profile or environment variable is changed; delete
# .runtime/ and it is as if nothing happened.
set -e
cd "$(dirname "$0")"
PY=".runtime/venv/bin/python"
NEED='import django, httpx, PIL, whitenoise, rich'

# an environment from an earlier run: use it only if it is whole. One whose
# dependencies never finished installing is set up again, not run.
if [ -x "$PY" ] && ! "$PY" -c "$NEED" 2>/dev/null; then
  echo "[Vallotton] The environment in .runtime/venv is incomplete; setting it up again ..."
  rm -rf .runtime/venv
fi

# the environment from the Python already installed; any step failing means
# the caller falls back to a downloaded Python
with_system() {
  "$BASE" -m venv .runtime/venv || return 1
  "$PY" -m pip --version >/dev/null 2>&1 || return 1
  echo "[Vallotton] Installing the backend's dependencies (a minute or two; pip shows its progress below) ..."
  "$PY" -m pip install --disable-pip-version-check -r backend/requirements.txt rich || return 1
  "$PY" -c "$NEED" 2>/dev/null || return 1
}

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
    if ! with_system; then
      echo "[Vallotton] That Python could not set the environment up; downloading a separate one instead ..."
      rm -rf .runtime/venv
      BASE=""
    fi
  fi

  if [ -z "$BASE" ]; then
    echo "[Vallotton] Downloading a Python into .runtime/ (about 40 MB) ..."
    UV=".runtime/uv/uv"
    if [ ! -x "$UV" ]; then
      curl -LsSf https://astral.sh/uv/install.sh \
        | env UV_INSTALL_DIR="$PWD/.runtime/uv" UV_NO_MODIFY_PATH=1 INSTALLER_NO_MODIFY_PATH=1 sh
    fi
    export UV_PYTHON_INSTALL_DIR="$PWD/.runtime/python"
    export UV_CACHE_DIR="$PWD/.runtime/cache"
    "$UV" python install 3.12
    "$UV" venv .runtime/venv --python 3.12 --seed
    echo "[Vallotton] Installing the backend's dependencies (a minute or two) ..."
    "$UV" pip install --python "$PY" -r backend/requirements.txt rich
    "$PY" -c "$NEED"
  fi
fi
exec "$PY" quickstart.py "$@"
