#!/usr/bin/env bash
# Creates .venv with the deps for scripts/process_guest_images.py.
# First run on a machine compiles dlib (~5 min); pip's wheel cache
# at ~/Library/Caches/pip/wheels makes every subsequent venv setup
# (in any worktree) finish in ~10 s.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VENV="$ROOT/.venv"
REQS="$ROOT/scripts/photos-requirements.txt"

if [ ! -x "$VENV/bin/python" ]; then
  python3 -m venv "$VENV"
fi

"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet -r "$REQS"

echo "ready. run:"
echo "  $VENV/bin/python scripts/process_guest_images.py"
