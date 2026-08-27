# Local mode: DynamoDB is local, Bedrock is real. See docs/LOCAL.md.
#
# Works with either `docker compose` or `finch compose` — picks whichever
# binary is on PATH, preferring docker. Override on the command line, e.g.:
#   make up AWS_PROFILE=my-bedrock-profile AWS_REGION=us-west-2

AWS_PROFILE ?= default
AWS_REGION ?= us-east-1
COMPOSE := $(shell command -v docker >/dev/null 2>&1 && echo "docker compose" || echo "finch compose")
LOCAL_DDB_ENDPOINT := http://127.0.0.1:8000
GATEWAY_URL := http://127.0.0.1:8080

.PHONY: up demo demo-offline prove verify-rating verify-formal down _host-deps

# Start DynamoDB Local, create every table it needs (mirroring
# iac/lib/dynamodb-stack.ts), build and start the gateway, then seed one
# local user + a scoped API key. Bedrock calls the gateway makes still go to
# your real AWS account via ~/.aws (read-only) — nothing here is a mock.
# The scripts under scripts/local/ run on the HOST (they talk to DynamoDB
# Local directly and import the backend's repositories), so the host needs the
# backend's runtime dependencies. Failing here with the fix printed beats
# failing three lines later with an ImportError traceback.
_host-deps:
	@python3 -c "import boto3" 2>/dev/null || { \
	  echo "[make] the host is missing the backend's Python dependencies." >&2; \
	  echo "[make] run: python3 -m pip install -r backend/requirements.txt" >&2; \
	  exit 1; \
	}
	@PYTHONPATH=backend python3 -c "from dynamo.client import get_dynamodb_resource" 2>/dev/null || { \
	  echo "[make] the host has boto3 but cannot import the backend package." >&2; \
	  echo "[make] run: python3 -m pip install -r backend/requirements.txt" >&2; \
	  exit 1; \
	}

up: _host-deps
	@echo "[make up] using: $(COMPOSE)"
	@echo "[make up] starting DynamoDB Local..."
	$(COMPOSE) up -d dynamodb-local
	@echo "[make up] waiting for DynamoDB Local on 127.0.0.1:8000..."
	@ok=0; for i in $$(seq 1 30); do \
	  curl -s -o /dev/null $(LOCAL_DDB_ENDPOINT) && ok=1 && break; sleep 1; \
	done; \
	if [ "$$ok" != "1" ]; then echo "[make up] DynamoDB Local did not come up" >&2; exit 1; fi
	@echo "[make up] creating tables (idempotent)..."
	AWS_ENDPOINT_URL_DYNAMODB=$(LOCAL_DDB_ENDPOINT) AWS_REGION=$(AWS_REGION) \
	  python3 scripts/local/create_tables.py
	@echo "[make up] building and starting the gateway..."
	$(COMPOSE) up -d --build gateway
	@echo "[make up] waiting for the gateway on $(GATEWAY_URL)/health..."
	@ok=0; for i in $$(seq 1 40); do \
	  curl -sf $(GATEWAY_URL)/health >/dev/null 2>&1 && ok=1 && break; sleep 1; \
	done; \
	if [ "$$ok" != "1" ]; then \
	  echo "[make up] gateway did not become healthy — check: $(COMPOSE) logs gateway" >&2; exit 1; \
	fi
	@echo "[make up] seeding a local user + API key (idempotent; reuses an existing key if still valid)..."
	AWS_ENDPOINT_URL_DYNAMODB=$(LOCAL_DDB_ENDPOINT) AWS_REGION=$(AWS_REGION) \
	  python3 scripts/local/seed_local_user.py
	@echo "[make up] ready. Run 'make demo' to call real Bedrock through your local gateway."

# The main event: call all three inference routes against your local gateway
# — which calls real Bedrock — and read the result back from your local
# ledger (UsageLogs + UserTenants), not from this script's own bookkeeping.
# Requires `make up` to have completed. Fails loudly on a credential problem;
# there is no mock to silently fall back to.
demo: _host-deps
	AWS_ENDPOINT_URL_DYNAMODB=$(LOCAL_DDB_ENDPOINT) AWS_REGION=$(AWS_REGION) \
	  AWS_PROFILE=$(AWS_PROFILE) STRATOCLAVE_LOCAL_URL=$(GATEWAY_URL) \
	  python3 scripts/local/demo.py

# The AWS-free side path: replays the existing offline Savings Report demo
# (bench/savings/demo_offline.py) over a checked-in synthetic workload. No
# network, no Docker/Finch, no `make up` needed — for anyone without an AWS
# account who still wants to see the reproducible-computation shape.
demo-offline:
	cd backend && python3 ../bench/savings/demo_offline.py

# Replays the Z3 formal proofs locally: no network, no AWS, no Docker/Finch.
# Needs `z3-solver` (backend/requirements-dev.txt) on whatever `python3`
# resolves to on your PATH — it does not use the gateway container, which
# deliberately does not ship dev/test dependencies (see backend/Dockerfile).
prove:
	@python3 -c "import z3" 2>/dev/null || { \
	  echo "z3-solver is not installed. Run: pip install -r backend/requirements-dev.txt" >&2; exit 1; \
	}
	cd backend && python3 -m pytest \
	  tests/test_billing_formal_z3.py \
	  tests/test_pending_golden_equivalence_z3.py \
	  tests/test_pending_protocol_z3.py \
	  tests/test_quota_formal_z3.py \
	  tests/test_savings_z3.py \
	  tests/test_sr_money_formal_z3.py \
	  tests/test_observability_emit_z3.py \
	  tests/test_rating_formal_z3.py \
	  tests/test_pricing_pinning_z3.py \
	  -q
	@echo "Proved: reserve/settle admits no double-counting, the PENDING-protocol"
	@echo "migration is a verified-equivalent money path, and the ceiling is sound"
	@echo "GIVEN that the reserve estimate dominates the settled actual per"
	@echo "component — under the axioms stated at the top of each test file above."
	@echo ""
	@echo "That premise is NOT proved and is false today: run 'make verify-rating'"
	@echo "for the differential checks and the pinned known defect."

# The differential half of the formal layer: drives the real pricing functions and
# an independently written reference with the same inputs, so "the encoding matches
# the code" is sampled rather than assumed. No network, no AWS, no container.
verify-rating:
	cd backend && python3 -m pytest tests/test_rating_differential.py -q
	@echo ""
	@echo "One xfail is expected and is a tracking device, not a nuisance: the"
	@echo "estimator prices three components while the charger bills four, so a"
	@echo "prompt-cache write settles above its reservation. See docs/EVIDENCE.md."

# Everything in the formal layer that needs no credentials and no container.
verify-formal: prove verify-rating

# Stop DynamoDB Local and the gateway, and remove the local DynamoDB volume.
# Your Bedrock account is never touched by this. Your local ledger, user, and
# seeded API key are wiped — `make up` recreates all of them from scratch.
down:
	$(COMPOSE) down -v
