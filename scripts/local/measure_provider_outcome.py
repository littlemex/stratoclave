#!/usr/bin/env python3
"""Measure what a failed Bedrock call costs, against the provider's own counters.

`docs/MEASUREMENTS.md` is the write-up; this is the harness behind it. AWS does not
document which failures are billed, so every row there is a measurement rather than
a reading of the docs, and this script is how the measurement is repeated.

Method, and why each part of it is load-bearing:

* **The provider's counters, not our estimate.** Each condition is read back from
  CloudWatch `AWS/Bedrock` (`Invocations`, `InvocationClientErrors`,
  `InputTokenCount`, `OutputTokenCount`). A client-side estimate cannot answer the
  question, because the question is what the provider charged for a call the client
  never saw the end of.
* **One condition per counter minute.** Metrics arrive at 1-minute resolution, so
  two conditions in one minute are one number. The script paces itself to the
  minute boundary and records which minute each condition owns.
* **A model the account otherwise never invokes.** Any other traffic on the same
  model in the same minute lands in the same counter. Pick a model nothing else
  uses; the script cannot check this for you, and it says so in its output.
* **An empty counter minute is not a zero.** If CloudWatch returns no datapoints,
  that is reported as `no_data` and the run is INCONCLUSIVE for that condition.
  Reporting an unmeasured zero here would manufacture exactly the belief this
  document exists to refute.

It spends real money — a few cents of on-demand tokens — and it deliberately
abandons one call mid-generation, which is the charge being measured.

Usage:

    python3 scripts/local/measure_provider_outcome.py --region us-east-1 \
        --model us.anthropic.claude-haiku-4-5-20251001-v1:0 \
        --out /tmp/provider-outcome.json

    # the retry finding needs no account and no money:
    python3 scripts/local/measure_provider_outcome.py --retry-arithmetic-only
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

METRICS = ("Invocations", "InvocationClientErrors", "InputTokenCount", "OutputTokenCount")

#: Long enough that the model is still generating when the deadline fires. This is
#: the whole point of the abandoned-call condition: the timeout must land
#: mid-generation, not after the model finished.
ABANDON_PROMPT = (
    "Write a detailed, structured explanation of how a distributed transaction "
    "log achieves atomicity. Cover write-ahead logging, two-phase commit, "
    "recovery after a crash, and the failure modes of each. Be thorough."
)
ABANDON_READ_TIMEOUT_S = 2.0


def _client(service: str, region: str, *, read_timeout: Optional[float] = None,
            single_attempt: bool = True):
    import boto3
    from botocore.config import Config

    kwargs: dict[str, Any] = {}
    if read_timeout is not None:
        kwargs["read_timeout"] = read_timeout
    if single_attempt:
        # NOT `max_attempts`: botocore rewrites that to N + 1 (finding 4). Getting
        # this wrong would make the abandoned call two charges and the measurement
        # unreadable.
        kwargs["retries"] = {"total_max_attempts": 1}
    return boto3.client(service, region_name=region, config=Config(**kwargs))


def retry_arithmetic() -> dict[str, Any]:
    """Finding 4, offline: what botocore does with the retry knob it is given."""
    import botocore
    import boto3
    from botocore.config import Config

    def resolved(retries: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        """Returns (what the client resolved, what the caller's own dict became).

        The second value is not a curiosity: botocore rewrites the dict it was
        handed IN PLACE, so a caller cannot even read back what it asked for.
        """
        caller_dict = dict(retries)
        client = boto3.client(
            "bedrock-runtime", region_name="us-east-1", config=Config(retries=caller_dict)
        )
        return dict(client.meta.config.retries or {}), caller_dict

    asked = {"max_attempts": 1}
    got, asked_after = resolved(asked)
    explicit, _ = resolved({"total_max_attempts": 1})
    return {
        "botocore_version": botocore.__version__,
        "asked": dict(asked),
        "asked_dict_after_the_call": asked_after,
        "resolved": got,
        "asked_total": {"total_max_attempts": 1},
        "resolved_total": explicit,
        "one_reservation_can_pay_for": got.get("total_max_attempts"),
        "holds": got.get("total_max_attempts") == 2 and explicit.get("total_max_attempts") == 1,
    }


def _minute_floor(when: datetime) -> datetime:
    return when.replace(second=0, microsecond=0)


def _wait_for_fresh_minute() -> None:
    """Start each condition just after a minute boundary, so its counters cannot
    share a minute with the previous condition's."""
    now = datetime.now(timezone.utc)
    sleep_s = 61 - now.second
    if sleep_s > 0:
        print(f"  … waiting {sleep_s}s for a clean counter minute", flush=True)
        time.sleep(sleep_s)


def read_counters(cw, *, model_id: str, minute: datetime) -> dict[str, Any]:
    """The provider's own numbers for one minute, or `no_data`.

    `ModelId` is the dimension Bedrock publishes; for an inference-profile id the
    published value can be the underlying model, so a miss reports the dimension
    values that DO exist rather than a zero.
    """
    start = _minute_floor(minute)
    end = start + timedelta(minutes=1)
    out: dict[str, Any] = {"minute_utc": start.isoformat(), "model_dimension": model_id}
    for metric in METRICS:
        resp = cw.get_metric_statistics(
            Namespace="AWS/Bedrock",
            MetricName=metric,
            Dimensions=[{"Name": "ModelId", "Value": model_id}],
            StartTime=start,
            EndTime=end,
            Period=60,
            Statistics=["Sum"],
        )
        points = resp.get("Datapoints") or []
        out[metric] = int(points[0]["Sum"]) if points else "no_data"
    if all(out[m] == "no_data" for m in METRICS):
        out["published_model_dimensions"] = _known_model_dimensions(cw)
    return out


def _known_model_dimensions(cw) -> list[str]:
    try:
        paginator = cw.get_paginator("list_metrics")
        seen: set[str] = set()
        for page in paginator.paginate(Namespace="AWS/Bedrock", MetricName="Invocations"):
            for metric in page.get("Metrics", []):
                for dim in metric.get("Dimensions", []):
                    if dim.get("Name") == "ModelId":
                        seen.add(dim["Value"])
        return sorted(seen)
    except Exception as exc:  # noqa: BLE001 — diagnostics must never mask the run
        return [f"<list_metrics failed: {exc}>"]


# --------------------------------------------------------------------- conditions


def condition_not_submitted(region: str, model_id: str) -> dict[str, Any]:
    """Our own serialiser refuses the request: it never reaches the service."""
    from botocore.exceptions import ParamValidationError

    client = _client("bedrock-runtime", region)
    try:
        client.converse(modelId=model_id, messages="not a list")  # type: ignore[arg-type]
    except ParamValidationError as exc:
        return {"outcome": "ParamValidationError", "detail": str(exc)[:200]}
    except Exception as exc:  # noqa: BLE001
        return {"outcome": type(exc).__name__, "detail": str(exc)[:200]}
    return {"outcome": "no_exception", "detail": "the SDK accepted a malformed call"}


def condition_rejected(region: str, model_id: str) -> dict[str, Any]:
    """The service rejects the request before a model can run."""
    from botocore.exceptions import ClientError

    client = _client("bedrock-runtime", region)
    try:
        client.converse(
            modelId=model_id,
            messages=[{"role": "user", "content": [{"text": "hi"}]}],
            # Over every model's ceiling: rejected by the service, not by botocore.
            inferenceConfig={"maxTokens": 10_000_000},
        )
    except ClientError as exc:
        return {
            "outcome": "ClientError",
            "code": exc.response.get("Error", {}).get("Code"),
            "status": exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode"),
        }
    except Exception as exc:  # noqa: BLE001
        return {"outcome": type(exc).__name__, "detail": str(exc)[:200]}
    return {"outcome": "no_exception", "detail": "the service accepted an over-limit maxTokens"}


def condition_abandoned(region: str, model_id: str, marker: str) -> dict[str, Any]:
    """The measured expensive case: the client stops waiting mid-generation.

    `requestMetadata` is stamped so the charge can be found in the invocation log
    afterwards (finding 5) — the caller never learns the provider's request id.
    """
    from botocore.exceptions import ReadTimeoutError

    client = _client("bedrock-runtime", region, read_timeout=ABANDON_READ_TIMEOUT_S)
    started = time.time()
    try:
        client.converse(
            modelId=model_id,
            messages=[{"role": "user", "content": [{"text": ABANDON_PROMPT}]}],
            inferenceConfig={"maxTokens": 4096},
            requestMetadata={"sc_probe": marker},
        )
    except ReadTimeoutError:
        return {
            "outcome": "ReadTimeoutError",
            "waited_s": round(time.time() - started, 2),
            "caller_received": None,
            "invocation_log_marker": marker,
        }
    except Exception as exc:  # noqa: BLE001
        return {"outcome": type(exc).__name__, "detail": str(exc)[:200]}
    return {
        "outcome": "completed_before_the_deadline",
        "detail": (
            "the model answered inside the read timeout, so this run measured "
            "nothing: lengthen ABANDON_PROMPT or shorten the timeout"
        ),
    }


def condition_stream_closed_by_consumer(region: str, model_id: str) -> dict[str, Any]:
    """The consumer stops reading a stream after two events."""
    client = _client("bedrock-runtime", region)
    resp = client.converse_stream(
        modelId=model_id,
        messages=[{"role": "user", "content": [{"text": ABANDON_PROMPT}]}],
        inferenceConfig={"maxTokens": 4096},
    )
    stream = resp.get("stream")
    read = 0
    for _event in stream:
        read += 1
        if read >= 2:
            break
    close = getattr(stream, "close", None)
    if callable(close):
        close()
    return {"outcome": "closed_by_consumer", "events_read": read}


def condition_completed(region: str, model_id: str) -> dict[str, Any]:
    """The control: a call that finishes, so the counters can be checked against a
    usage block the provider itself reported."""
    client = _client("bedrock-runtime", region)
    resp = client.converse(
        modelId=model_id,
        messages=[{"role": "user", "content": [{"text": "Reply with the single word: ok"}]}],
        inferenceConfig={"maxTokens": 16},
    )
    usage = resp.get("usage", {})
    return {
        "outcome": "completed",
        "reported_input_tokens": int(usage.get("inputTokens", 0)),
        "reported_output_tokens": int(usage.get("outputTokens", 0)),
    }


CONDITIONS = (
    ("not_submitted", condition_not_submitted),
    ("rejected_pre_inference", condition_rejected),
    ("abandoned_on_read_timeout", condition_abandoned),
    ("stream_closed_by_consumer", condition_stream_closed_by_consumer),
    ("completed", condition_completed),
)


def run(region: str, model_id: str, *, settle_wait_s: int) -> dict[str, Any]:
    run_id = uuid.uuid4().hex[:12]
    results: dict[str, Any] = {
        "source": "real",
        "provider": "aws-bedrock",
        "api": "converse+converse_stream",
        "pricing": "on-demand-token",
        "region": region,
        "model_id": model_id,
        "run_id": run_id,
        "started_utc": datetime.now(timezone.utc).isoformat(),
        "retry_arithmetic": retry_arithmetic(),
        "conditions": {},
        "caveats": [
            "one trial per condition",
            "counters are per-minute sums: any other traffic on this model in the "
            "same minute is included",
            "a condition with no datapoints is reported as no_data, never as zero",
        ],
    }

    for name, fn in CONDITIONS:
        print(f"[{name}] running", flush=True)
        _wait_for_fresh_minute()
        minute = datetime.now(timezone.utc)
        if name == "abandoned_on_read_timeout":
            observed = fn(region, model_id, f"{run_id}-{name}")  # type: ignore[call-arg]
        else:
            observed = fn(region, model_id)  # type: ignore[call-arg]
        results["conditions"][name] = {"minute_utc": _minute_floor(minute).isoformat(),
                                      "observed": observed}
        print(f"[{name}] {observed}", flush=True)

    print(f"waiting {settle_wait_s}s for the counters to publish", flush=True)
    time.sleep(settle_wait_s)

    cw = _client("cloudwatch", region)
    for name, entry in results["conditions"].items():
        minute = datetime.fromisoformat(entry["minute_utc"])
        entry["counters"] = read_counters(cw, model_id=model_id, minute=minute)
        print(f"[{name}] counters {entry['counters']}", flush=True)

    results["finished_utc"] = datetime.now(timezone.utc).isoformat()
    results["verdict"] = _verdict(results)
    return results


def _verdict(results: dict[str, Any]) -> dict[str, Any]:
    """What the run establishes, stated only where it has data to state it."""
    conds = results["conditions"]

    def tokens(name: str) -> Any:
        counters = conds.get(name, {}).get("counters", {})
        values = [counters.get("InputTokenCount"), counters.get("OutputTokenCount")]
        if all(v == "no_data" for v in values):
            return "no_data"
        return sum(v for v in values if isinstance(v, int))

    abandoned = tokens("abandoned_on_read_timeout")
    rejected = tokens("rejected_pre_inference")
    verdict = {
        "an_abandoned_call_was_billed": (
            "INCONCLUSIVE" if abandoned == "no_data" else abandoned > 0
        ),
        "abandoned_token_count": abandoned,
        "a_rejection_was_billed": (
            "INCONCLUSIVE" if rejected == "no_data" else rejected > 0
        ),
        "retry_arithmetic_holds": results["retry_arithmetic"]["holds"],
    }
    if "no_data" in (abandoned, rejected):
        verdict["note"] = (
            "a condition came back with no datapoints. Either the counters had not "
            "published yet (raise --settle-wait) or the ModelId dimension differs "
            "from the id that was invoked (see published_model_dimensions)."
        )
    return verdict


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument(
        "--model",
        default="us.anthropic.claude-haiku-4-5-20251001-v1:0",
        help="use a model this account otherwise does not invoke",
    )
    parser.add_argument("--out", help="write the raw run JSON here")
    parser.add_argument(
        "--settle-wait", type=int, default=180,
        help="seconds to wait for CloudWatch to publish the counters",
    )
    parser.add_argument(
        "--retry-arithmetic-only", action="store_true",
        help="only the offline finding: no account, no money, no Bedrock call",
    )
    args = parser.parse_args(argv)

    if args.retry_arithmetic_only:
        payload = retry_arithmetic()
        print(json.dumps(payload, indent=2))
        return 0 if payload["holds"] else 1

    print(
        "This spends real money and abandons one call mid-generation, which is the "
        "charge being measured.",
        flush=True,
    )
    results = run(args.region, args.model, settle_wait_s=args.settle_wait)
    text = json.dumps(results, indent=2)
    if args.out:
        with open(args.out, "w") as fh:
            fh.write(text)
        print(f"wrote {args.out}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
