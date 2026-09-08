#!/usr/bin/env python3
"""Create every DynamoDB table this backend uses, against DynamoDB Local.

Mirrors `iac/lib/dynamodb-stack.ts` 1:1 (partition key, sort key, every GSI,
every TTL attribute) so a DynamoDB-Local-backed run exercises the same key
schema production does. Table names use the code's own defaults
(`stratoclave-<name>`) — every `backend/dynamo/*.py` / `backend/mvp/**/*.py`
module already falls back to that name when its `DYNAMODB_*_TABLE` env var is
unset, so no table-name env vars need to be set for local use.

Idempotent: an existing table (or an already-enabled TTL) is left alone. Safe
to run repeatedly, e.g. every `make up`.

Requires `AWS_ENDPOINT_URL_DYNAMODB` to point at DynamoDB Local — this script
refuses to run otherwise, so it can never accidentally create tables against a
real AWS account.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any, Optional


def _ensure_backend_importable() -> None:
    """Make `dynamo` importable whether this runs on the host (repo checkout)
    or inside the gateway container (where the backend package IS /app)."""
    try:
        import dynamo  # noqa: F401
        return
    except ImportError:
        pass
    here = Path(__file__).resolve()
    for candidate in (here.parents[2] / "backend", Path("/app")):
        if (candidate / "dynamo").is_dir():
            sys.path.insert(0, str(candidate))
            return
    raise SystemExit(
        "Could not locate the backend package ('dynamo/'). Run this inside the "
        "gateway container (`make up` does) or from a full repo checkout."
    )


_ensure_backend_importable()

from _local_guard import require_local_dynamodb  # noqa: E402




# ---------------------------------------------------------------------------
# Table specs — one entry per `iac/lib/dynamodb-stack.ts` table.
# ---------------------------------------------------------------------------
S, N = "S", "N"

TABLES: list[dict[str, Any]] = [
    {
        "name": "stratoclave-sessions",
        "pk": ("session_id", S), "sk": ("sk", S),
        "gsis": [
            {"name": "user-id-index", "pk": ("user_id", S), "sk": ("created_at", N)},
            {"name": "tenant-id-index", "pk": ("tenant_id", S), "sk": ("created_at", N)},
        ],
    },
    {
        "name": "stratoclave-messages",
        "pk": ("session_id", S), "sk": ("message_id", S),
    },
    {
        "name": "stratoclave-users",
        "pk": ("user_id", S), "sk": ("sk", S),
        "gsis": [
            {"name": "email-index", "pk": ("email", S)},
            {"name": "auth-provider-user-id-index", "pk": ("auth_provider_user_id", S)},
        ],
    },
    {
        "name": "stratoclave-user-tenants",
        "pk": ("user_id", S), "sk": ("tenant_id", S),
        "gsis": [
            {"name": "tenant-id-index", "pk": ("tenant_id", S), "sk": ("user_id", S)},
        ],
    },
    {
        "name": "stratoclave-usage-logs",
        "pk": ("tenant_id", S), "sk": ("timestamp_log_id", S), "ttl": "ttl",
        "gsis": [
            {"name": "user-id-index", "pk": ("user_id", S), "sk": ("timestamp_log_id", S)},
        ],
    },
    {
        "name": "stratoclave-app-settings",
        "pk": ("setting_key", S),
    },
    {
        "name": "stratoclave-tags",
        "pk": ("session_id", S), "sk": ("tag_name", S),
    },
    {
        "name": "stratoclave-sse-tokens",
        "pk": ("token_id", S), "ttl": "ttl",
    },
    {
        "name": "stratoclave-tenants",
        "pk": ("tenant_id", S),
        "gsis": [
            {"name": "team-lead-index", "pk": ("team_lead_user_id", S), "sk": ("created_at", S)},
        ],
    },
    {
        "name": "stratoclave-permissions",
        "pk": ("role", S),
    },
    {
        "name": "stratoclave-trusted-accounts",
        "pk": ("account_id", S),
    },
    {
        "name": "stratoclave-sso-pre-registrations",
        "pk": ("email", S),
        "gsis": [
            {"name": "iam-user-index", "pk": ("iam_user_lookup_key", S)},
        ],
    },
    {
        "name": "stratoclave-api-keys",
        "pk": ("key_hash", S),
        "gsis": [
            {"name": "user-id-index", "pk": ("user_id", S), "sk": ("created_at", S)},
        ],
    },
    {
        "name": "stratoclave-sso-nonces",
        "pk": ("nonce", S), "ttl": "expires_at",
    },
    {
        "name": "stratoclave-ui-tickets",
        "pk": ("ticket_hash", S), "ttl": "expires_at",
    },
    {
        "name": "stratoclave-tenant-budgets",
        "pk": ("tenant_id", S), "sk": ("sk", S), "ttl": "ttl",
        # NEW_AND_OLD_IMAGES in production, feeding the shadow ledger projector
        # (docs/design/ledger-hot-path.md). Nothing in the local demo path
        # consumes this stream; attempted best-effort below.
        "stream": "NEW_AND_OLD_IMAGES",
    },
    {
        "name": "stratoclave-pricing-config",
        "pk": ("pk", S), "sk": ("sk", S),
    },
    {
        "name": "stratoclave-rate-limits",
        "pk": ("pk", S), "ttl": "expires_at",
    },
    {
        "name": "stratoclave-model-quotas",
        "pk": ("pk", S), "sk": ("sk", S), "ttl": "expires_at",
    },
    {
        "name": "stratoclave-observability",
        "pk": ("pk", S), "sk": ("sk", S), "ttl": "expires_at",
        "gsis": [
            {"name": "GSI1", "pk": ("gsi1pk", S), "sk": ("gsi1sk", S)},
        ],
    },
    {
        "name": "stratoclave-routing-signals",
        "pk": ("pk", S), "sk": ("sk", S), "ttl": "expires_at",
    },
    {
        "name": "stratoclave-saar-memory",
        "pk": ("pk", S), "sk": ("sk", S), "ttl": "ttl",
    },
    {
        "name": "stratoclave-credit-ledger",
        "pk": ("pk", S), "sk": ("sk", S),
        "gsis": [
            {"name": "run-index", "pk": ("gsi1pk", S), "sk": ("gsi1sk", S)},
        ],
    },
    {
        # Grant rows: the record of capacity that was given, and the requests that
        # asked for it. The backend reads this table, so a local environment without
        # it cannot exercise the raise flow at all — the failure arrives as a
        # ResourceNotFoundException from a route that looks unrelated.
        "name": "stratoclave-quota-events",
        "pk": ("pk", S), "sk": ("sk", S),
        "gsis": [
            {"name": "tenant-status-index",
             "pk": ("tenant_id", S), "sk": ("status_created_at", S)},
            # INCLUDE, matching the IaC: the sweeper reads only the amount to
            # subtract and the row to subtract it from. Projected faithfully rather
            # than widened, so a query that reads an unprojected attribute fails
            # here too instead of only after deployment.
            {"name": "grant-expiry-index",
             "pk": ("grant_status", S), "sk": ("expires_at", N),
             "projection": ("INCLUDE", [
                 "grant_id", "tenant_id", "approved_amount_microusd",
                 "target_pk", "target_sk", "period",
             ])},
        ],
    },
]


def _attr_defs(spec: dict[str, Any]) -> list[dict[str, str]]:
    """DynamoDB requires exactly the attributes used by the key schema plus
    every GSI's key schema — no more, no less — so this is built from the spec
    rather than hand-duplicated per table."""
    seen: dict[str, str] = {}
    for key in ("pk", "sk"):
        if key in spec:
            name, typ = spec[key]
            seen[name] = typ
    for gsi in spec.get("gsis", []):
        for key in ("pk", "sk"):
            if key in gsi:
                name, typ = gsi[key]
                seen[name] = typ
    return [{"AttributeName": n, "AttributeType": t} for n, t in seen.items()]


def _projection(gsi: dict[str, Any]) -> dict[str, Any]:
    """This index's projection, defaulting to ALL — see the call site for why a
    narrowed projection is carried over rather than widened."""
    spec = gsi.get("projection")
    if spec is None:
        return {"ProjectionType": "ALL"}
    kind, attributes = spec
    if kind == "INCLUDE":
        return {"ProjectionType": "INCLUDE", "NonKeyAttributes": list(attributes)}
    return {"ProjectionType": kind}


def _key_schema(pk: tuple[str, str], sk: Optional[tuple[str, str]]) -> list[dict[str, str]]:
    schema = [{"AttributeName": pk[0], "KeyType": "HASH"}]
    if sk is not None:
        schema.append({"AttributeName": sk[0], "KeyType": "RANGE"})
    return schema


def create_table(client, spec: dict[str, Any]) -> str:
    name = spec["name"]
    try:
        client.describe_table(TableName=name)
        return "exists"
    except client.exceptions.ResourceNotFoundException:
        pass

    kwargs: dict[str, Any] = {
        "TableName": name,
        "AttributeDefinitions": _attr_defs(spec),
        "KeySchema": _key_schema(spec["pk"], spec.get("sk")),
        "BillingMode": "PAY_PER_REQUEST",
    }
    if spec.get("gsis"):
        kwargs["GlobalSecondaryIndexes"] = [
            {
                "IndexName": g["name"],
                "KeySchema": _key_schema(g["pk"], g.get("sk")),
                # ALL by default because that is the CDK default every IaC index
                # but one relies on. Where the IaC narrows a projection, this
                # narrows it too: a WIDER local index is not the harmless superset
                # it looks like, because a query reading an attribute the
                # production index does not project then succeeds here and fails
                # only once deployed — the local environment exists to catch that,
                # not to hide it.
                "Projection": _projection(g),
            }
            for g in spec["gsis"]
        ]
    if spec.get("stream"):
        kwargs["StreamSpecification"] = {
            "StreamEnabled": True,
            "StreamViewType": spec["stream"],
        }

    try:
        client.create_table(**kwargs)
    except Exception as exc:  # noqa: BLE001
        if spec.get("stream"):
            # DynamoDB Local's stream support has historically lagged the real
            # service; retry once without it rather than block the whole demo
            # on a feature nothing in the local path reads.
            print(f"[create_tables] {name}: stream request failed ({exc}); retrying without it")
            kwargs.pop("StreamSpecification", None)
            client.create_table(**kwargs)
        else:
            raise

    _wait_active(client, name)

    if spec.get("ttl"):
        _enable_ttl(client, name, spec["ttl"])

    return "created"


def _wait_active(client, name: str, timeout_s: int = 30) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        status = client.describe_table(TableName=name)["Table"]["TableStatus"]
        if status == "ACTIVE":
            return
        time.sleep(0.5)
    raise SystemExit(f"[create_tables] {name}: did not become ACTIVE within {timeout_s}s")


def _enable_ttl(client, name: str, attribute: str) -> None:
    try:
        client.update_time_to_live(
            TableName=name,
            TimeToLiveSpecification={"Enabled": True, "AttributeName": attribute},
        )
    except Exception as exc:  # noqa: BLE001
        # Known DynamoDB Local limitation: TTL is accepted but expiry is not
        # actually enforced. Enabling it is still worth doing (keeps the
        # schema faithful, and some SDK paths check whether TTL is on), but a
        # local build that rejects the call outright must not block the demo.
        print(f"[create_tables] {name}: TTL enable failed (non-fatal): {exc}")


def main() -> None:
    client = require_local_dynamodb("create_tables")

    created, existed = [], []
    for spec in TABLES:
        result = create_table(client, spec)
        (created if result == "created" else existed).append(spec["name"])

    print(f"\n[create_tables] {len(created)} created, {len(existed)} already existed "
          f"({len(TABLES)} total)")
    for name in created:
        print(f"  created : {name}")
    for name in existed:
        print(f"  exists  : {name}")

    gsi_count = sum(len(s.get("gsis", [])) for s in TABLES)
    print(f"[create_tables] {gsi_count} GSIs declared across all tables")


if __name__ == "__main__":
    main()
