#!/bin/bash

# Full rebuild of the API image — use when dependencies (pyproject.toml) change.

echo "Rebuilding and restarting woa_api..."
docker compose stop api
docker compose rm -f api
docker compose up -d --build api

echo ""
docker ps -f name=woa_api
