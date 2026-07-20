"""Generate ONE Savings Certificate from the REAL engine on LIVE scverify infra,
over a realistic MULTI-SCENARIO synthetic traffic seed. The output is the demo /
sales one-time asset — a certificate the real engine produced on real infra, not a
hand-written sample (Fable savings-certificate review: provenance-guaranteed demo).

Scenarios seeded (a realistic tenant-day mix):
  * 3x VSR advised a CHEAPER model, tenant did NOT follow (billed dear) -> SAVING
  * 1x VSR advised a DEARER model (escalation), tenant billed cheap        -> LOSS
  * 2x tenant FOLLOWED the VSR (billed == suggested)                       -> no delta
  * 1x request with no VSR steering                                        -> no_suggestion

Runs sv.savings_certificate(traffic="synthetic") so the cert self-labels as a
seeded sample, then prints it via the CLI's human formatter, then cleans up every
row it wrote (bounded to a throwaway tenant).

Run on EC2 with AWS_REGION=us-east-1 and the scverify DYNAMODB_* env set.
"""
import os
import sys
import uuid

sys.path.insert(0, "/home/coder/stratoclave/backend")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ["DYNAMODB_ROUTING_SIGNALS_TABLE"] = "scverify-routing-signals"
os.environ["DYNAMODB_USAGE_LOGS_TABLE"] = "scverify-usage-logs"

from mvp.learning import decision_log as dl                # noqa: E402
from mvp.learning import savings as sv                     # noqa: E402
from mvp.learning import savings_cli as cli                # noqa: E402
from mvp.vsr.client import DECISION_HARD_APPLIED, DECISION_PREFER_APPLIED  # noqa: E402
from dynamo import UsageLogsRepository                      # noqa: E402

TENANT = f"demo-savings-{uuid.uuid4().hex[:8]}"
NOW = dl._now_ms()
DAY = dl._day(NOW)
OPUS = "claude-opus-4-7"
HAIKU = "claude-haiku-4-5"
OPUS_ID = "us.anthropic.claude-opus-4-7"
HAIKU_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"


def _price(pkey, tin, tout):
    from mvp.pricing import actual_cost_microusd
    return actual_cost_microusd(pricing_key=pkey, input_tokens=tin, output_tokens=tout)


def seed_decision(span, requested, chosen, suggested, decision):
    dl._put(dl.build_decision_item(
        tenant_id=TENANT, run_id="wf-demo", span_id=span, group_id=None,
        requested_model=requested, selection_reason=None, fallback_reason=None,
        chosen={"model": chosen}, rejected=[], estimate_inputs={}, created_at_ms=NOW,
        vsr=({"decision": decision, "suggested_model": suggested,
              "mode": "hard" if decision == DECISION_HARD_APPLIED else "prefer",
              "config_version": "demo-v1"} if suggested else None)))


def seed_usage(span, model_id, tin, tout, pkey):
    UsageLogsRepository().record(
        tenant_id=TENANT, user_id="u-demo", user_email="demo@example.com",
        model_id=model_id, input_tokens=tin, output_tokens=tout,
        request_id=span, cost_microusd=_price(pkey, tin, tout))


def main():
    scen = []
    # 3x SAVING: VSR advised haiku (cheap), tenant billed opus (dear).
    for i in range(3):
        s = f"req-save-{i}"
        seed_decision(s, OPUS, OPUS, HAIKU, DECISION_HARD_APPLIED)
        seed_usage(s, OPUS_ID, 10_000, 2_000, "opus")
        scen.append(s)
    # 1x LOSS: VSR advised opus (dear), tenant billed haiku (cheap) -> following costs more.
    s = "req-loss-0"
    seed_decision(s, HAIKU, HAIKU, OPUS, DECISION_HARD_APPLIED)
    seed_usage(s, HAIKU_ID, 8_000, 1_500, "haiku")
    scen.append(s)
    # 2x FOLLOWED: tenant billed the SAME model the VSR suggested (no delta).
    for i in range(2):
        s = f"req-followed-{i}"
        seed_decision(s, HAIKU, HAIKU, HAIKU, DECISION_PREFER_APPLIED)
        seed_usage(s, HAIKU_ID, 5_000, 800, "haiku")
        scen.append(s)
    # 1x no VSR steering.
    s = "req-nosug-0"
    seed_decision(s, OPUS, OPUS, None, None)
    seed_usage(s, OPUS_ID, 3_000, 500, "opus")
    scen.append(s)

    import time
    time.sleep(1)  # let strongly-consistent reads settle

    # THE REAL ENGINE on LIVE infra, self-labeled synthetic.
    cert = sv.savings_certificate(tenant_id=TENANT, day=DAY, traffic="synthetic")

    print("\n" + "=" * 70)
    print("DEMO SAVINGS CERTIFICATE (real engine, live scverify, synthetic traffic)")
    print("=" * 70)
    # render via the shipped CLI formatter by monkeypatching the fetch.
    sv_cert = sv.savings_certificate
    sv.savings_certificate = lambda **kw: cert
    try:
        cli.main(["--tenant", TENANT, "--day", DAY, "--traffic", "synthetic"])
    finally:
        sv.savings_certificate = sv_cert

    s = cert["savings"]
    # honest expectations: 3 saving + 1 loss = 4 counterfactual; 2 followed. The
    # no-VSR-steering request produces NO join row (the reconcile is decision-driven
    # over VSR-acted requests), so it is correctly absent from class_counts.
    cc = s["class_counts"]
    ok = (cert["traffic"] == "synthetic"
          and cc.get("counterfactual") == 4
          and cc.get("followed") == 2
          and s["quality"]["measured"] is False)
    print(f"\n[assert] class_counts={cc} net={s['net_saving_microusd']} "
          f"traffic={cert['traffic']} quality_measured={s['quality']['measured']}")

    # cleanup.
    from boto3.dynamodb.conditions import Key as _K
    from dynamo.client import get_dynamodb_resource
    ut = UsageLogsRepository()._table
    ur = ut.query(KeyConditionExpression=_K("tenant_id").eq(TENANT))
    for it in ur.get("Items", []):
        ut.delete_item(Key={"tenant_id": TENANT, "timestamp_log_id": it["timestamp_log_id"]})
    sig = get_dynamodb_resource().Table(dl.signals_table_name())
    rows = list(dl.query_day(tenant_id=TENANT, day=DAY))
    for r in rows:
        sig.delete_item(Key={"pk": r["pk"], "sk": r["sk"]})
    print(f"[cleanup] usage={len(ur.get('Items', []))} decisions={len(rows)}")

    print("DEMO_CERT_RESULT:", "PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
