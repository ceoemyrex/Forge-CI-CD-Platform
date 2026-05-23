#!/usr/bin/env bash
# Bootstrap local Python environment for Forge development.
set -euo pipefail

cd "$(dirname "$0")/.."

python -m venv .venv

if [ -f .venv/Scripts/activate ]; then
  # Windows (Git Bash)
  source .venv/Scripts/activate
else
  source .venv/bin/activate
fi

python -m pip install --upgrade pip
python -m pip install -r requirements.txt

echo "venv ready — activate with: source .venv/Scripts/activate (Windows) or source .venv/bin/activate (Linux)"
