"""Per-tenant VSR configuration store — S3-backed OPAQUE blob + save-time
validation proxied to the version-pinned VSR itself.

Design (Fable, reviewed): the VSR config (a YAML whose schema is the VSR's OWN
and changes across VSR versions) is treated by Stratoclave as an OPAQUE blob.
Stratoclave NEVER parses it field-by-field — loose coupling. It only:

  1. stores it as bytes in S3 (`vsr-config/<tenant_id>.yaml`, `vsr-config/default.yaml`),
  2. at SAVE time, proxies the blob to the pinned VSR's ``/v1/config/validate``
     so garbage can never be stored (schema knowledge stays in the VSR), and
  3. exposes admin routes to GET/PUT/DELETE the raw text.

The VSR reads the right tenant's blob lazily per consult (single shared task,
no per-tenant tasks) and falls back to last-known-good / default on a broken
blob — so a broken save degrades ONLY that tenant to normal Bedrock routing
(the consult is already fail-open). That load model + LKG is the VSR's
responsibility; the CONTRACT is documented in ``docs/VSR_CONFIG_CONTRACT.md``.

BLAST RADIUS: nothing here is on the money/hot path. The consult path is
unchanged (still just ``tenant_id``). This module is reached only from the
admin router, only when ``EXTERNAL_VSR_ENABLED=true``.
"""
from __future__ import annotations

import os
import re
import threading
from typing import Optional

import httpx

from core.logging import get_logger

from .client import _base_url, external_vsr_enabled

logger = get_logger(__name__)


# --------------------------------------------------------------------------
# Bounds + identifiers.
# --------------------------------------------------------------------------

# Hard size cap, enforced at PUT (and again by the VSR at fetch/validate). A
# routing-tuning YAML is a few KB; 256KB is generous headroom while capping an
# alias-bomb / OOM attempt long before it reaches the shared VSR task.
MAX_BLOB_BYTES = 256 * 1024

# The one reserved key: the org-wide fallback config, admin-only (enforced in
# the router, not here). Every other blob is keyed by a tenant id.
DEFAULT_KEY = "default"

# A tenant id that is safe to interpolate into an S3 key: no path traversal, no
# separators. Mirrors the id shape the tenants table issues. Anything else is
# rejected BEFORE it can build a key — an SSRF/path-traversal guard for the
# object store, symmetric with the endpoint allowlist on the serving side.
_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")

# Validate proxy timeout: this is an admin, off-hot-path call (not the 150ms
# consult budget), so give the VSR room to parse a real config. Still bounded
# so a hung VSR fails the SAVE loudly rather than pinning the admin request.
_VALIDATE_TIMEOUT_S = 5.0


class VsrConfigError(Exception):
    """Base for save-path failures mapped to HTTP by the router."""


class ConfigTooLarge(VsrConfigError):
    """Blob exceeds MAX_BLOB_BYTES -> 413."""


class ConfigRejected(VsrConfigError):
    """The pinned VSR's /validate rejected the blob -> 422. Carries the VSR's
    verbatim error text so the admin sees the real reason (schema stays in the
    VSR — Stratoclave just relays)."""

    def __init__(self, errors: object) -> None:
        super().__init__("vsr rejected config")
        self.errors = errors


class ValidatorUnavailable(VsrConfigError):
    """The VSR /validate endpoint was unreachable / not VERIFIED at save time
    -> 503. We FAIL THE SAVE LOUDLY rather than store an unvalidated blob (the
    LKG fallback is a safety net, never the primary defense)."""


# --------------------------------------------------------------------------
# S3 client + key derivation.
# --------------------------------------------------------------------------

_s3_client = None
_s3_lock = threading.Lock()


def _bucket() -> str:
    return (os.getenv("VSR_CONFIG_BUCKET", "") or "").strip()


def _get_s3():
    global _s3_client
    if _s3_client is not None:
        return _s3_client
    with _s3_lock:
        if _s3_client is None:
            import boto3
            from botocore.config import Config

            region = os.getenv("AWS_REGION", "us-east-1")
            _s3_client = boto3.client(
                "s3",
                region_name=region,
                config=Config(retries={"max_attempts": 2, "mode": "standard"}),
            )
    return _s3_client


def reset_for_test() -> None:
    """Drop the cached S3 client + validate HTTP client (test hook)."""
    global _s3_client, _validate_client
    with _s3_lock:
        _s3_client = None
    with _validate_lock:
        if _validate_client is not None:
            try:
                _validate_client.close()
            except Exception:  # noqa: BLE001
                pass
        _validate_client = None


def _normalize_id(tenant_id: str) -> str:
    """Validate + return the id used in the S3 key. Rejects anything that is not
    a safe id (path traversal, separators) so a caller can never escape the
    ``vsr-config/`` prefix. ``default`` is a legal id (the fallback blob)."""
    tid = (tenant_id or "").strip()
    if not _SAFE_ID.match(tid):
        raise VsrConfigError(f"invalid tenant id for vsr config: {tenant_id!r}")
    return tid


def _key(tenant_id: str) -> str:
    return f"vsr-config/{_normalize_id(tenant_id)}.yaml"


# --------------------------------------------------------------------------
# Save-time validation proxy (schema-agnostic: the VSR owns the schema).
# --------------------------------------------------------------------------

_validate_client: Optional[httpx.Client] = None
_validate_lock = threading.Lock()


def _get_validate_client() -> Optional[httpx.Client]:
    global _validate_client
    base = _base_url()
    if not base:
        return None
    with _validate_lock:
        if _validate_client is None:
            _validate_client = httpx.Client(
                base_url=base,
                timeout=httpx.Timeout(
                    connect=2.0, read=_VALIDATE_TIMEOUT_S,
                    write=2.0, pool=2.0,
                ),
            )
        return _validate_client


def validate_blob(blob: str) -> None:
    """POST the raw blob to the pinned VSR's ``/v1/config/validate``. Returns
    normally iff the VSR answers 200/valid. Raises:

      * ConfigTooLarge   — over the size cap (checked here too, before any I/O);
      * ValidatorUnavailable — VSR unreachable / non-JSON / 5xx (fail the save);
      * ConfigRejected   — VSR answered 422/{valid:false} (relay its errors).

    Stratoclave stays schema-agnostic: it only interprets the valid/invalid
    verdict, never the config fields."""
    if len(blob.encode("utf-8")) > MAX_BLOB_BYTES:
        raise ConfigTooLarge(f"config exceeds {MAX_BLOB_BYTES} bytes")
    client = _get_validate_client()
    if client is None:
        raise ValidatorUnavailable("VSR base url not configured")
    try:
        resp = client.post(
            "/v1/config/validate",
            content=blob.encode("utf-8"),
            headers={"Content-Type": "application/yaml"},
        )
    except Exception as e:  # noqa: BLE001 — unreachable => fail the save loudly.
        logger.warning("vsr_validate_unreachable", error=str(e))
        raise ValidatorUnavailable(f"vsr validate unreachable: {e}") from e
    if resp.status_code == 200:
        try:
            body = resp.json()
        except Exception:  # noqa: BLE001 — a 200 with a non-JSON body is a broken validator.
            raise ValidatorUnavailable("vsr validate returned non-JSON 200")
        if isinstance(body, dict) and body.get("valid") is True:
            return
        # 200 but not valid:true -> treat as a rejection with whatever it said.
        raise ConfigRejected(body.get("errors") if isinstance(body, dict) else body)
    if resp.status_code in (400, 413, 422):
        try:
            body = resp.json()
        except Exception:  # noqa: BLE001
            body = {"errors": [f"vsr {resp.status_code}"]}
        raise ConfigRejected(body.get("errors") if isinstance(body, dict) else body)
    # 5xx / anything else: the validator itself is unhealthy -> fail the save.
    logger.warning("vsr_validate_bad_status", status=resp.status_code)
    raise ValidatorUnavailable(f"vsr validate status {resp.status_code}")


# --------------------------------------------------------------------------
# Store operations (opaque bytes; no YAML parsing anywhere here).
# --------------------------------------------------------------------------

def get_config(tenant_id: str) -> Optional[tuple[str, Optional[str]]]:
    """Return ``(text, version_id)`` for the tenant's blob, or None if absent
    (the tenant then inherits ``default`` at the VSR). Raises VsrConfigError on
    a bad id or a missing bucket."""
    bucket = _bucket()
    if not bucket:
        raise VsrConfigError("VSR_CONFIG_BUCKET not configured")
    key = _key(tenant_id)
    s3 = _get_s3()
    from botocore.exceptions import ClientError

    try:
        resp = s3.get_object(Bucket=bucket, Key=key)
    except ClientError as e:
        # ONLY a genuine "object absent" is "not configured yet" => None. Every
        # OTHER S3 ClientError (AccessDenied, SlowDown/throttling, 5xx, KMS key
        # errors, ...) surfaces as a generic ClientError whose CLASS NAME is also
        # "ClientError", so matching on the class name would swallow real
        # infrastructure faults as an empty tenant config (masking a broken
        # deploy behind a "no config" UI state). Key off the error CODE instead.
        code = (e.response or {}).get("Error", {}).get("Code", "")
        status = (e.response or {}).get("ResponseMetadata", {}).get("HTTPStatusCode")
        if code in ("NoSuchKey", "NoSuchBucket", "404") or status == 404:
            return None
        raise VsrConfigError(f"s3 get failed: {code or e}") from e
    except Exception as e:  # noqa: BLE001 — non-ClientError transport failure.
        raise VsrConfigError(f"s3 get failed: {e}") from e
    body = resp["Body"].read()
    return body.decode("utf-8", errors="replace"), resp.get("VersionId")


def put_config(tenant_id: str, blob: str) -> Optional[str]:
    """Validate the blob via the VSR, then write it to S3. Returns the S3
    VersionId (versioning is ON). Never writes an unvalidated blob. Raises
    ConfigTooLarge / ConfigRejected / ValidatorUnavailable / VsrConfigError."""
    bucket = _bucket()
    if not bucket:
        raise VsrConfigError("VSR_CONFIG_BUCKET not configured")
    key = _key(tenant_id)  # validates id BEFORE any network/validate call
    validate_blob(blob)    # raises unless the pinned VSR says valid
    s3 = _get_s3()
    try:
        resp = s3.put_object(
            Bucket=bucket, Key=key,
            Body=blob.encode("utf-8"),
            ContentType="application/yaml",
        )
    except Exception as e:  # noqa: BLE001
        raise VsrConfigError(f"s3 put failed: {e}") from e
    logger.info("vsr_config_saved", tenant_id=_normalize_id(tenant_id),
                version_id=resp.get("VersionId"), bytes=len(blob.encode("utf-8")))
    return resp.get("VersionId")


def delete_config(tenant_id: str) -> None:
    """Remove a tenant's override so it reverts to ``default`` at the VSR. A
    versioned bucket keeps the deleted content recoverable. Deleting the
    ``default`` blob is allowed (org-wide revert) but is admin-gated in the
    router."""
    bucket = _bucket()
    if not bucket:
        raise VsrConfigError("VSR_CONFIG_BUCKET not configured")
    key = _key(tenant_id)
    s3 = _get_s3()
    try:
        s3.delete_object(Bucket=bucket, Key=key)
    except Exception as e:  # noqa: BLE001
        raise VsrConfigError(f"s3 delete failed: {e}") from e
    logger.info("vsr_config_deleted", tenant_id=_normalize_id(tenant_id))


def config_admin_enabled() -> bool:
    """The admin surface is live only when the external VSR feature is on AND a
    bucket is configured. Off => the router returns 404-equivalent (never a
    half-configured 500)."""
    return external_vsr_enabled() and bool(_bucket())
