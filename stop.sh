#!/bin/bash
set -e

echo "Stopping Workout Agent containers..."
docker compose down

echo "Done!"
