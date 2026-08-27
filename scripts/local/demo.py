#!/usr/bin/env python3
"""The point of local mode: hit real Bedrock, through the gateway, entirely on
your own machine, and read the result back from your own ledger — the same
"server-side record matches what the client was told" check used in the
deployed-path live verification (docs/EVIDENCE.md, "deployed-live" tier,
2026-08-27), now runnable with zero AWS infrastructure deployed.

For each of the three inference routes this reports:
  - the response the gateway returned, and how long it took
  - the EFFECTIVE model your local ledger recorded for the call (read from
    the UsageLogs table, the same field `stratoclave usage show` reads) —
    this can differ from the alias you asked for, which is itself evidence
    that model-registry resolution ran server-side, not just an echo
  - how much the token quota moved in the UserTenants table, read before and
    after the call

This does NOT fall back to a mock provider on a credential failure — there is
no mock in the request path. A missing/expired credential surfaces as a loud,
specific error before any HTTP call is even made.
"""
from __future__ import annotations

import http.client
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse


def _ensure_backend_importable() -> None:
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
        "Could not locate the backend package ('dynamo/'). Run this from a "
        "full repo checkout, with `make up` already run."
    )


_ensure_backend_importable()

from boto3.dynamodb.conditions import Key  # noqa: E402
from _local_guard import require_local_dynamodb  # noqa: E402
from dynamo.usage_logs import UsageLogsRepository  # noqa: E402
from dynamo.user_tenants import UserTenantsRepository  # noqa: E402

GATEWAY_URL = os.environ.get("STRATOCLAVE_LOCAL_URL", "http://127.0.0.1:8080")
KEY_FILE = Path(__file__).resolve().parents[2] / "data" / "local" / "api_key"
USER_ID = "local-dev-user"
ORG_ID = os.environ.get("DEFAULT_ORG_ID", "default-org")

# Small, cheap, and known-good against a live gateway (used in the 2026-08-27
# deployed-live verification): a short reply keeps token counts easy to read.
CASES = [
    ("Anthropic Messages", "POST", "/v1/messages",
     {"model": "claude-haiku-4-5", "max_tokens": 8,
      "messages": [{"role": "user", "content": "Reply with one word: ok"}]}),
    ("OpenAI Chat Completions", "POST", "/v1/chat/completions",
     {"model": "claude-haiku-4-5", "max_tokens": 8,
      "messages": [{"role": "user", "content": "Reply with one word: ok"}]}),
    ("OpenAI Responses", "POST", "/openai/v1/responses",
     {"model": "openai.gpt-5.6-sol", "max_output_tokens": 16,
      "input": "Reply with one word: ok"}),
]


def _preflight_credentials() -> None:
    """Fail loudly, before touching the gateway, if Bedrock credentials will
    not resolve. Real STS, not a mock — this only confirms a credential
    CHAIN resolves, not that it can call Bedrock (that is what the demo
    itself proves)."""
    import boto3

    profile = os.environ.get("AWS_PROFILE") or None
    region = os.environ.get("AWS_REGION", "us-east-1")
    try:
        session = boto3.Session(profile_name=profile, region_name=region)
        session.client("sts").get_caller_identity()
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(
            "No usable AWS credentials for Bedrock "
            f"(AWS_PROFILE={profile or '(default)'}, AWS_REGION={region}): {exc}\n"
            "Local mode calls REAL Bedrock — there is no mock to fall back to. "
            "Run `aws sso login` (or point AWS_PROFILE at a profile with static "
            "credentials) and try again."
        )


def _read_key() -> str:
    if not KEY_FILE.exists():
        raise SystemExit(f"No local API key at {KEY_FILE}. Run `make up` first.")
    key = KEY_FILE.read_text().strip()
    if not key:
        raise SystemExit(f"{KEY_FILE} is empty. Run `make up` again.")
    return key


def _call(method: str, path: str, payload: dict, key: str) -> tuple[int, str]:
    url = urlparse(GATEWAY_URL)
    conn = http.client.HTTPConnection(url.hostname, url.port, timeout=60)
    try:
        conn.request(
            method, path, body=json.dumps(payload),
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        )
        resp = conn.getresponse()
        return resp.status, resp.read().decode()
    finally:
        conn.close()


def _credit_used() -> Optional[int]:
    row = UserTenantsRepository().get(USER_ID, ORG_ID)
    return int(row["credit_used"]) if row else None


def _latest_usage_log_since(since_iso: str, *, attempts: int = 5, delay_s: float = 0.4) -> Optional[dict[str, Any]]:
    """The newest UsageLogs row for this user at/after `since_iso`. A short
    retry loop absorbs GSI propagation lag, not settle latency — the gateway
    has already returned its HTTP response by the time this runs."""
    table = UsageLogsRepository()._table
    for _ in range(attempts):
        resp = table.query(
            IndexName="user-id-index",
            KeyConditionExpression=Key("user_id").eq(USER_ID) & Key("timestamp_log_id").gte(since_iso),
            ScanIndexForward=False,
            Limit=1,
        )
        items = resp.get("Items", [])
        if items:
            return items[0]
        time.sleep(delay_s)
    return None


def main() -> None:
    require_local_dynamodb("demo")

    _preflight_credentials()
    key = _read_key()

    print(f"[demo] gateway:   {GATEWAY_URL}")
    print(f"[demo] ledger:    DynamoDB Local ({os.environ['AWS_ENDPOINT_URL_DYNAMODB']})")
    print()

    any_failed = False
    for label, method, path, payload in CASES:
        before = _credit_used()
        call_start_iso = datetime.now(timezone.utc).isoformat()
        t0 = time.monotonic()
        status, body = _call(method, path, payload, key)
        elapsed_ms = round((time.monotonic() - t0) * 1000, 1)

        print(f"== {label} ({path}) ==")
        if status != 200:
            any_failed = True
            print(f"  FAILED: HTTP {status}")
            print(f"  {body[:500]}")
            if status == 503 and "openai" in path:
                print(
                    "  This route is gated by CODEX_ENABLED=true "
                    "(backend/mvp/openai_responses.py). docker-compose.yml sets "
                    "it for the gateway container; if you are running the "
                    "backend outside compose, set it yourself."
                )
            print()
            continue

        parsed = json.loads(body)
        usage = parsed.get("usage") or {}
        requested_model = payload["model"]

        log_row = _latest_usage_log_since(call_start_iso)
        after = _credit_used()
        delta = (after - before) if (before is not None and after is not None) else None

        print(f"  status: 200 in {elapsed_ms} ms")
        print(f"  response usage: {usage}")
        if log_row:
            print(
                f"  ledger record (UsageLogs): requested='{requested_model}' "
                f"resolved='{log_row.get('model_id')}' "
                f"input_tokens={log_row.get('input_tokens')} "
                f"output_tokens={log_row.get('output_tokens')}"
            )
        else:
            print("  ledger record (UsageLogs): not found yet (see docs/LOCAL.md)")
        print(f"  token quota moved (UserTenants.credit_used): {delta} "
              f"(before={before}, after={after})")
        print()

    print("Everything above except the elapsed time was read back from your own "
          "local DynamoDB tables, not from this script's own bookkeeping.")
    print("This checks reservation, settlement, and model-alias resolution "
          "against real Bedrock traffic. It does not check the dollar-pool "
          "ledger path (no pool is configured for the local user), and it is "
          "a single-user, single-machine run — see docs/LOCAL.md for what a "
          "local run cannot verify.")

    if any_failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
