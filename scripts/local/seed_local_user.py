#!/usr/bin/env python3
"""Seed one local user + a scoped API key, using the same code paths the
running gateway uses — no new auth bypass, no hand-rolled hashing.

Writes three things via the real repositories:
  1. a Users row (backend/dynamo/users.py: UsersRepository.put_user)
  2. a UserTenants row granting the default tenant's starting quota
     (backend/dynamo/user_tenants.py: UserTenantsRepository.ensure)
  3. an ApiKeys row (backend/dynamo/api_keys.py: ApiKeysRepository.create),
     scoped to messages:send / responses:send / usage:read-self — the same
     scopes `stratoclave api-key create` grants by default.

Idempotent across re-runs. The plaintext key is only ever recoverable once
(the gateway itself never stores it — see api_keys.py), so this script keeps
its own record in `data/local/api_key` (already gitignored via `data/`) and
reuses it on subsequent runs by checking the key's hash is still a live row —
if the local DynamoDB volume was wiped (`make down` removes it) the old
record is orphaned and a fresh key is minted automatically.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any


def _ensure_backend_importable() -> None:
    try:
        import dynamo  # noqa: F401
        return
    except ImportError:
        pass
    here = Path(__file__).resolve()
    for candidate in (here.parents[2] / "backend", Path("/app")):
        if (candidate / "dynamo").is_dir():
            sys.path.insert(0, str(candidate))
            return
    raise SystemExit(
        "Could not locate the backend package ('dynamo/'). Run this inside the "
        "gateway container (`make up` does) or from a full repo checkout."
    )


_ensure_backend_importable()

from _local_guard import require_local_dynamodb  # noqa: E402
from dynamo.api_keys import ApiKeysRepository, hash_key  # noqa: E402
from dynamo.user_tenants import UserTenantsRepository  # noqa: E402
from dynamo.users import UsersRepository  # noqa: E402

LOCAL_USER_ID = "local-dev-user"
LOCAL_EMAIL = "local-dev@stratoclave.local"
LOCAL_KEY_NAME = "local-dev"
LOCAL_SCOPES = ["messages:send", "responses:send", "usage:read-self"]
KEY_FILE = Path(__file__).resolve().parents[2] / "data" / "local" / "api_key"


def _existing_live_key() -> str | None:
    """The saved plaintext key, if its hash is still an active row. None if
    there is no saved key, or the row it pointed to is gone/revoked."""
    if not KEY_FILE.exists():
        return None
    plain = KEY_FILE.read_text().strip()
    if not plain:
        return None
    item = ApiKeysRepository().get_by_hash(hash_key(plain))
    if item and not item.get("revoked_at"):
        return plain
    return None


def main() -> None:
    require_local_dynamodb("seed_local_user")
    org_id = os.environ.get("DEFAULT_ORG_ID", "default-org")

    UsersRepository().put_user(
        user_id=LOCAL_USER_ID,
        email=LOCAL_EMAIL,
        auth_provider="local",
        auth_provider_user_id=LOCAL_USER_ID,
        org_id=org_id,
        roles=["user"],
    )
    print(f"[seed_local_user] user ready: {LOCAL_USER_ID} (org={org_id}, role=user)")

    membership: dict[str, Any] = UserTenantsRepository().ensure(
        user_id=LOCAL_USER_ID, tenant_id=org_id, role="user"
    )
    print(
        f"[seed_local_user] tenant membership ready: total_credit="
        f"{membership.get('total_credit')} credit_used={membership.get('credit_used')} "
        f"source={membership.get('credit_source')}"
    )

    reused = _existing_live_key()
    if reused:
        print(f"[seed_local_user] reusing existing key from {KEY_FILE}")
        print(f"[seed_local_user] API key: {reused}")
        return

    item, plain = ApiKeysRepository().create(
        user_id=LOCAL_USER_ID,
        name=LOCAL_KEY_NAME,
        scopes=LOCAL_SCOPES,
        expires_at=None,
        created_by=LOCAL_USER_ID,
    )
    KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    KEY_FILE.write_text(plain + "\n")
    KEY_FILE.chmod(0o600)
    print(f"[seed_local_user] created key {item['key_id']} (scopes: {', '.join(LOCAL_SCOPES)})")
    print(f"[seed_local_user] saved to {KEY_FILE} (mode 600)")
    print(f"[seed_local_user] API key: {plain}")


if __name__ == "__main__":
    main()
