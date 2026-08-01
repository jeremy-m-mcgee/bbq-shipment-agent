#!/usr/bin/env bash
set -euo pipefail

npm install -g @anthropic-ai/claude-code

python -m pip install --upgrade pip
if [ -f requirements.txt ]; then
  pip install -r requirements.txt
fi

echo "setup complete"
claude --version || echo "WARNING: claude not on PATH"
