#!/bin/bash
set -e

echo "[INFO] Starting Stratoclave Backend"

# Note: Alembic migrations removed - using DynamoDB only
# No SQL database migrations needed

# Start the application
echo "[INFO] Starting uvicorn server..."
exec uvicorn main:app --host 0.0.0.0 --port 8000
