#!/bin/sh
set -e

echo "Running database migrations..."
alembic upgrade head

echo "Starting API..."
exec python -m api.app
