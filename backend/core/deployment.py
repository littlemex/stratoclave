"""Facts about how this process is deployed, read in one place so every module
answers them the same way: whether it runs locally, how a boolean setting is
spelled, and which host the gateway's own API URL names."""
from __future__ import annotations

import ipaddress
import os
from typing import Optional
from urllib.parse import urlsplit

import idna

#: `ENVIRONMENT` values for a developer machine or a test run, where the gateway
#: may infer its own URL from request headers. Anything else is a deployment.
LOCAL_ENVIRONMENTS = frozenset({"development", "local", "test"})

_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"", "0", "false", "no", "off"})
_DEFAULT_PORTS = {"https": 443, "http": 80}


def is_local_environment() -> bool:
    return os.getenv("ENVIRONMENT", "").strip().lower() in LOCAL_ENVIRONMENTS


def flag(name: str) -> bool:
    """A boolean setting: on for 1/true/yes/on (any case), off otherwise -- an
    unrecognised spelling silently reads as off. Use `strict_flag` for a
    setting where that silence would hide a typo rather than a choice."""
    return os.getenv(name, "").strip().lower() in _TRUE


def strict_flag(name: str) -> bool:
    """A boolean setting that refuses to be misread: on for 1/true/yes/on, off
    for the empty string/0/false/no/off (any case), and anything else is a
    startup failure rather than a silent off. `enabled`, `y` and `required` are
    all plausible things an operator could type for "on" that `flag()` would
    read as off; a setting that gates a security control should fail loudly on
    a typo instead of shipping disabled."""
    raw = os.getenv(name, "").strip().lower()
    if raw in _TRUE:
        return True
    if raw in _FALSE:
        return False
    raise RuntimeError(
        f"{name}={raw!r} is not a recognised boolean; use one of "
        f"{sorted(_TRUE)} for on, or one of {sorted(_FALSE - {''})} (or leave it "
        f"unset) for off"
    )


def host_of(url: str) -> Optional[str]:
    """The host a URL names: an IP literal canonicalised with `ipaddress`
    (IPv6 compressed to its shortest form and bracketed), or a hostname
    IDNA-encoded under UTS #46 (`idna.encode(..., uts46=True)`, not the
    stdlib `str.encode("idna")` codec -- that codec is IDNA 2003, which
    disagrees with UTS #46 on real inputs, e.g. German "straße"), with the
    port included only when it is not the scheme's default -- the same
    normalisation `cli/src/mvp/sso.rs::audience_host` gets for free from the
    `url` crate's own WHATWG-URL-standard host parsing (shared test vectors:
    `contracts/host_normalization_vectors.json`). None when the value is not
    a plain absolute http(s) URL: no scheme or host, a scheme other than
    http/https, a malformed bracketed IPv6 literal, a userinfo, query or
    fragment component (a configured endpoint has no business carrying
    credentials or a path's tail, and silently dropping them would publish
    whatever was left over through `/.well-known/stratoclave-config`), or a
    hostname `idna.encode` itself refuses (empty, a label with a leading or
    trailing hyphen, an over-length label, or any codepoint a hostname
    cannot contain at all -- "https://a, https://b"'s `.hostname` of
    `"a, https"` is exactly this last case, since `urlsplit` does not
    validate an unbracketed netloc as a host at all)."""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        # e.g. "https://[::1" -- an unterminated IPv6 literal. `urlsplit` raises
        # here rather than on later attribute access, so the guard has to wrap
        # the call itself, not just `.hostname`/`.port` below.
        return None
    if parts.scheme not in _DEFAULT_PORTS or not parts.hostname:
        return None
    if parts.username is not None or parts.password is not None:
        return None
    if parts.query or parts.fragment:
        return None
    try:
        port = parts.port
    except ValueError:
        return None

    raw_hostname = parts.hostname
    # An IP literal: canonicalise with `ipaddress` BEFORE anything IDNA-shaped
    # ever sees it -- `idna.encode` rejects a raw IPv6 literal outright (":"
    # is not a valid hostname codepoint), and the stdlib `.hostname` property
    # already stripped the brackets a bracketed IPv6 literal was written
    # with, so what is left is exactly the string `ipaddress.ip_address`
    # expects. `ipaddress` also compresses: "0:0:0:0:0:0:0:1" and "::1" must
    # read back as the SAME audience, which neither `urlsplit` nor the stdlib
    # "idna" codec did on their own.
    try:
        ip = ipaddress.ip_address(raw_hostname)
    except ValueError:
        ip = None
    if ip is not None:
        host = f"[{ip}]" if ip.version == 6 else str(ip)
        return host if port in (None, _DEFAULT_PORTS[parts.scheme]) else f"{host}:{port}"

    # A hostname. Strip one trailing root-label dot ("example.com." names the
    # same name as "example.com" for every purpose this function serves)
    # before encoding -- `idna.encode` does not normalise that away on its
    # own (it would keep the dot verbatim in the output).
    hostname = raw_hostname.rstrip(".")
    if not hostname:
        return None
    try:
        host = idna.encode(hostname, uts46=True).decode("ascii")
    except idna.IDNAError:
        return None
    return host if port in (None, _DEFAULT_PORTS[parts.scheme]) else f"{host}:{port}"


def api_endpoint() -> Optional[str]:
    """The gateway's own public API URL (`STRATOCLAVE_API_ENDPOINT`), or None."""
    value = os.getenv("STRATOCLAVE_API_ENDPOINT", "").strip().rstrip("/")
    return value or None


def validate_api_endpoint() -> None:
    """Startup check: a deployment must name its own API URL, as an absolute
    http(s) URL. The CLI is told this URL, and signed sign-in requests are bound
    to its host (`mvp.sso_sts`)."""
    value = api_endpoint()
    if value is None:
        if is_local_environment():
            return
        raise RuntimeError(
            "STRATOCLAVE_API_ENDPOINT must be set unless ENVIRONMENT is one of "
            f"{sorted(LOCAL_ENVIRONMENTS)} (current: {os.getenv('ENVIRONMENT', '')!r})"
        )
    if host_of(value) is None:
        raise RuntimeError(f"STRATOCLAVE_API_ENDPOINT is not an absolute http(s) URL: {value!r}")
