"""Per-region Bedrock Runtime client pool."""
from __future__ import annotations

import os
from functools import lru_cache

import boto3


@lru_cache(maxsize=8)
def bedrock_client(region: str):
    """Return a cached bedrock-runtime client for the given region."""
    return boto3.client("bedrock-runtime", region_name=region)


def default_region() -> str:
    return os.getenv("BEDROCK_REGION") or os.getenv("AWS_REGION", "us-east-1")
