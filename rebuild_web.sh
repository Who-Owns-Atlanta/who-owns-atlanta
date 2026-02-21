#!/bin/bash

# Stops, rebuilds, and restarts ONLY the web service
# This is useful when dependencies or code changes require a new image

echo "Stopping and removing 'nbh_web' container..."
docker compose stop web
docker compose rm -f web

echo "Rebuilding and starting 'web' service..."
docker compose up -d --build web

echo "Checking status..."
docker ps -f name=nbh_web
