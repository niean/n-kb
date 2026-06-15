#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

exec docker compose -f "$PROJECT_DIR/docker/docker-compose.yml" run --rm -T n-kb python -m app.interfaces.mcp.stdio
