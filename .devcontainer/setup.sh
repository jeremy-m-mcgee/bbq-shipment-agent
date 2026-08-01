#!/usr/bin/env bash
set -euo pipefail

npm install -g @anthropic-ai/claude-code
pip install uv

if [ -f pyproject.toml ]; then
  uv sync
else
  echo "No pyproject.toml yet, skipping uv sync"
fi

claude --version || echo "WARNING: claude not on PATH"
