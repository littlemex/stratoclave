FROM public.ecr.aws/docker/library/python:3.11-slim

WORKDIR /app

# Install system dependencies (curl for health checks).
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Install runtime dependencies only (requirements-dev.txt stays off the
# image to keep pytest/moto/ruff out of the container). Layer ordering
# preserves pip cache for iterative builds.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application.
COPY . .

# Data directory the app writes into at runtime.
RUN mkdir -p /app/data

# Entrypoint.
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

# ---------------------------------------------------------------------
# Drop to a non-root user (P1-5).
# Running as root inside the container is a defense-in-depth violation
# called out by Reviewer 5: a container escape lands on UID 0 and any
# writable mount becomes root-writable. `stratoclave` (UID 10001) owns
# the application tree so the entrypoint can still read / write
# /app/data without elevated privileges.
# ---------------------------------------------------------------------
RUN groupadd --system --gid 10001 stratoclave && \
    useradd  --system --uid 10001 --gid stratoclave --home-dir /app --no-create-home stratoclave && \
    chown -R stratoclave:stratoclave /app
USER stratoclave

EXPOSE 8000

# Dockerfile-level HEALTHCHECK (P1-5).
# ECS has its own container health check in the task definition; this
# directive gives value to anyone running the image directly (compose,
# k8s, local) and complements the ECS config without duplicating it.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl --fail --silent http://127.0.0.1:8000/health || exit 1

# Run application with migrations.
ENTRYPOINT ["/app/entrypoint.sh"]
