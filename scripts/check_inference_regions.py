#!/usr/bin/env python3
"""Compare every registry entry's `inference_regions` with what Bedrock reports.

Run before a release, with read-only AWS credentials for the account the gateway
serves from:

    python3 scripts/check_inference_regions.py
    python3 scripts/check_inference_regions.py --registry path/to/models.json

Exits 1 when an entry disagrees with Bedrock, or Bedrock no longer knows an entry
that records the field, printing the
value Bedrock reports so the entry can be corrected. Only control-plane reads
(`GetInferenceProfile`) are made, each in the entry's own `bedrock_region`, because
a profile's destinations depend on the region it is called from.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "backend"))


def main() -> int:
    import boto3

    from mvp.discovery.reach import ReachUnknown, destination_regions
    from mvp.models import NO_PROFILE_SCOPE, load_registry

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--registry", help="registry document (default: the bundled one)")
    args = ap.parse_args()

    clients: dict[str, object] = {}
    problems = 0
    for entry in load_registry(args.registry):
        if entry.served_by != "bedrock":
            continue
        region = entry.bedrock_region
        client = clients.setdefault(region, boto3.client("bedrock", region_name=region))
        try:
            actual = destination_regions(client, entry.bedrock_model_id,
                                         is_profile=entry.profile_scope != NO_PROFILE_SCOPE,
                                         source_region=region)
        except ReachUnknown as exc:
            if entry.inference_regions is None:
                # Never recorded and Bedrock does not know it (a retired profile):
                # with no boundary it is served as before, with one it is left out.
                print(f"unrecorded {entry.bedrock_model_id} ({region}): {exc}")
            else:
                print(f"UNKNOWN   {entry.bedrock_model_id} ({region}): {exc}")
                problems += 1
            continue
        declared = entry.inference_regions
        if declared != actual:
            print(f"MISMATCH  {entry.bedrock_model_id} ({region}): declared "
                  f"{list(declared) if declared else None}, Bedrock reports "
                  f"{json.dumps(list(actual))}")
            problems += 1
        else:
            print(f"ok        {entry.bedrock_model_id} ({region}): {list(actual)}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
