"""A boto3 session with REAL credentials, for the opt-in live checks.

`tests/conftest.py` plants dummy `AWS_*` credentials in the environment at import so
that no test can reach AWS by accident. Those env vars beat a named profile, and
botocore resolves credentials lazily — at the first API call, not when the client is
built — so removing them only around `Session(...)` is not enough: by the time the
request goes out they are back, and the call fails with
`UnrecognizedClientException`.

So the credentials are frozen while the environment is clean and handed to the new
session explicitly, which makes it independent of the environment afterwards.
"""
from __future__ import annotations

import os
from typing import Optional

_DUMMY_KEYS = ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN")


def real_session(boto3, *, profile: Optional[str] = None):
    """A session bound to real credentials, or `None` when there are none."""
    saved = {key: os.environ.pop(key, None) for key in _DUMMY_KEYS}
    try:
        profile = profile or os.getenv("AWS_PROFILE")
        probe = boto3.Session(profile_name=profile) if profile else boto3.Session()
        credentials = probe.get_credentials()
        if credentials is None:
            return None
        frozen = credentials.get_frozen_credentials()
    finally:
        for key, value in saved.items():
            if value is not None:
                os.environ[key] = value
    return boto3.Session(
        aws_access_key_id=frozen.access_key,
        aws_secret_access_key=frozen.secret_key,
        aws_session_token=frozen.token,
        region_name=probe.region_name,
    )
