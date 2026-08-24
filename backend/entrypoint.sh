#!/bin/bash
set -e

echo "[INFO] Starting Stratoclave Backend"

# Note: Alembic migrations removed - using DynamoDB only
# No SQL database migrations needed

# How many uvicorn processes serve this task.
#
# One process was the only option before, and it makes the GIL the ceiling on
# latency rather than the task's CPU: measured on 2026-08-25, p50 tracked the
# number of requests in flight PER PROCESS (390 ms at 4, 1331 ms at 32, 7706 ms at
# 128) while task CPU stayed under 70%, because each request needs several short
# bursts of Python for signing and serialization and they take turns.
#
# More processes only help if there is CPU behind them — N workers on one vCPU
# still run one bytecode stream at a time — so this is sized WITH the task's CPU,
# not instead of it. `GATEWAY_UVICORN_WORKERS` defaults to 1 so the behaviour is
# unchanged unless a deployment asks for more, and the IaC sets it alongside the
# task size for that reason.
#
# What each worker keeps its own copy of: the bedrock-mantle bearer cache, the
# httpx and boto3 connection pools, the pricing cache, and the router's advisory
# cooldown map. All are per-process by design and correct when duplicated — the
# cost is N times the token mints per TTL and N times the idle connections, which
# is the price of not sharing a GIL.
WORKERS="${GATEWAY_UVICORN_WORKERS:-1}"
if ! [[ "$WORKERS" =~ ^[1-9][0-9]*$ ]]; then
  echo "[FAIL] GATEWAY_UVICORN_WORKERS must be a positive integer (got '${WORKERS}')" >&2
  exit 1
fi

echo "[INFO] Starting uvicorn server (workers=${WORKERS})..."
if [[ "$WORKERS" == "1" ]]; then
  # Single process: exec so uvicorn is PID 1 and receives SIGTERM directly, which
  # is what runs the lifespan shutdown that closes the pooled clients.
  exec uvicorn main:app --host 0.0.0.0 --port 8000
fi

# Multi-process: uvicorn's supervisor is PID 1 and forwards signals to its
# children, so each worker still runs its own lifespan shutdown.
exec uvicorn main:app --host 0.0.0.0 --port 8000 --workers "$WORKERS"
