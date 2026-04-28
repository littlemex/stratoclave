"""STS presigned URL verification + identity parsing (Phase S).

CLI から受け取った `sts:GetCallerIdentity` 署名済みリクエストを Backend 自身が
STS に転送し、返ってきた Arn / UserId / Account を抽出する.

セキュリティ方針 (design doc §7.4):
- SSRF 防御: https://sts.*.amazonaws.com/ 系のみ許可
- method=POST かつ Action=GetCallerIdentity のみ許可
- X-Amz-Date が ±5 分以内かチェック (replay 緩和、nonce テーブルは Phase 3 で追加)
- timeout 10 秒
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

# EC2 Instance Profile 判定: session 部が i-<hex 8-17> のみ
_INSTANCE_PROFILE_SESSION_RE = re.compile(r"^i-[0-9a-f]{8,17}$")


IdentityType = Literal["sso_user", "federated_role", "iam_user", "instance_profile"]


@dataclass(frozen=True)
class StsIdentity:
    """STS GetCallerIdentity から抽出した 4 つ組 + 分類結果."""

    arn: str
    user_id: str
    account_id: str
    identity_type: IdentityType
    role_name: Optional[str]       # assumed-role のとき
    session_name: Optional[str]    # assumed-role の末尾 (email or username or instance-id)
    iam_user_name: Optional[str]   # iam_user のとき


def verify_and_call_sts(
    method: str,
    url: str,
    headers: dict[str, str],
    body: str = "",
) -> StsIdentity:
    """CLI から受け取った presigned リクエストを検証し、STS を直接呼んで身元確認する.

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

    # Replay guard (P3-1) — must happen before the STS round trip.
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
    except Exception as e:  # pragma: no cover — configuration fallback
        _log.warning(
            "sso_nonce_store_unavailable",
            extra={"error": str(e), "error_type": type(e).__name__},
        )

    # 署名済みのまま STS に転送 (body は sigv4 署名に含まれるため改変不可)
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

    # XML parse
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

    # Action=GetCallerIdentity の検証 (query または body どちらでも、一応 URL 側を確認)
    query = parse_qs(parsed.query)
    action_values = query.get("Action", [])
    # aws-sdk-rust の presigning では body に Action が入るケースがあるため、
    # URL に Action が無ければ Authorization ヘッダで signed-payload として含まれる想定.
    # ここでは URL に Action があれば厳密チェック、無ければ pass (最終的に STS が弾く)
    if action_values and action_values[0] != _STS_ACTION:
        raise HTTPException(
            status_code=400,
            detail=f"Only Action={_STS_ACTION} is accepted",
        )

    # X-Amz-Date 検証 (ヘッダ名は大文字小文字を区別しないことを考慮)
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

    # Authorization ヘッダ存在チェック (sigv4 署名)
    if "authorization" not in lowered:
        raise HTTPException(
            status_code=400, detail="Authorization header (sigv4) is required"
        )


def _parse_sts_response(xml_text: str) -> StsIdentity:
    """STS GetCallerIdentity XML から Arn/UserId/Account を抽出し identity_type を分類."""
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
    """Arn から identity_type, role_name, session_name, iam_user_name を決定.

    Arn 形式:
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

        # Instance Profile 判定: session が i-<hex> かつ UserId が AROA* で始まる
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

        # IAM Identity Center (AWS SSO) の reserved role 形式
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

        # それ以外の assumed-role は federated / manual AssumeRole
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
