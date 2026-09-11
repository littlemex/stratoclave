"""PR2 of the model-discovery change: discover, and record. Nothing here loads,
grants, or probes a model.

- `records` (E1) — the discovered record and its store: one item per profile,
  living beside the routing config and the entitlement grants in the existing
  `stratoclave-user-tenants` table.
- `gates` (E3) — the three data-derived gates that decide whether a discovered
  profile is blocked, and why.
- `pricing_key` (E5) — a content-addressed key for the selector's per-token
  output, so a stable price has a stable name.
- `reconcile` (E2) — the CLI entry point that walks a live account, runs the
  gates, and writes discovered records. `python -m mvp.discovery.reconcile`.

PR3 adds the probe (`protocol_unverified`) and the machinery that makes a
discovered record loadable and grantable. Nothing in this package reaches
the reserve path, a route, or `permissions.json`.
"""
from __future__ import annotations
