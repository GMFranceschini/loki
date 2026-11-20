#!/bin/bash
# Set Loki up from a fresh clone.
#
#   scripts/install.sh              create the environment and check it
#   scripts/install.sh --schedule   also install the daily 07:00 fetch
#   scripts/install.sh --app        also build Loki.app and put it on the Desktop
#
# Safe to rerun: it reuses an existing .venv rather than rebuilding it.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PYTHON_VERSION="3.12"
VENV="$ROOT/.venv"

say() { printf '\n==> %s\n' "$1"; }

# ---------------------------------------------------------------- environment
if [ -x "$VENV/bin/python" ]; then
    say "Using the environment already at .venv"
elif command -v uv >/dev/null 2>&1; then
    say "Creating .venv with uv (Python $PYTHON_VERSION)"
    uv venv --python "$PYTHON_VERSION" "$VENV"
else
    say "Creating .venv with the system Python"
    if ! command -v "python$PYTHON_VERSION" >/dev/null 2>&1; then
        echo "Python $PYTHON_VERSION was not found." >&2
        echo "Install it, or install uv (https://docs.astral.sh/uv/) and rerun." >&2
        exit 1
    fi
    "python$PYTHON_VERSION" -m venv "$VENV"
fi

say "Installing dependencies"
if command -v uv >/dev/null 2>&1; then
    VIRTUAL_ENV="$VENV" uv pip install -e ".[dev]"
else
    "$VENV/bin/python" -m pip install --upgrade pip >/dev/null
    "$VENV/bin/python" -m pip install -e ".[dev]"
fi

# ---------------------------------------------------------------- check
say "Running the test suite"
"$VENV/bin/python" -m pytest tests/ -q

say "Checking config, model, database and network"
# The first run downloads the ~30 MB embedding model.
"$ROOT/bin/loki" doctor || true

# ---------------------------------------------------------------- extras
for arg in "$@"; do
    case "$arg" in
        --schedule) say "Installing the daily fetch"; "$ROOT/scripts/install_schedule.sh" ;;
        --app)      say "Building Loki.app";          "$ROOT/scripts/build_app.sh" --desktop ;;
    esac
done

cat <<'DONE'

==> Ready.

Fill the database with the last week of papers, then open the UI:

    bin/loki fetch --backfill 7
    bin/loki serve          # http://127.0.0.1:8713

Edit config/interests.yaml to describe what you want to read about,
or press Topics in the web UI.
DONE
