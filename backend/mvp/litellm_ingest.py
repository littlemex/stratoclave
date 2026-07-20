"""LiteLLM config.yaml ingestion — SCAFFOLD ONLY (litellm wedge slice-3).

This is the reserved seam for the drop-in migration on-ramp: a team moving off
LiteLLM feeds Stratoclave its existing `config.yaml` and gets an honest static
report of what would migrate, before sending a single request. The full
implementation is intentionally NOT shipped in this PR — only the entry point is
opened so the shape is agreed and callers can be wired later.

DESIGN (to implement): a PURE translator that whitelist-extracts only
`model_name` + `litellm_params.model` from each `model_list` entry (secrets
dropped at the door — `api_key` reduced to an existence flag, never a value,
never in a log/error), and classifies each entry into EXACTLY ONE of:
  * mapped      — STRICT: registry-resolvable AND has an explicit pricing rate
                  AND no alias conflict. Only these are safe to bill.
  * unsupported — a structured `reason` code (unknown_provider / unknown_model /
                  pricing_missing / malformed / wildcard) — the future
                  provider-adapter slice's requirements backlog.
  * conflict    — alias_conflict, or pool_divergent (a repeated `model_name`
                  resolving to different models). Never silently merged.
  * ignored     — top-level litellm sections this tool does not migrate
                  (router_settings, per-deployment rpm/tpm, ...).
Plus a dry-run-first CLI whose `--apply` re-translates (never trusts a stale
report), blocks the whole apply on ANY conflict, merges into the tenant
allowlist through the admin-routing put path, and guards tenant existence.

PROVENANCE: a complete, reviewed, tested implementation (translator + CLI + 31
tests, 952 lines, Fable-reviewed across 4 rounds) existed at commit 267d6ba on
`feat/marker-separate-item` and was deliberately dropped from this PR so the
"scaffold only" contract stays unambiguous. Recover it from git when slice-3
lands: `git show 267d6ba`.
"""
from __future__ import annotations

from typing import Any


def translate_litellm_config(config: dict[str, Any]):
    """SCAFFOLD: not implemented. See the module docstring for the intended
    four-way classification, and commit 267d6ba for a complete implementation to
    restore when litellm ingestion (slice-3) is scheduled. Raising (rather than
    returning an empty report) makes an accidental early call fail loud instead
    of silently claiming "nothing migrates"."""
    raise NotImplementedError(
        "litellm config.yaml ingestion is scaffold-only; the full translator "
        "ships in a later slice (design in this module's docstring; reference "
        "implementation at commit 267d6ba)."
    )
