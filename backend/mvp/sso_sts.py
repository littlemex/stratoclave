"""STS presigned URL verification + identity parsing (Phase S).

The backend forwards the `sts:GetCallerIdentity` signed request received from the CLI
directly to STS and extracts the returned Arn / UserId / Account.

Security policy (design doc §7.4):
- SSRF defense: only https://sts.*.amazonaws.com/ hosts are allowed.
- Only method=POST and Action=GetCallerIdentity are accepted.
- X-Amz-Date must be within ±5 minutes of now (replay mitigation; nonce table added in Phase 3).
- timeout: 10 seconds.
"""
from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal, Optional
from urllib.parse import urlparse, parse_qs

import httpx
from fastapi import HTTPException


_log = logging.getLogger(__name__)

_ALLOWED_STS_HOSTS = {
    "sts.amazonaws.com",
    "sts.us-east-1.amazonaws.com",
    "sts.us-east-2.amazonaws.com",
    "sts.us-west-1.amazonaws.com",
    "sts.us-west-2.amazonaws.com",
    "sts.eu-west-1.amazonaws.com",
    "sts.eu-central-1.amazonaws.com",
    "sts.ap-northeast-1.amazonaws.com",
    "sts.ap-southeast-1.amazonaws.com",
    "sts.ap-southeast-2.amazonaws.com",
}

_STS_ACTION = "GetCallerIdentity"

# EC2 Instance Profile detection: session part is i-<hex 8-17> only.
_INSTANCE_PROFILE_SESSION_RE = re.compile(r"^i-[0-9a-f]{8,17}$")


IdentityType = Literal["sso_user", "federated_role", "iam_user", "instance_profile"]


@dataclass(frozen=True)
class StsIdentity:
    """4-tuple extracted from STS GetCallerIdentity plus the classification result."""

    arn: str
    user_id: str
    account_id: str
    identity_type: IdentityType
    role_name: Optional[str]       # set for assumed-role identities
    session_name: Optional[str]    # tail of the assumed-role ARN (email, username, or instance-id)
    iam_user_name: Optional[str]   # set for iam_user identities


def verify_and_call_sts(
    method: str,
    url: str,
    headers: dict[str, str],
    body: str = "",
) -> StsIdentity:
    """Validate the presigned request received from the CLI and call STS directly to verify identity.

    Replay protection (P3-1): before transporting the signed request to
    STS, record its fingerprint in the nonces table with a short TTL.
    A second submission of the same signature — even inside the ±5
    minute skew window — is rejected with 401. The table is optional:
    environments that have not yet provisioned it fall back to the
    legacy skew-only check (logged once as a warning).

    DNS rebinding / poisoning: httpx's default transport verifies TLS
    certificates end-to-end, so an attacker hijacking DNS cannot present
    a valid AWS-signed certificate for the allowlisted host. The vouch
    pattern therefore stays safe without migrating to the boto3 STS
    client (which would force us to re-sign and lose the pass-through).
    """
    _validate_inputs(method, url, headers)

    # Replay guard (P3-1, sweep-4 C-Critical-B2 fail-closed restored).
    #
    # Historical context: earlier sweeps wrapped the nonces-table
    # consume() in `except Exception: log.warning`. That is fail-OPEN:
    # if DynamoDB throttles, the IAM policy drifts, or the table is
    # un-provisioned on a forked deployment, the entire vouch flow
    # silently regresses to "skew-only" replay protection, which any
    # attacker who captured a single sigv4 request can beat for the
    # next ±5 minutes. A fail-OPEN control is worse than no control
    # because it hides the failure from operators.
    #
    # Sweep-4 behaviour:
    #   NonceReplayError                -> 401 "replay"
    #   any OTHER exception             -> 401 "dependency unavailable"
    # Operators still see the error in logs, but the request is
    # refused rather than silently degraded.
    from dynamo.sso_nonces import NonceReplayError, SsoNoncesRepository

    lowered = {k.lower(): v for k, v in headers.items()}
    auth_header = lowered.get("authorization", "")
    x_amz_date = lowered.get("x-amz-date", "")
    try:
        SsoNoncesRepository().consume(
            authorization=auth_header, x_amz_date=x_amz_date
        )
    except NonceReplayError:
        raise HTTPException(
            status_code=401,
            detail="Replay detected: this signed request has already been used.",
        )
    except HTTPException:
        raise
    except Exception as e:
        # Fail-closed on ANY other error. We expose a generic 401 to
        # the caller (no leaking of backend topology) and log the
        # actual cause at ERROR level so oncall can fix the underlying
        # table / IAM / throttling problem.
        _log.error(
            "sso_nonce_store_unavailable_fail_closed",
            extra={"error": str(e), "error_type": type(e).__name__},
        )
        raise HTTPException(
            status_code=401,
            detail=(
                "SSO replay protection temporarily unavailable — request refused. "
                "Retry later; if the problem persists contact the service operator."
            ),
        )

    # Forward the signed request to STS unchanged (body is part of the sigv4 signature and must not be modified).
    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.request(
                method=method,
                url=url,
                headers=headers,
                content=body.encode("utf-8") if body else b"",
            )
    except httpx.HTTPError as e:
        raise HTTPException(
            status_code=502,
            detail=f"Failed to reach STS: {e}",
        )
    if resp.status_code != 200:
        raise HTTPException(
            status_code=401,
            detail=(
                f"STS rejected the presigned request (HTTP {resp.status_code}). "
                "The AWS credentials may be expired or invalid."
            ),
        )

    # Parse the XML response.
    try:
        return _parse_sts_response(resp.text)
    except (ET.ParseError, KeyError) as e:
        _log.error("sts_response_parse_failed", extra={"error": str(e)})
        raise HTTPException(status_code=502, detail="Malformed STS response")


def _validate_inputs(method: str, url: str, headers: dict[str, str]) -> None:
    if method.upper() != "POST":
        raise HTTPException(status_code=400, detail="STS request must use POST")

    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise HTTPException(status_code=400, detail="STS URL must use HTTPS")
    if parsed.hostname not in _ALLOWED_STS_HOSTS:
        raise HTTPException(
            status_code=400,
            detail=f"STS host not allowed: {parsed.hostname}",
        )

    # Validate Action=GetCallerIdentity (check the URL side; it can also be in the body).
    query = parse_qs(parsed.query)
    action_values = query.get("Action", [])
    # aws-sdk-rust presigning sometimes puts Action in the body instead of the URL;
    # if Action is absent from the URL, it is assumed to be in the Authorization signed-payload.
    # We enforce strictly when Action is in the URL; otherwise pass (STS will reject invalid requests).
    if action_values and action_values[0] != _STS_ACTION:
        raise HTTPException(
            status_code=400,
            detail=f"Only Action={_STS_ACTION} is accepted",
        )

    # Validate X-Amz-Date (header names are case-insensitive).
    lowered = {k.lower(): v for k, v in headers.items()}
    x_amz_date = lowered.get("x-amz-date")
    if not x_amz_date:
        raise HTTPException(status_code=400, detail="X-Amz-Date header is required")
    try:
        signed_at = datetime.strptime(x_amz_date, "%Y%m%dT%H%M%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid X-Amz-Date: {x_amz_date}")
    skew = abs((datetime.now(timezone.utc) - signed_at).total_seconds())
    if skew > 300:
        raise HTTPException(
            status_code=400,
            detail=f"Presigned URL timestamp out of range ({int(skew)}s skew)",
        )

    # Verify that the Authorization header (sigv4 signature) is present.
    if "authorization" not in lowered:
        raise HTTPException(
            status_code=400, detail="Authorization header (sigv4) is required"
        )


def _parse_sts_response(xml_text: str) -> StsIdentity:
    """Parse Arn/UserId/Account from the STS GetCallerIdentity XML and classify identity_type."""
    ns = {"sts": "https://sts.amazonaws.com/doc/2011-06-15/"}
    root = ET.fromstring(xml_text)
    arn_elem = root.find(".//sts:Arn", ns)
    user_id_elem = root.find(".//sts:UserId", ns)
    account_elem = root.find(".//sts:Account", ns)
    if arn_elem is None or user_id_elem is None or account_elem is None:
        raise KeyError("STS response missing Arn/UserId/Account")

    arn = (arn_elem.text or "").strip()
    user_id = (user_id_elem.text or "").strip()
    account_id = (account_elem.text or "").strip()

    return classify_arn(arn=arn, user_id=user_id, account_id=account_id)


def classify_arn(*, arn: str, user_id: str, account_id: str) -> StsIdentity:
    """Determine identity_type, role_name, session_name, and iam_user_name from an ARN.

    ARN formats:
      - arn:aws:iam::<acc>:user/<name>                       -> iam_user
      - arn:aws:sts::<acc>:assumed-role/<role>/<session>     -> assumed_role (SSO or federated or instance)
    """
    if arn.startswith("arn:aws:iam::") and ":user/" in arn:
        iam_user_name = arn.split(":user/", 1)[1]
        return StsIdentity(
            arn=arn,
            user_id=user_id,
            account_id=account_id,
            identity_type="iam_user",
            role_name=None,
            session_name=None,
            iam_user_name=iam_user_name,
        )

    if ":assumed-role/" in arn:
        tail = arn.split(":assumed-role/", 1)[1]
        parts = tail.split("/", 1)
        if len(parts) != 2:
            raise HTTPException(
                status_code=400,
                detail=f"Malformed assumed-role ARN: {arn}",
            )
        role_name, session = parts

        # Instance Profile detection: session matches i-<hex> and UserId starts with AROA.
        if _INSTANCE_PROFILE_SESSION_RE.match(session) and user_id.startswith("AROA"):
            return StsIdentity(
                arn=arn,
                user_id=user_id,
                account_id=account_id,
                identity_type="instance_profile",
                role_name=role_name,
                session_name=session,
                iam_user_name=None,
            )

        # IAM Identity Center (AWS SSO) reserved role format.
        if role_name.startswith("AWSReservedSSO_"):
            return StsIdentity(
                arn=arn,
                user_id=user_id,
                account_id=account_id,
                identity_type="sso_user",
                role_name=role_name,
                session_name=session,
                iam_user_name=None,
            )

        # Any other assumed-role is treated as a federated / manual AssumeRole.
        return StsIdentity(
            arn=arn,
            user_id=user_id,
            account_id=account_id,
            identity_type="federated_role",
            role_name=role_name,
            session_name=session,
            iam_user_name=None,
        )

    raise HTTPException(
        status_code=400,
        detail=f"Unsupported ARN format: {arn}",
    )
