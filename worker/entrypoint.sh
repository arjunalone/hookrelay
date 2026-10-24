#!/bin/sh
set -e

echo "Checking database migrations..."
alembic upgrade head

echo "Starting worker..."
exec python -m worker.worker
