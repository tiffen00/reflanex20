#!/usr/bin/env bash
set -e

# Ensure storage directories exist
mkdir -p storage/campaigns

# Start the FastAPI server
exec uvicorn backend.main:app --host 0.0.0.0 --port "${PORT:-8000}"
