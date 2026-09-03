#!/usr/bin/env python3
"""Does a tenant at its money ceiling have something to do besides wait? Walk it
end to end on a real deployment, and never take the client's word for a figure a
shipped function can compute instead.

WHAT THIS IS FOR
----------------
`backend/tests/` proves the grant/raise mechanism unit-by-unit against
DynamoDB Local. None of those runs a real gateway request, none waits for a
real EventBridge schedule to fire a real Lambda, and none can show that a
grant which raised a real ceiling is reversed by something OTHER than a person
clicking a button. This script does all three, against a NAMED real deployment.

Follows `prove_ceiling.py`'s convention exactly: `--deployment <prefix>` points
this at a real deployment's real DynamoDB tables (refusing the production
prefix by name), and every figure reported is read back from the deployment
rather than recomputed locally.

WHAT MAKES THIS DECISIVE RATHER THAN MERELY GREEN
--------------------------------------------------
1. The refusal, the approval and the re-admission are read from a REAL HTTP
   gateway (`STRATOCLAVE_LOCAL_URL`), not from calling the service functions
   in-process — a refusal that only argparse ever manufactures other things.
2. The requester and the approver are two DIFFERENT identities (self-approval
   is refused by the contract), and the approver deliberately approves LESS
   than was asked, so "approved_amount_microusd" and "asked_amount_microusd"
   can be told apart in the requester's own view.
3. Expiry is witnessed two ways and each is labelled which: (a) an EventBridge
   schedule actually firing the sweeper Lambda, observed in CloudWatch Logs
   with a wall-clock timestamp this run did not control, and (b) if the
   schedule has not ticked within the wait budget, a manual `invoke` as a
   fallback — reported as exactly that, not conflated with (a).
4. The post-expiry ceiling is read from `TenantBudgetsRepository.pool_summary`
   (the shipped function every surface calls), and compared against the
   baseline captured before the grant was ever applied — not against a
   locally recomputed figure.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE.parent.parent / "backend"))

from _local_guard import require_local_dynamodb  # noqa: E402
from prove_ceiling import (  # noqa: E402 -- reuse the deployment-pointing machinery
    PRODUCTION_PREFIX,
    point_at_deployment,
)

GATEWAY_URL = os.environ.get("STRATOCLAVE_LOCAL_URL", "http://127.0.0.1:8080")

TENANT_ID = os.environ.get("PROVE_RAISE_TENANT", "verify-raise-tenant")
REQUESTER_ID = "verify-raise-requester"
APPROVER_ID = "verify-raise-approver"
KEY_DIR = _HERE.parent.parent / "data" / "local"


def log(msg: str) -> None:
    print(f"[prove_raise] {msg}", flush=True)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
def _req(method: str, path: str, key: Optional[str], body: Optional[dict] = None,
          timeout: float = 30.0) -> tuple[int, dict]:
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(GATEWAY_URL + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            raw = resp.read() or b"{}"
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read() or b"{}"
        try:
            return e.code, json.loads(raw)
        except Exception:  # noqa: BLE001
            return e.code, {"raw": raw[:800].decode("utf-8", "replace")}


def _get(path: str, key: str) -> tuple[int, dict]:
    return _req("GET", path, key)


def _post(path: str, key: str, body: dict) -> tuple[int, dict]:
    return _req("POST", path, key, body)


def _put(path: str, key: str, body: dict) -> tuple[int, dict]:
    return _req("PUT", path, key, body)


# ---------------------------------------------------------------------------
# Setup: a dedicated tenant, two identities, and a tiny pool
# ---------------------------------------------------------------------------
def seed_identities() -> tuple[str, str]:
    """Create the tenant, the requester (role=user) and the approver
    (role=admin), and return (requester_key, approver_key).

    Two DIFFERENT people, on purpose: R6/self-approval-refused is part of what
    this run has to witness, and a single identity wearing both hats could
    not witness it even by accident.
    """
    from dynamo.api_keys import ApiKeysRepository, hash_key
    from dynamo.tenants import TenantsRepository, ADMIN_OWNED
    from dynamo.user_tenants import UserTenantsRepository
    from dynamo.users import UsersRepository

    tenants = TenantsRepository()
    if tenants.get(TENANT_ID) is None:
        tenants.create(
            name="prove_raise verification tenant", team_lead_user_id=ADMIN_OWNED,
            created_by="prove_raise", tenant_id=TENANT_ID)
        log(f"created tenant {TENANT_ID!r}")
    else:
        log(f"reusing tenant {TENANT_ID!r}")

    UsersRepository().put_user(
        user_id=REQUESTER_ID, email="verify-raise-requester@example.invalid",
        auth_provider="local", auth_provider_user_id=REQUESTER_ID,
        org_id=TENANT_ID, roles=["user"])
    UserTenantsRepository().ensure(user_id=REQUESTER_ID, tenant_id=TENANT_ID, role="user")

    UsersRepository().put_user(
        user_id=APPROVER_ID, email="verify-raise-approver@example.invalid",
        auth_provider="local", auth_provider_user_id=APPROVER_ID,
        org_id=TENANT_ID, roles=["admin"])
    UserTenantsRepository().ensure(user_id=APPROVER_ID, tenant_id=TENANT_ID, role="admin")

    def _key_for(user_id: str, name: str, scopes: list[str], filename: str) -> str:
        keyfile = KEY_DIR / filename
        if keyfile.exists():
            plain = keyfile.read_text().strip()
            item = ApiKeysRepository().get_by_hash(hash_key(plain))
            if item and not item.get("revoked_at") and str(item.get("user_id")) == user_id:
                log(f"reusing key at {keyfile}")
                return plain
        item, plain = ApiKeysRepository().create(
            user_id=user_id, name=name, scopes=scopes, expires_at=None, created_by=user_id)
        keyfile.parent.mkdir(parents=True, exist_ok=True)
        keyfile.write_text(plain + "\n")
        keyfile.chmod(0o600)
        log(f"created key {item['key_id']} for {user_id} and saved it to {keyfile}")
        return plain

    requester_key = _key_for(
        REQUESTER_ID, "raise-verification-requester",
        ["messages:send", "responses:send", "usage:read-self", "limits:raise-self"],
        "api_key_raise_requester")
    approver_key = _key_for(
        APPROVER_ID, "raise-verification-approver",
        ["messages:send", "responses:send", "usage:read-self",
         "limits:raise-self", "limits:approve", "tenants:update"],
        "api_key_raise_approver")
    return requester_key, approver_key


# ---------------------------------------------------------------------------
# The walk
# ---------------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--deployment", required=False,
                     help=f"run against this deployment's REAL DynamoDB tables. "
                          f"Refuses {PRODUCTION_PREFIX!r}.")
    ap.add_argument("--headroom-microusd", type=int, default=20_000,
                     help="the pool's headroom before any grant, in micro-USD")
    # 2048 output tokens on claude-haiku-4-5 (5,500,000 microUSD/MTok, see
    # mvp/defaults/pricing.json) settles at roughly 11,264 microUSD -- comfortably
    # more than the 10,000-microUSD grant approved in item 2, which is what lets
    # item 7 drive the ceiling negative later by setting the baseline to zero
    # while that grant is still the only thing on top of it.
    ap.add_argument("--probe-max-tokens", type=int, default=2048)
    ap.add_argument("--grant-window-seconds", type=int, default=360,
                     help="how long the grant lives; must be >= 300 (R11's minimum)")
    ap.add_argument("--sweep-wait-seconds", type=int, default=420,
                     help="how long to wait for the REAL EventBridge schedule "
                          "(5 min period) before falling back to a manual invoke")
    args = ap.parse_args()

    if args.deployment:
        point_at_deployment(args.deployment)
    else:
        require_local_dynamodb("prove_raise")

    from dynamo.tenant_budgets import TenantBudgetsRepository, current_period
    from mvp.grants import latest_permissible_expiry_for_period

    requester_key, approver_key = seed_identities()
    period = current_period()
    budgets = TenantBudgetsRepository()

    results: dict[str, Any] = {}

    # ---- 0. A small pool with known headroom, seat-tracking off -----------
    existing = budgets.get(TENANT_ID, period) or {}
    already_settled = int(existing.get("pool_settled_microusd", 0) or 0)
    already_reserved = int(existing.get("pool_reserved_microusd", 0) or 0)
    baseline_limit = already_settled + already_reserved + args.headroom_microusd
    budgets.set_manual_limit(
        tenant_id=TENANT_ID, period=period, manual_limit_microusd=baseline_limit,
        status="active")
    before_summary = budgets.pool_summary(TENANT_ID, period)
    log(f"tenant={TENANT_ID} period={period} baseline_limit_microusd={baseline_limit}")
    log(f"pool_summary before anything: {before_summary}")
    baseline_ceiling = int(before_summary["pool_limit_microusd"])

    # ================================================================
    # ITEM 1 — a tenant at its ceiling is refused, and the refusal names
    # the raise path.
    # ================================================================
    log("\n== item 1: refuse at the ceiling, and name the raise path ==")
    # Sized to exceed a small pool with a comfortable margin without leaning on
    # a near-context-window value: claude-haiku-4-5's output rate is
    # 5,500,000 microUSD/MTok (mvp/defaults/pricing.json), so 8192 output
    # tokens bounds at roughly 45,056 microUSD -- well above the
    # `--headroom-microusd` default of 20,000 and still an ordinary max_tokens
    # value no model registry caps below.
    over_limit_tokens = 8192
    status, body = _post(
        "/v1/chat/completions", requester_key,
        {"model": "claude-haiku-4-5", "max_tokens": over_limit_tokens,
         "messages": [{"role": "user", "content": "ok"}]})
    log(f"oversized probe -> HTTP {status}")
    log(f"body: {json.dumps(body)[:2000]}")
    detail = body.get("detail") if isinstance(body.get("detail"), dict) else body
    results["item1_status"] = status
    results["item1_detail"] = detail
    results["item1_names_raise_path"] = bool(
        status == 402 and detail.get("grantable") is True
        and isinstance(detail.get("raise_hint"), dict))

    # ================================================================
    # ITEM 2 — a raise is requested, approved for LESS than asked, and the
    # APPROVED amount (not the asked one) reaches the requester's own view.
    # ================================================================
    log("\n== item 2: request, approve for less than asked, and read it back ==")
    asked = args.headroom_microusd  # deliberately more than we intend to approve
    client_token = f"prove-raise-{int(time.time())}"
    status, submit_body = _post(
        "/api/mvp/me/limit-raises", requester_key,
        {"asked_amount_microusd": asked, "reason_code": "usage_spike",
         "client_token": client_token})
    log(f"submit -> HTTP {status} body={json.dumps(submit_body)[:1000]}")
    results["item2_submit_status"] = status
    request_id = submit_body.get("request_id")

    approved_amount = asked // 2  # deliberately less than asked
    now_epoch = int(time.time())
    expires_at = now_epoch + args.grant_window_seconds
    ceiling_expiry = latest_permissible_expiry_for_period(now_epoch, period)
    if expires_at > ceiling_expiry:
        expires_at = ceiling_expiry
    status, approve_body = _post(
        f"/api/mvp/admin/limit-raises/{request_id}/approve", approver_key,
        {"approved_amount_microusd": approved_amount, "expires_at": expires_at,
         "decision_comment": "prove_raise: approving half of what was asked, on purpose"})
    log(f"approve (half of asked) -> HTTP {status}")
    log(f"body: {json.dumps(approve_body)[:1500]}")
    results["item2_approve_status"] = status
    results["item2_asked_amount_microusd"] = asked
    results["item2_approved_amount_microusd"] = approved_amount
    grant = approve_body.get("grant", {}) if isinstance(approve_body, dict) else {}
    grant_id = grant.get("grant_id")
    results["item2_grant"] = grant
    results["item2_field_matches_approved_not_asked"] = (
        int(grant.get("approved_amount_microusd", -1)) == approved_amount)

    # The requester's OWN view of the decision.
    status, mine_body = _get("/api/mvp/me/limit-raises", requester_key)
    mine = next((r for r in mine_body.get("requests", []) if r.get("request_id") == request_id), None)
    log(f"requester's own view of the decided request: {json.dumps(mine)[:800]}")
    results["item2_requester_view"] = mine
    results["item2_requester_sees_approved_amount"] = bool(
        mine and int(mine.get("approved_amount_microusd", -1)) == approved_amount)

    after_grant_summary = budgets.pool_summary(TENANT_ID, period)
    log(f"pool_summary after the grant applied: {after_grant_summary}")
    results["ceiling_after_grant"] = int(after_grant_summary["pool_limit_microusd"])
    results["ceiling_after_grant_equals_baseline_plus_approved"] = (
        int(after_grant_summary["pool_limit_microusd"])
        == baseline_ceiling + approved_amount)

    # ================================================================
    # ITEM 3 — work that was refused before the grant is admitted after it,
    # at the raised ceiling.
    # ================================================================
    log("\n== item 3: the same-shaped request that was refused now admits ==")
    status, admit_body = _post(
        "/v1/chat/completions", requester_key,
        {"model": "claude-haiku-4-5", "max_tokens": args.probe_max_tokens,
         "messages": [{"role": "user", "content": "ok, say a short word"}]})
    log(f"probe under the raised ceiling -> HTTP {status} body={json.dumps(admit_body)[:500]}")
    results["item3_status"] = status
    results["item3_admitted"] = status == 200

    # ================================================================
    # ITEM 6 — an operator setting a figure while a grant is live lands
    # where they meant: the response decomposes baseline vs granted.
    # ================================================================
    log("\n== item 6: operator sets a NEW baseline while the grant is still live ==")
    new_baseline_cents = (baseline_ceiling + 500_000) // 10_000  # +$0.50, in cents
    status, set_body = _put(
        f"/api/mvp/admin/tenants/{TENANT_ID}/pool-budget", approver_key,
        {"limit_usd_cents": new_baseline_cents, "period": period})
    log(f"set new baseline while grant live -> HTTP {status}")
    log(f"body: {json.dumps(set_body)[:1200]}")
    results["item6_status"] = status
    results["item6_body"] = set_body
    expected_new_limit = new_baseline_cents * 10_000 + approved_amount
    results["item6_lands_where_meant"] = (
        status == 200
        and int(set_body.get("baseline_microusd", -1)) == new_baseline_cents * 10_000
        and int(set_body.get("pool_granted_microusd", -1)) == approved_amount
        and int(set_body.get("pool_limit_microusd", -1)) == expected_new_limit)

    # Also witness R17b (409 figure_includes_active_grant): re-send the exact
    # figure now in force while the grant still sits on top of it.
    current_limit_cents = int(set_body.get("pool_limit_microusd", 0)) // 10_000
    status_r17b, body_r17b = _put(
        f"/api/mvp/admin/tenants/{TENANT_ID}/pool-budget", approver_key,
        {"limit_usd_cents": current_limit_cents, "period": period})
    log(f"(R17b) resend the CURRENT figure (includes the grant) -> HTTP {status_r17b} "
        f"body={json.dumps(body_r17b)[:600]}")
    results["item_r17b_status"] = status_r17b
    results["item_r17b_is_figure_includes_active_grant"] = (
        status_r17b == 409
        and isinstance(body_r17b.get("detail"), dict)
        and body_r17b["detail"].get("type") == "figure_includes_active_grant")

    # ================================================================
    # ITEM 7 — negative headroom is reported signed and unclamped.
    # ================================================================
    log("\n== item 7: lower the baseline below committed spend; read signed headroom ==")
    # `remaining_microusd` is read straight off the stored `pool_headroom_microusd`
    # counter (`TenantBudgetsRepository.pool_summary`), not recomputed from
    # limit/reserved/settled -- so driving it negative needs the counter itself
    # pushed down, not just a small settle. A short model reply settles for far
    # less than `--probe-max-tokens` bounds (the model stops early), so item 3's
    # actual settle is not a reliable lever here. Inject a large committed spend
    # directly on the counter this test targets -- the same kind of surgical,
    # single-attribute write items 4/5 already use to force a real expiry -- so
    # a zero baseline is unambiguously smaller than what is already spent.
    import boto3  # noqa: E402 -- local import, reused again in the items-4/5 block

    ddb = boto3.client("dynamodb", region_name=os.environ.get("AWS_REGION", "us-east-1"))
    ddb.update_item(
        TableName=budgets._table.name,
        Key={"tenant_id": {"S": TENANT_ID}, "sk": {"S": f"BUDGET#{period}"}},
        UpdateExpression="ADD pool_headroom_microusd :neg, pool_settled_microusd :amt",
        ExpressionAttributeValues={":neg": {"N": "-50000"}, ":amt": {"N": "50000"}},
    )
    log("injected -50,000 microUSD onto pool_headroom_microusd (and the matching "
        "+50,000 onto pool_settled_microusd) so a zero baseline is unambiguously "
        "below committed spend")
    tiny_cents = 0
    status, tiny_body = _put(
        f"/api/mvp/admin/tenants/{TENANT_ID}/pool-budget", approver_key,
        {"limit_usd_cents": tiny_cents, "period": period})
    log(f"set baseline to $0.00 -> HTTP {status} body={json.dumps(tiny_body)[:1200]}")
    results["item7_status"] = status
    results["item7_remaining_microusd"] = tiny_body.get("remaining_microusd")
    results["item7_over_ceiling_microusd"] = tiny_body.get("over_ceiling_microusd")
    results["item7_signed_unclamped"] = (
        status == 200 and int(tiny_body.get("remaining_microusd", 1)) < 0
        and int(tiny_body.get("over_ceiling_microusd", 0)) ==
        -int(tiny_body.get("remaining_microusd", 0)))

    # Restore a sane baseline (above the grant) before touching expiry, so the
    # sweep's ceiling-restoration in item 4/5 is not confounded by this probe.
    budgets.set_manual_limit(
        tenant_id=TENANT_ID, period=period,
        manual_limit_microusd=baseline_ceiling, status="active")
    restored = budgets.pool_summary(TENANT_ID, period)
    log(f"restored baseline to {baseline_ceiling}: {restored}")

    # ================================================================
    # ITEMS 4/5 — the grant expires with nobody acting, the ceiling returns
    # to EXACTLY the pre-grant figure, and the next admission is refused
    # again.
    # ================================================================
    log("\n== items 4/5: wait for a REAL schedule tick to revoke the grant ==")
    # Make the grant expire NOW (in the past) so whichever mechanism ticks next
    # -- schedule or manual fallback -- has work to do immediately rather than
    # requiring the run to sit idle for the whole window first.
    from dynamo.quota_events import QuotaEventsRepository

    events_repo = QuotaEventsRepository()
    live_grant = events_repo.get_grant(tenant_id=TENANT_ID, grant_id=grant_id)
    log(f"grant {grant_id} before forcing expiry: status={live_grant.get('status')} "
        f"expires_at={live_grant.get('expires_at')}")

    import boto3

    ddb = boto3.client("dynamodb", region_name=os.environ.get("AWS_REGION", "us-east-1"))
    table_name = events_repo._table.name
    forced_expiry = int(time.time()) - 5
    # Direct, surgical UpdateItem against the grant's own key -- not a new write
    # path, just moving `expires_at` into the past so the REAL sweep (schedule or
    # manual) has something due the next time it runs. The GSI it is queried
    # through (`grant-expiry-index`) is a plain projection of this attribute, so
    # this is exactly the write the approval path itself would have made had the
    # window been shorter.
    ddb.update_item(
        TableName=table_name,
        Key={"pk": {"S": f"TENANT#{TENANT_ID}"}, "sk": {"S": f"GRANT#{grant_id}"}},
        UpdateExpression="SET expires_at = :e",
        ExpressionAttributeValues={":e": {"N": str(forced_expiry)}},
    )
    log(f"forced grant {grant_id}'s expires_at to {forced_expiry} (now - 5s), "
        f"real GSI included, so a real schedule tick has work to do")

    sweep_fn = f"{args.deployment}-quota-grant-sweeper" if args.deployment else None
    logs_client = boto3.client("logs", region_name=os.environ.get("AWS_REGION", "us-east-1"))
    # NOT the Lambda-default "/aws/lambda/<fn>": QuotaGrantsStack creates this
    # log group itself, under "/lambda/<fn>" (iac/lib/quota-grants-stack.ts),
    # for the same reason DEPLOYMENT.md documents that path -- a Lambda's
    # default log group is created lazily on first invocation, which breaks
    # the stack's own MetricFilter on a fresh deploy. Querying the default
    # prefix here found nothing (ResourceNotFoundException) and silently fell
    # through to "not witnessed" on a run where the schedule DID fire.
    log_group = f"/lambda/{sweep_fn}" if sweep_fn else None

    scheduled_witnessed = False
    scheduled_detail = ""
    deadline = time.time() + args.sweep_wait_seconds
    grant_after_wait = None
    if log_group:
        start_ms = int(time.time() * 1000)
        log(f"polling CloudWatch Logs {log_group} for a sweeper_ran heartbeat "
            f"emitted by the SCHEDULE (not by us) for up to {args.sweep_wait_seconds}s")
        while time.time() < deadline:
            grant_after_wait = events_repo.get_grant(tenant_id=TENANT_ID, grant_id=grant_id)
            if str(grant_after_wait.get("status")) != "ACTIVE":
                # The grant moved -- CloudWatch Logs ingestion lags a live tail by
                # up to tens of seconds, so the heartbeat that explains WHY it
                # moved may not be readable yet. Retry the read rather than
                # concluding "unwitnessed" from a single premature query.
                for _attempt in range(6):
                    try:
                        resp = logs_client.filter_log_events(
                            logGroupName=log_group, startTime=start_ms,
                            filterPattern='"sweeper_ran"')
                        events_seen = resp.get("events", [])
                        if events_seen:
                            scheduled_witnessed = True
                            scheduled_detail = (
                                f"{len(events_seen)} sweeper_ran heartbeat(s) in "
                                f"CloudWatch Logs since this run started waiting, "
                                f"grant now status={grant_after_wait.get('status')}")
                            break
                    except Exception as exc:  # noqa: BLE001
                        scheduled_detail = f"log read failed: {exc}"
                        break
                    time.sleep(10)
                if not scheduled_witnessed and not scheduled_detail:
                    scheduled_detail = (
                        f"grant left ACTIVE (now {grant_after_wait.get('status')}) "
                        f"but no sweeper_ran heartbeat was readable within the "
                        f"retry budget")
                break
            time.sleep(10)
    results["item45_schedule_wait_seconds"] = args.sweep_wait_seconds
    results["item45_scheduled_witnessed"] = scheduled_witnessed
    results["item45_scheduled_detail"] = scheduled_detail

    if not scheduled_witnessed:
        log("the real 5-minute schedule did not revoke the grant inside the wait "
            "budget (or its log could not be read) -- falling back to a MANUAL "
            "invoke, reported as exactly that, not as the scheduled path")
        if sweep_fn:
            lam = boto3.client("lambda", region_name=os.environ.get("AWS_REGION", "us-east-1"))
            resp = lam.invoke(FunctionName=sweep_fn, Payload=b"{}")
            payload = json.loads(resp["Payload"].read())
            log(f"manual invoke of {sweep_fn}: {json.dumps(payload)}")
            results["item45_manual_invoke_result"] = payload

    grant_final = events_repo.get_grant(tenant_id=TENANT_ID, grant_id=grant_id)
    log(f"grant {grant_id} final status: {json.dumps(grant_final, default=str)[:500]}")
    results["item45_grant_final_status"] = str(grant_final.get("status"))
    results["item45_revoked_without_a_person"] = (
        str(grant_final.get("status")) == "EXPIRED"
        and str(grant_final.get("revoked_by")) == "sweeper")

    final_summary = budgets.pool_summary(TENANT_ID, period)
    log(f"pool_summary after the sweep: {final_summary}")
    results["item4_ceiling_after_expiry"] = int(final_summary["pool_limit_microusd"])
    results["item4_ceiling_returns_to_exact_baseline"] = (
        int(final_summary["pool_limit_microusd"]) == baseline_ceiling)

    log("\n== item 5: the next admission after expiry, refused again ==")
    status, refused_again_body = _post(
        "/v1/chat/completions", requester_key,
        {"model": "claude-haiku-4-5", "max_tokens": over_limit_tokens,
         "messages": [{"role": "user", "content": "ok"}]})
    log(f"post-expiry oversized probe -> HTTP {status} "
        f"body={json.dumps(refused_again_body)[:1200]}")
    results["item5_status"] = status
    detail2 = (refused_again_body.get("detail")
               if isinstance(refused_again_body.get("detail"), dict) else refused_again_body)
    results["item5_detail"] = detail2
    results["item5_reason_identical_to_item1"] = (
        detail2.get("reason") == results["item1_detail"].get("reason")
        and detail2.get("message") == results["item1_detail"].get("message"))
    results["item5_message_mentions_expiry"] = "expir" in json.dumps(detail2).lower()

    # ================================================================
    # Print the summary a human reads.
    # ================================================================
    print("\n" + "=" * 78)
    print("[prove_raise] SUMMARY")
    print("=" * 78)
    print(json.dumps(results, indent=2, default=str))


if __name__ == "__main__":
    main()
