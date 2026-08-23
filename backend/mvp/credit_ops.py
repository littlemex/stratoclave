"""Shared request semantics for setting/clearing a user's per-user TOKEN quota
(`UserTenants.total_credit` / `credit_used`).

Both the Admin endpoint (`admin_users.set_credit`) and the Team-Lead endpoint
(`team_lead.set_member_credit`) write the SAME attribute under the SAME rules, so
the validation and the "which cap to write" resolution live here ONCE — a single
source of truth that cannot drift between the two roles.
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field, model_validator

from dynamo.user_tenants import UNLIMITED_CREDIT


class CreditAction(BaseModel):
    """Mixin carrying the three cap-mutation fields + their invariant.

    Exactly one cap source may be given:
      - `total_credit`: set the cap to this token count, OR
      - `unlimited: true`: set the cap to the effectively-unbounded sentinel
        (the tenant dollar pool and per-model quota still apply), OR
      - neither, with `reset_used: true`: keep the current cap, just clear the
        used-counter to 0 (a PARTIAL update — see UserTenantsRepository).
    `reset_used` clears `credit_used` to 0 alongside any of the above.
    """

    total_credit: Optional[int] = Field(default=None, ge=0, le=10_000_000)
    reset_used: bool = False
    unlimited: bool = False

    @model_validator(mode="after")
    def _validate_one_action(self) -> "CreditAction":
        if self.total_credit is not None and self.unlimited:
            raise ValueError("provide either total_credit or unlimited, not both")
        if self.total_credit is None and not self.unlimited and not self.reset_used:
            raise ValueError(
                "no-op: provide total_credit, unlimited=true, or reset_used=true"
            )
        return self

    def resolved_total(self) -> Optional[int]:
        """The cap to write: the sentinel for unlimited, the explicit value, or
        None for reset-only (which must NOT re-write the cap — the repository
        does a partial update on None so a concurrent cap change is not clobbered)."""
        if self.unlimited:
            return UNLIMITED_CREDIT
        return self.total_credit
