#!/bin/bash
set -e

echo "Building and starting Workout Agent containers..."
docker compose up -d --build

echo "Waiting for containers to initialise..."
sleep 3

echo "Forcing an initial data population run..."
docker compose exec -d agent python insight_cron.py --daily || true
docker compose exec -d agent python insight_cron.py --weekly || true

echo "Done!"
echo "Dashboard is running at: http://localhost:${WEB_PORT:-8770}"
echo ""
echo "To view agent logs: docker compose logs -f agent"
echo "To view web logs: docker compose logs -f web"
