#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# restart
docker compose stop n-kb ollama-pull-bge-m3
docker compose rm -f n-kb ollama-pull-bge-m3
docker compose up -d --remove-orphans qdrant ollama
docker compose up -d --build --force-recreate --remove-orphans n-kb
echo

# status
sleep 2
echo "compose ps n-kb"
docker compose ps n-kb
echo

# health
sleep 2
echo "curl -fsS http://localhost:8212/health"
curl -fsS http://localhost:8212/health | jq .
