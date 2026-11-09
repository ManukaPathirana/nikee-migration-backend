#!/bin/sh
# Optional start-up steps, then run the container command (API or worker).
set -e

if [ "${RUN_MIGRATIONS:-0}" = "1" ]; then
  echo "Applying database migrations..."
  alembic upgrade head
fi

if [ "${RUN_SEED:-0}" = "1" ]; then
  echo "Seeding data (safe to repeat)..."
  python -m app.scripts.seed
fi

exec "$@"
