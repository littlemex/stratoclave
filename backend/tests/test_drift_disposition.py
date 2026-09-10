"""E11 (narrowed): an unresolvable pricing key must not drift to `default` silently.

E11 originally specified a `drift_disposition(key, observation)` predicate,
called from the reserve-time resolution path, that could refuse a
reservation outright. That was withdrawn: at reserve time there is no
`observation` to consult -- usage does not exist until the provider answers
-- and `rate_for`/`reservation_bound.py` already make the `default` fallback
conservative there (a dearer `default` estimates a larger cost for the same
tokens, which shrinks the permitted usage, never grows it). Nothing at
reserve needs refusing.

The real finding survives, just relocated: `default` bounds the RATE per
token, not the CHARGE, and that distinction only matters where the actual
charge is computed -- settle, not reserve. That is E10
(`test_metering_fault_settle.py`), not this file.

What is left here, per the correction, is smaller and does not need a new
predicate: `mvp.pricing.snapshot_rates` already falls back an unresolvable
pricing key to `default` (unconditionally, and correctly per the above), but
today it does so silently. A model quietly billing at `default` for weeks is
a trust problem even though it is not a revenue one -- so the fallback must
say so, loudly, naming which key drifted.
"""
from __future__ import annotations

from structlog.testing import capture_logs

from mvp import pricing


#: Not a real registry entry under any test fixture in this suite; the whole
#: point is a key `snapshot_rates` cannot resolve.
_UNRESOLVABLE_KEY = "definitely-not-a-registered-pricing-key"


def test_unresolvable_pricing_key_still_resolves_to_default(dynamodb_mock):
    """The fallback itself is unchanged and stays that way -- E11 is about the
    silence, not the resolution. Pinned anyway (`still` is the operative word
    in the contract line) so a future change that also touches this branch
    has something to trip if it accidentally stops falling back."""
    pricing.reset_cache()
    pricing.reset_version_cache()
    default = pricing.baseline_rates()["default"]

    snap = pricing.snapshot_rates(_UNRESOLVABLE_KEY)

    assert snap.input_per_mtok_microusd == default.input_per_mtok_microusd
    assert snap.output_per_mtok_microusd == default.output_per_mtok_microusd
    assert snap.cache_read_per_mtok_microusd == default.cache_read_per_mtok_microusd
    assert snap.cache_write_per_mtok_microusd == default.cache_write_per_mtok_microusd


def test_unresolvable_pricing_key_drift_is_not_silent(dynamodb_mock):
    """The actual defect: today this fallback logs nothing, so an unresolvable
    key can drift to `default` for as long as nobody happens to read the
    ledger closely enough to notice. A warning must fire, and it must name
    the key -- a warning that only says "something fell back to default"
    would not tell an operator WHICH model to go fix.

    Not pinned to a specific event name: the handoff commits to "a distinct
    warning naming the key", not to a spelling, and asserting on the field
    that actually matters (the key appears somewhere in a warning-level
    entry) will not fail a correct implementation over a naming choice.
    """
    pricing.reset_cache()
    pricing.reset_version_cache()

    with capture_logs() as logs:
        pricing.snapshot_rates(_UNRESOLVABLE_KEY)

    warnings = [e for e in logs if e.get("log_level") == "warning"]
    assert warnings, (
        "an unresolvable pricing key drifted to `default` with no warning logged "
        "at all -- the fallback must not be silent"
    )
    assert any(_UNRESOLVABLE_KEY in str(v) for e in warnings for v in e.values()), (
        f"a warning fired, but none of them name the key that drifted: {warnings!r}"
    )


def test_a_resolvable_key_does_not_spuriously_warn(dynamodb_mock):
    """The warning is for DRIFT, not for every settle. A key that resolves
    normally (`opus`, seeded by the bundled floor) must not trip it -- an
    implementation that warns unconditionally on every `snapshot_rates` call
    would pass the two tests above and make the real warning useless noise."""
    pricing.reset_cache()
    pricing.reset_version_cache()

    with capture_logs() as logs:
        pricing.snapshot_rates("opus")

    warnings = [e for e in logs if e.get("log_level") == "warning"]
    assert not any("opus" in str(v) for e in warnings for v in e.values()), (
        f"a key that resolved normally must not trigger the drift warning: {warnings!r}"
    )
