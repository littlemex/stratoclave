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

- `promotion` — the promotion candidate store: its own table, its access
  constraint enforced on read, and the write-time half of its identifier
  check; deriving the fields a discovered record and a verified probe
  observation already answer and requiring the three only a human can;
  reporting which identifiers a promotion is about to make reachable; and
  re-validating the composed namespace -- the code-resident registry plus
  every stored candidate -- at every process start. Activation is not here.
"""
from __future__ import annotations
