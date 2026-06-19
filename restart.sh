#!/bin/bash
set -e

echo "Restarting Workout Agent containers..."
./stop.sh
./start.sh
