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

import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal, Optional
from urllib.parse import urlparse, parse_qs

import httpx
from fastapi import HTTPException

from core.logging import get_logger

# `get_logger` (structlog's stdlib-wrapping `BoundLogger`), not the bare
# `logging.getLogger` this module used before: a plain stdlib logger's
# `.info(msg, extra={...})` call puts the `extra` dict on the `LogRecord`
# object, but `setup_logging`'s production handler formats records with
# `"%(message)s"`, which never reads it -- so every field the runbook below
# tells an operator to watch (`audience_bound`, `presented_host`, `reason`)
# was silently absent from the actual JSON line shipped to CloudWatch,
# while this exact source still looked correct. `get_logger(...).info(event,
# **fields)` renders through structlog's own JSON processor instead, which
# is the path every other module here uses and the one the runbook's
# Logs Insights queries actually parse.
_log = get_logger(__name__)

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

#: Query-string parameter names (case-insensitive) that mean the request is
#: signed via SigV4's query-string form rather than the `Authorization`
#: header. `_validate_inputs` refuses any of these outright -- see its own
#: comment for why letting one through would make `_check_audience`'s
#: "was this header covered by the signature" claim unverifiable.
_QUERY_SIGV4_PARAMETERS = {
    "x-amz-algorithm",
    "x-amz-credential",
    "x-amz-date",
    "x-amz-signedheaders",
    "x-amz-signature",
    "x-amz-security-token",
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
    # The part of the STS UserId that AWS assigns and a caller cannot choose:
    # `AROA…` for an assumed role, `AIDA…` for an IAM user. The ARN is NOT a
    # substitute — an assumed-role ARN ends in the RoleSessionName, which whoever
    # calls AssumeRole picks, so two different callers of the same role can
    # produce byte-identical ARNs. Anything that binds an account to a principal
    # has to bind to this instead. Defaulted so existing constructions stay valid.
    principal_id: Optional[str] = None


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
    _validate_inputs(method, url, headers, body)
    audience_bound = _check_audience(headers)

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
            error=str(e), error_type=type(e).__name__,
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
        identity = _parse_sts_response(resp.text)
    except (ET.ParseError, KeyError) as e:
        _log.error("sts_response_parse_failed", error=str(e))
        raise HTTPException(status_code=502, detail="Malformed STS response")
    # After STS has verified the caller, so junk cannot inflate it: the share of
    # unbound exchanges is how an operator knows every CLI is upgraded before
    # turning STRATOCLAVE_STS_AUDIENCE_REQUIRED on (docs/DEPLOYMENT.md).
    _log.info("sso_sts_exchange_verified", arn=identity.arn, audience_bound=audience_bound)
    return identity


def _validate_inputs(method: str, url: str, headers: dict[str, str], body: str = "") -> None:
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

    # Refuse a query-string SigV4 (presigned-URL signing). `_check_audience`
    # below trusts the `Authorization` header's `SignedHeaders` list to say
    # whether `AUDIENCE_HEADER` was covered by the signature STS verified --
    # true only when STS verified THAT header. A request can carry a valid
    # query-string signature (which STS also accepts) alongside a fabricated,
    # unsigned `Authorization` header that merely claims the audience header
    # was signed; nothing here would call STS wrong, but `_check_audience`
    # would believe a claim STS never checked. Refusing any of SigV4's query
    # parameters outright removes the ambiguity rather than trying to prefer
    # one signature over the other.
    query_params = {k.lower() for k in parse_qs(parsed.query, keep_blank_values=True)}
    forbidden = query_params & _QUERY_SIGV4_PARAMETERS
    if forbidden:
        raise HTTPException(
            status_code=400,
            detail=(
                "STS request must not sign via the query string "
                f"(found {sorted(forbidden)}); use header-based SigV4"
            ),
        )

    # Validate Action=GetCallerIdentity. The action can be in the query string or,
    # for signers that presign the POST form (aws-sdk-rust does), in the body — so
    # both are checked. Enforcing only the query string, as this did before, left
    # the documented control ("Only Action=GetCallerIdentity is accepted")
    # unimplemented for exactly the signer the comment named: the gateway would
    # forward any STS action the presented credentials allow. It never granted the
    # presenter anything they did not already hold, and a non-identity response
    # fails to parse, but a control that is documented and absent is worse than one
    # that is neither.
    action = _signed_action(parsed.query, body)
    if action is None:
        raise HTTPException(
            status_code=400,
            detail=f"STS request must carry Action={_STS_ACTION}",
        )
    if action != _STS_ACTION:
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



#: The header that binds a signed GetCallerIdentity to THIS gateway: the host of
#: the gateway URL the CLI is configured to send the exchange to -- the same
#: value used for the audience and for the POST target (`cli/src/mvp/sso.rs`'s
#: `login`), so a request signed for one gateway only ever names one host.
#: The CLI puts it in the request before signing, so it is covered by the
#: signature and STS refuses any change to it; the gateway refuses a request
#: naming a host that is not one of its own API URLs.
#:
#: What it protects against, once `STRATOCLAVE_STS_AUDIENCE_REQUIRED` is on: a
#: GetCallerIdentity a user signed for another service using the same pattern
#: (HashiCorp Vault's AWS auth is the well-known one) or for a look-alike gateway
#: being replayed here inside the 5 minute window. Until it is on, a request
#: without the header is still accepted, so CLIs that predate it keep working.
AUDIENCE_HEADER = "x-stratoclave-audience"


def expected_audiences() -> frozenset[str]:
    """This gateway's own hosts: the host of `STRATOCLAVE_API_ENDPOINT`, plus any
    hosts in `STRATOCLAVE_STS_AUDIENCES` (comma-separated URLs, for a deployment
    reached under more than one name), normalised as `core.deployment.host_of`."""
    from core.deployment import api_endpoint, host_of

    urls = [api_endpoint() or ""] + os.getenv("STRATOCLAVE_STS_AUDIENCES", "").split(",")
    return frozenset(h for h in (host_of(u) for u in urls if u.strip()) if h)


def audience_required() -> bool:
    """Whether a signed request with no `AUDIENCE_HEADER` is refused.

    Read with `strict_flag` rather than `flag`: this setting gates a security
    control, so a typo in it (`enabled`, `y`, `required`) must fail startup
    rather than silently read as off and ship the gate open.
    """
    from core.deployment import strict_flag

    return strict_flag("STRATOCLAVE_STS_AUDIENCE_REQUIRED")


def validate_audience_config() -> None:
    """Startup check: every `STRATOCLAVE_STS_AUDIENCES` entry must name a host, and
    with the requirement on, the gateway must have at least one host to compare
    with, or every sign-in would be refused."""
    from core.deployment import host_of

    bad = [u for u in os.getenv("STRATOCLAVE_STS_AUDIENCES", "").split(",")
           if u.strip() and host_of(u) is None]
    if bad:
        raise RuntimeError(f"STRATOCLAVE_STS_AUDIENCES has entries that are not http(s) URLs: {bad}")
    if audience_required() and not expected_audiences():
        raise RuntimeError("STRATOCLAVE_STS_AUDIENCE_REQUIRED is on but no API URL is configured, "
                           "so no signed request could name this gateway")


def _sigv4_parameters(authorization: str) -> dict[str, str]:
    """The SigV4 Authorization parameters, parsed strictly: exactly one each of
    Credential, SignedHeaders and Signature after the algorithm, nothing else, so
    the gateway reads the same SignedHeaders STS verifies."""
    scheme, _, rest = authorization.strip().partition(" ")
    if scheme != "AWS4-HMAC-SHA256":
        raise HTTPException(status_code=400, detail="malformed SigV4 Authorization header: scheme")
    params: dict[str, str] = {}
    for part in rest.split(","):
        key, sep, value = part.strip().partition("=")
        if not sep or key in params or key not in {"Credential", "SignedHeaders", "Signature"}:
            raise HTTPException(status_code=400, detail="malformed SigV4 Authorization header")
        params[key] = value
    if set(params) != {"Credential", "SignedHeaders", "Signature"}:
        raise HTTPException(status_code=400, detail="malformed SigV4 Authorization header")
    return params


def access_key_id_of(headers: dict[str, str]) -> Optional[str]:
    """The access key id a signed `GetCallerIdentity` names: the first slash-
    separated component of its `Authorization` header's `Credential=` scope
    string (`<access-key-id>/<date>/<region>/<service>/aws4_request`), parsed
    with the same strict `_sigv4_parameters` this module uses to decide
    whether `AUDIENCE_HEADER` was signed -- so a request this gateway would
    refuse as malformed is not read as naming an access key id here either.
    Used for rate-limiting a sign-in attempt by the credential it presents
    (`core.rate_limit.check_identity_rate_limit`), before the request is
    forwarded to STS. None when the header is missing or malformed; the
    caller falls back to the address-only flood guard in that case.
    """
    lowered = {k.lower(): v for k, v in headers.items()}
    try:
        credential = _sigv4_parameters(lowered.get("authorization", ""))["Credential"]
    except HTTPException:
        return None
    access_key_id = credential.split("/", 1)[0]
    return access_key_id or None


def _check_audience(headers: dict[str, str]) -> bool:
    """Refuse a signed request bound to a host that is not this gateway's; return
    whether the request was bound at all.

    Every refusal is logged as a WARNING (`sso_sts_audience_refused`) naming the
    host the request presented, if any, and the reason, before the exception
    reaches the caller -- otherwise a host an operator forgot to list in
    `STRATOCLAVE_STS_AUDIENCES` (see docs/DEPLOYMENT.md's runbook) produces a
    401 the caller sees and nothing the operator can find in logs.
    """
    lowered = {k.lower(): v for k, v in headers.items()}
    try:
        return _check_audience_or_raise(lowered, headers)
    except HTTPException as exc:
        _log.warning(
            "sso_sts_audience_refused",
            presented_host=lowered.get(AUDIENCE_HEADER, ""),
            reason=exc.detail,
        )
        raise


def _check_audience_or_raise(lowered: dict[str, str], headers: dict[str, str]) -> bool:
    if len(lowered) != len(headers):
        raise HTTPException(status_code=400, detail="header names repeat with different case")
    signed = set(_sigv4_parameters(lowered.get("authorization", ""))["SignedHeaders"].split(";"))
    present = AUDIENCE_HEADER in lowered
    if AUDIENCE_HEADER in signed and not present:
        raise HTTPException(status_code=401, detail=f"{AUDIENCE_HEADER} is signed but missing")
    if not present:
        if audience_required():
            raise HTTPException(status_code=401, detail=(
                f"the signed request must carry {AUDIENCE_HEADER}; update the CLI"))
        return False
    if AUDIENCE_HEADER not in signed:
        raise HTTPException(status_code=401, detail=f"{AUDIENCE_HEADER} must be a signed header")
    expected = expected_audiences()
    if not expected:
        raise HTTPException(status_code=401, detail="this gateway has no API URL configured to compare with")
    if lowered[AUDIENCE_HEADER].strip().lower() not in expected:
        raise HTTPException(status_code=401, detail=f"the signed request names another host in {AUDIENCE_HEADER}")
    return True


def _signed_action(query: str, body: str) -> Optional[str]:
    """The `Action` the signed request carries, from the query string or the form body.

    Returns None when neither carries one, which is itself a refusal: a request
    with no action is not a `GetCallerIdentity` vouch.
    """
    for source in (query, body):
        if not source:
            continue
        values = parse_qs(source).get("Action", [])
        if values:
            return values[0]
    return None


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


def stable_principal_id(user_id: str) -> str:
    """The AWS-assigned prefix of an STS UserId, with any session part removed.

    `AROAEXAMPLE:alice@example.com` -> `AROAEXAMPLE`. The session part is the
    RoleSessionName, which the caller of AssumeRole chooses; the prefix is the
    role's (or user's) unique id, which it does not.
    """
    return (user_id or "").split(":", 1)[0]


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
            principal_id=stable_principal_id(user_id),
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
                principal_id=stable_principal_id(user_id),
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
                principal_id=stable_principal_id(user_id),
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
            principal_id=stable_principal_id(user_id),
        )

    raise HTTPException(
        status_code=400,
        detail=f"Unsupported ARN format: {arn}",
    )
