#!/bin/bash

# Restart the API container without rebuilding the image.
# With --reload active, uvicorn picks up code changes automatically —
# you only need this script for env changes or hard restarts.
#
# For dependency changes (pyproject.toml), do a full rebuild instead:
#   docker compose up -d --build api

echo "Restarting woa_api..."
docker compose stop api
docker compose rm -f api
docker compose up -d api

echo ""
docker ps -f name=woa_api
