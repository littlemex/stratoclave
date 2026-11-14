#!/usr/bin/env python3
"""perigraph conformance adapter for stratoclave's collector (contract: perigraph conformance/README.md).

Run from a perigraph checkout, pointing at this backend:

    python tools/conform.py --role collector \
        --adapter "python /path/to/stratoclave/backend/tests/perigraph_adapter.py"

Declares the collector role only: the gateway writes records and neither stamps a model nor receives records.
"""
from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from mvp import perigraph_record  # noqa: E402


def main() -> None:
    for line in sys.stdin:
        req = json.loads(line)
        op = req.get("op")
        if op == "describe":
            reply = {"name": "stratoclave", "roles": ["collector"]}
        elif op == "collect":
            reply = {"attributes": perigraph_record.record(req["body"])}
        else:
            reply = {"error": f"stratoclave declares the collector role only; {op!r} is not handled"}
        print(json.dumps({"id": req["id"], **reply}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
