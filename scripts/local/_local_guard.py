"""One place that decides whether a local-mode script is allowed to write.

These scripts create 23 tables and seed a user. Pointed at a real account by
accident, that is a mess to undo, so the guard lives here rather than being
restated in each script.

Checking that `AWS_ENDPOINT_URL_DYNAMODB` is *set* is not enough. Nothing in
`dynamo.client` passes `endpoint_url`; it relies on botocore reading that
variable, and a botocore old enough to not support it ignores the variable
silently — the script would print a local endpoint and then create every table
in the caller's real AWS account. So the check is made against the endpoint the
client actually resolved.
"""
from __future__ import annotations

import os
from urllib.parse import urlparse

# Hosts that mean "this is AWS, not a local stand-in".
_AWS_SUFFIXES = (".amazonaws.com", ".amazonaws.com.cn", ".api.aws")

# DynamoDB Local does not authenticate, but every request to it is still signed,
# so botocore refuses to make one without credentials.
#
# It also rejects any access key id containing a non-alphanumeric character —
# measured against DynamoDB Local 2.x on 2026-08-28: `abc123` and
# `AKIAIOSFODNN7EXAMPLE` are accepted, while `ci-local`, `ci_local`, `ci.local`
# and `ci local` all fail. It derives its database file name from the key, so the
# key has to be a usable file name. The refusal is
# `UnrecognizedClientException: The Access Key ID or security token is invalid`,
# which reads exactly like a rejection from AWS and sends you looking in the
# wrong place. These values are AWS's own documentation examples: alphanumeric,
# and useless against AWS.
_SIGNING_ONLY_ID = "AKIAIOSFODNN7EXAMPLE"
_SIGNING_ONLY_SECRET = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"


def _ensure_signable(tag: str) -> None:
    """Give botocore something to sign with if the host has no credentials.

    Must run before the first client is built: botocore resolves credentials at
    client construction, so setting these afterwards would have no effect.

    Safe because the caller has already been pinned to a local endpoint — these
    values can only ever sign a request to a store that ignores them.
    """
    import boto3

    try:
        found = boto3.Session().get_credentials()
    except Exception:  # a broken profile or config file is the same as none here
        found = None
    if found is not None:
        return

    os.environ["AWS_ACCESS_KEY_ID"] = _SIGNING_ONLY_ID
    os.environ["AWS_SECRET_ACCESS_KEY"] = _SIGNING_ONLY_SECRET
    os.environ.setdefault("AWS_REGION", "us-east-1")
    print(
        f"[{tag}] no AWS credentials on this host — signing local requests with "
        "AWS's example key pair. The local store ignores them, and they cannot "
        "reach AWS."
    )


def require_local_dynamodb(tag: str):
    """Return a DynamoDB client, or exit if it is not pointed at a local store.

    `tag` only prefixes the log lines, so each script names itself.
    """
    declared = os.environ.get("AWS_ENDPOINT_URL_DYNAMODB", "")
    if not declared:
        raise SystemExit(
            f"[{tag}] AWS_ENDPOINT_URL_DYNAMODB is not set. Refusing to run: this "
            "script writes tables and would do so against a real AWS account "
            "otherwise. `make up` sets it for you; set it yourself if running "
            "standalone."
        )

    _ensure_signable(tag)

    from dynamo.client import get_dynamodb_resource

    client = get_dynamodb_resource().meta.client
    resolved = client.meta.endpoint_url or ""
    host = (urlparse(resolved).hostname or "").lower()

    if host.endswith(_AWS_SUFFIXES):
        if (urlparse(declared).hostname or "").lower() == host:
            # The caller asked for AWS. Nothing is broken; the answer is no.
            raise SystemExit(
                f"[{tag}] AWS_ENDPOINT_URL_DYNAMODB points at {declared!r}, a real "
                "AWS endpoint. Refusing to run: this script writes 23 tables and a "
                "seed user, and is only meant for a local store."
            )

        import botocore

        raise SystemExit(
            f"[{tag}] AWS_ENDPOINT_URL_DYNAMODB is set to {declared!r}, but the "
            f"client resolved to {resolved!r} — a real AWS endpoint. Refusing to "
            "run. botocore did not honour the variable; support for it landed in "
            f"botocore 1.29 and this process is using {botocore.__version__}. "
            "Install the backend's pinned requirements "
            "(python3 -m pip install -r backend/requirements.txt)."
        )

    print(f"[{tag}] endpoint declared: {declared}")
    print(f"[{tag}] endpoint resolved: {resolved}")
    return client
