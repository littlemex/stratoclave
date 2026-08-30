#!/usr/bin/env python3
"""Copy idempotency records out of the money partitions into the permanent one.

WHY THIS EXISTS

An external-authorize idempotency record maps a client key to the authorization it
minted, and the guarantee is that the mapping holds for all time. It used to be
stored inside the money partition (`TENANT#<t>#P#<period>`), which made the KEY's
identity expire with the billing period: a retry two periods later found nothing,
minted a second hold, and one key could produce two charges. Records are written to
`TENANT#<t>#IDEMP` now, and the reader still looks in the old locations — but only
for the period it was handed and the one before it, because it cannot know which
period a much older record was written in.

So a retry of a PRE-UPGRADE key, more than one period after it was first used, can
still miss. This script closes that window by copying the old records to the
permanent partition, where the reader looks first and without needing to guess a
period. Run it once after deploying, per table.

WHAT IT DOES

Scans the ledger table for items whose sort key is an idempotency record and whose
partition is a money partition, and writes each one to `TENANT#<t>#IDEMP` with the
same sort key and attributes. The copy is conditional on the target not existing, so:

  - re-running is safe (an existing target is left alone, never overwritten),
  - a record written by the new code always wins over an older copy of the same key,
    because the new one is already there and the copy is skipped.

The source rows are left in place. The ledger is append-only, and a copy that also
deleted its source would be the one operation the table's IAM policy forbids.

USAGE

    python3 scripts/local/backfill_idemp_partition.py --dry-run
    python3 scripts/local/backfill_idemp_partition.py

    --table   ledger table name (default: $DYNAMODB_CREDIT_LEDGER_TABLE or
              stratoclave-credit-ledger)
    --region  AWS region (default: $AWS_REGION)
"""
from __future__ import annotations

import argparse
import os
import sys

import boto3
from botocore.exceptions import ClientError


IDEMP_SK_PREFIX = "EV#IDEMP#"


def target_pk(source_pk: str) -> str | None:
    """`TENANT#<t>#P#<period>` → `TENANT#<t>#IDEMP`, or None if not a money pk."""
    if not source_pk.startswith("TENANT#") or "#P#" not in source_pk:
        return None
    tenant = source_pk[len("TENANT#"):].split("#P#", 1)[0]
    if not tenant:
        return None
    return f"TENANT#{tenant}#IDEMP"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--table", default=os.getenv(
        "DYNAMODB_CREDIT_LEDGER_TABLE", "stratoclave-credit-ledger"))
    ap.add_argument("--region", default=os.getenv("AWS_REGION"))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    table = boto3.resource("dynamodb", region_name=args.region).Table(args.table)

    scanned = copied = skipped = present = 0
    kwargs: dict = {}
    while True:
        resp = table.scan(**kwargs)
        for item in resp.get("Items", []):
            scanned += 1
            sk = str(item.get("sk", ""))
            if not sk.startswith(IDEMP_SK_PREFIX):
                continue
            new_pk = target_pk(str(item.get("pk", "")))
            if new_pk is None:
                present += 1  # already in the permanent partition
                continue
            if args.dry_run:
                print(f"would copy {item['pk']} {sk} -> {new_pk}")
                copied += 1
                continue
            row = dict(item)
            row["pk"] = new_pk
            try:
                table.put_item(Item=row, ConditionExpression="attribute_not_exists(pk)")
                copied += 1
            except ClientError as e:
                if e.response.get("Error", {}).get("Code") == \
                        "ConditionalCheckFailedException":
                    skipped += 1  # a newer record for this key already exists
                else:
                    raise
        cursor = resp.get("LastEvaluatedKey")
        if not cursor:
            break
        kwargs["ExclusiveStartKey"] = cursor

    print(f"scanned={scanned} copied={copied} "
          f"skipped_existing={skipped} already_permanent={present}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
