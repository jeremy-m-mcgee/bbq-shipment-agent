#!/usr/bin/env bash
set -euo pipefail

npm install -g @anthropic-ai/claude-code
pip install uv

# devcontainer.json points UV_ENV_FILE at .env, and uv exits 2 when that file
# is missing. Seed it so a fresh clone can run before anyone adds a real key.
# Never overwrite an existing .env — it holds secrets.
if [ ! -f .env ] && [ -f .env.example ]; then
  cp .env.example .env
  echo "Created .env from .env.example. Fill in LD_SDK_KEY before running the pipeline."
fi

if [ -f pyproject.toml ]; then
  uv sync
else
  echo "No pyproject.toml yet, skipping uv sync"
fi

claude --version || echo "WARNING: claude not on PATH"
