# A-02-docker: tag-only references are mutable. The base image is
# expected to be pinned by digest in the production build pipeline:
# CI passes `--build-arg PYTHON_BASE=public.ecr.aws/docker/library/python@sha256:<...>`
# so the actual layer hash is locked in the deployed image. Local
# development falls back to the floating tag below.
ARG PYTHON_BASE=public.ecr.aws/docker/library/python:3.11-slim
FROM ${PYTHON_BASE}

WORKDIR /app

# Install system dependencies (curl for health checks). `--no-install-recommends`
# minimises the attack surface of the base image; the explicit `apt-get
# update` is kept in the same RUN as the cleanup so the cache layer stays
# tight.
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Install runtime dependencies only (requirements-dev.txt stays off the
# image to keep pytest/moto/ruff out of the container). Layer ordering
# preserves pip cache for iterative builds.
#
# A-03-pip: the production image opts into `--require-hashes` when a
# pinned `requirements.lock.txt` is present. The lock file is generated
# by `pip-compile --generate-hashes` in CI and committed alongside the
# floating `requirements.txt`. Dev / local runs without the lock fall
# back to the floating list with `--no-cache-dir`.
COPY requirements.txt requirements.lock.txt* ./
RUN if [ -f requirements.lock.txt ]; then \
        pip install --no-cache-dir --require-hashes --no-deps -r requirements.lock.txt; \
    else \
        pip install --no-cache-dir -r requirements.txt; \
    fi

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
