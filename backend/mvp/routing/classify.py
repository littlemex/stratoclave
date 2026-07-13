"""Error classification for retry/fallback decisions."""
from __future__ import annotations

from botocore.exceptions import ClientError, ReadTimeoutError, ConnectTimeoutError

from .types import Disposition, Target


def classify(exc: Exception, target: Target) -> Disposition:
    """Classify an exception into a retry disposition.

    Bias toward FAILOVER over backoff — capacity diversity beats time diversity
    for interactive streaming paths.
    """
    if isinstance(exc, ClientError):
        code = exc.response.get("Error", {}).get("Code", "")
        if code == "ThrottlingException":
            return Disposition.FAILOVER
        if code in ("ServiceUnavailableException", "InternalServerException"):
            return Disposition.RETRY_SAME
        if code == "ModelNotReadyException":
            return Disposition.FAILOVER
        if code == "AccessDeniedException" and target.cost_tier == 0:
            return Disposition.FAILOVER
        if code in ("ValidationException", "ResourceNotFoundException"):
            return Disposition.FATAL
        return Disposition.FATAL

    if isinstance(exc, (ReadTimeoutError, ConnectTimeoutError, TimeoutError, OSError)):
        return Disposition.FAILOVER

    return Disposition.FATAL
