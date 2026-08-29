"""Per-region Bedrock Runtime client pool.

This module used to build its own client with `boto3.client("bedrock-runtime")`
and no `Config` at all, which meant botocore's defaults on the failover path:
60 s connect, NO read timeout, and standard retry mode's default of three
attempts. So the router — whose own retry loop already re-sends deliberately —
multiplied each of its attempts by up to three invisible SDK attempts, on a
client the timeout regression test never looked at because it only inspects
`mvp._bedrock_clients`.

There is now one factory. Routing gets the same single-attempt, explicitly
timed-out client as every other path, and a change to those settings cannot
apply to one caller and miss the other.
"""
from __future__ import annotations

import os


def bedrock_client(region: str):
    """Return the shared, configured bedrock-runtime client for `region`."""
    from .._bedrock_clients import bedrock_runtime_client

    return bedrock_runtime_client(region)


def default_region() -> str:
    return os.getenv("BEDROCK_REGION") or os.getenv("AWS_REGION", "us-east-1")
