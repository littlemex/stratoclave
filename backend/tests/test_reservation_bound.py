"""Unit tests for `mvp.reservation_bound` (CONTRACT-hard-ceiling.md).

Pure-function tests only — no AWS, no moto. These exercise the properties the
contract states explicitly for the sound/calibrated bound and the image
dimension parsers, so a change to the arithmetic or the header parsing that
breaks soundness fails here before it ever reaches a pipeline test.

Deliberately does NOT hard-code the contract's own worked numbers (all of
which have been removed from the contract on purpose across several
revisions) — every assertion here is derived from the PROPERTY the function
must satisfy (monotonicity, coverage of the worst rate, byte-vs-character
soundness, ceil-division), not from a specific ratio.
"""
from __future__ import annotations

import struct
import zlib

import pytest

from mvp.rates import Rate
from mvp import reservation_bound as rb


def _rate(input_=5_000_000, output=15_000_000, cache_read=500_000, cache_write=6_250_000) -> Rate:
    """A rate with cache_write priced ABOVE input — reproduces the contract's
    defect #1 shape (the point being: `worst_input_side_rate_microusd` must
    still pick it up no matter what the actual numbers are)."""
    return Rate(
        input_per_mtok_microusd=input_,
        output_per_mtok_microusd=output,
        cache_read_per_mtok_microusd=cache_read,
        cache_write_per_mtok_microusd=cache_write,
    )


# ---------------------------------------------------------------------------
# Section 3: the sound bound
# ---------------------------------------------------------------------------


def test_worst_input_side_rate_picks_the_maximum_of_the_three():
    rate = _rate()
    assert rb.worst_input_side_rate_microusd(rate) == max(
        rate.input_per_mtok_microusd,
        rate.cache_read_per_mtok_microusd,
        rate.cache_write_per_mtok_microusd,
    )
    # Order independence: swapping which leg is worst must not change the
    # logic (no hard-coded "input is worst" or "cache_write is worst").
    swapped = _rate(input_=9_000_000, cache_write=1_000_000, cache_read=1_000_000)
    assert rb.worst_input_side_rate_microusd(swapped) == 9_000_000


def test_strict_bound_covers_a_settle_that_lands_entirely_on_cache_write():
    """The defect's exact shape: settle can bill fresh input, cache_read, OR
    cache_write for the SAME input-side tokens depending on what the provider
    chose to cache. The bound must cover whichever leg actually happens."""
    rate = _rate()
    input_bytes = 10_000
    bound = rb.strict_reservation_microusd(rate, input_bytes=input_bytes, max_output_tokens=0)

    from mvp.pricing import mtok_cost_for_rounding

    # However the tokens split across the three legs, as long as their COUNT
    # does not exceed input_bytes (the soundness assumption: token_count <=
    # byte_count), the actual charge for any split must not exceed the bound.
    for fresh, cache_read, cache_write in (
        (input_bytes, 0, 0),
        (0, input_bytes, 0),
        (0, 0, input_bytes),
        (input_bytes // 3, input_bytes // 3, input_bytes // 3),
    ):
        actual = (
            mtok_cost_for_rounding(fresh, rate.input_per_mtok_microusd, "ceil")
            + mtok_cost_for_rounding(cache_read, rate.cache_read_per_mtok_microusd, "ceil")
            + mtok_cost_for_rounding(cache_write, rate.cache_write_per_mtok_microusd, "ceil")
        )
        assert actual <= bound, (fresh, cache_read, cache_write, actual, bound)


def test_strict_bound_is_monotone_in_every_argument():
    rate = _rate()
    base = rb.strict_reservation_microusd(
        rate, input_bytes=1000, max_output_tokens=100, effort_multiplier=1,
        extra_input_tokens=0,
    )
    assert rb.strict_reservation_microusd(
        rate, input_bytes=1001, max_output_tokens=100, effort_multiplier=1,
    ) >= base
    assert rb.strict_reservation_microusd(
        rate, input_bytes=1000, max_output_tokens=101, effort_multiplier=1,
    ) >= base
    assert rb.strict_reservation_microusd(
        rate, input_bytes=1000, max_output_tokens=100, effort_multiplier=2,
    ) >= base
    assert rb.strict_reservation_microusd(
        rate, input_bytes=1000, max_output_tokens=100, effort_multiplier=1,
        extra_input_tokens=50,
    ) >= base


def test_strict_bound_is_total_never_raises_on_boundary_inputs():
    rate = _rate()
    for kwargs in (
        dict(input_bytes=0, max_output_tokens=0),
        dict(input_bytes=-5, max_output_tokens=-5),  # defensive clamp to 0
        dict(input_bytes=10**9, max_output_tokens=10**6, effort_multiplier=8),
    ):
        result = rb.strict_reservation_microusd(rate, **kwargs)
        assert isinstance(result, int)
        assert result >= 0


def test_byte_count_is_sound_where_char_count_is_not():
    """The exact soundness argument the contract makes: a byte-level tokeniser
    can spend MORE than one token per multi-byte character, so bounding by
    character count is unsound while bounding by byte count is not (a token
    consumes >= 1 byte, so token_count <= byte_count always holds)."""
    cjk_text = "日本語" * 100  # 3 CJK chars * 100, each 3 UTF-8 bytes
    emoji_text = "\U0001f600" * 100  # 4 UTF-8 bytes each
    ascii_text = "a" * 300

    assert rb.utf8_byte_count(cjk_text) > len(cjk_text)
    assert rb.utf8_byte_count(emoji_text) > len(emoji_text)
    # ASCII is the one case bytes == chars; still sound (equality, never less).
    assert rb.utf8_byte_count(ascii_text) == len(ascii_text)


def test_output_bound_scales_with_effort_multiplier():
    assert rb.output_bound_tokens(100, 1) == 100
    assert rb.output_bound_tokens(100, 4) == 400
    assert rb.output_bound_tokens(100, 0) == 100  # multiplier clamps to >= 1
    assert rb.output_bound_tokens(-5, 1) == 0  # negative clamps to 0


# ---------------------------------------------------------------------------
# Section 3b: images
# ---------------------------------------------------------------------------


def test_image_token_bound_is_ceil_division_and_clamps_nonpositive():
    assert rb.image_token_bound(0, 0) == 0
    assert rb.image_token_bound(-10, 100) == 0
    # Exact multiple of PIXELS_PER_TOKEN.
    p = rb.PIXELS_PER_TOKEN
    assert rb.image_token_bound(p, 1) == 1
    # One pixel over must round UP, never down (a bound may never truncate).
    assert rb.image_token_bound(p + 1, 1) == 2


def _make_png(width: int, height: int) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(
            ">I", zlib.crc32(tag + data)
        )

    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    idat = zlib.compress(b"\x00" + b"\x00" * (width * 3))
    return sig + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


def _make_gif(width: int, height: int) -> bytes:
    header = b"GIF89a" + struct.pack("<HH", width, height)
    return header + b"\x00" * 10  # enough padding for the parser to not need more


def _make_jpeg(width: int, height: int) -> bytes:
    # SOI, then an APP0 (JFIF) segment, then SOF0 carrying height/width, then EOI.
    soi = b"\xff\xd8"
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00" + b"\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    sof0 = (
        b"\xff\xc0"
        + struct.pack(">H", 17)  # length
        + b"\x08"  # precision
        + struct.pack(">HH", height, width)
        + b"\x03"  # number of components
        + b"\x01\x11\x00\x02\x11\x00\x03\x11\x00"
    )
    eoi = b"\xff\xd9"
    return soi + app0 + sof0 + eoi


def test_png_dimensions_parsed_from_header():
    assert rb.image_dimensions_from_bytes(_make_png(64, 32)) == (64, 32)


def test_gif_dimensions_parsed_from_header():
    assert rb.image_dimensions_from_bytes(_make_gif(120, 80)) == (120, 80)


def test_jpeg_dimensions_parsed_by_scanning_markers():
    assert rb.image_dimensions_from_bytes(_make_jpeg(200, 100)) == (200, 100)


def test_unrecognised_or_truncated_data_returns_none_fail_closed():
    assert rb.image_dimensions_from_bytes(b"not an image") is None
    assert rb.image_dimensions_from_bytes(b"\x89PNG\r\n\x1a\n") is None  # truncated PNG
    assert rb.image_dimensions_from_bytes(b"") is None


def test_image_dimensions_from_base64_round_trips():
    import base64

    raw = _make_png(10, 20)
    encoded = base64.b64encode(raw).decode()
    assert rb.image_dimensions_from_base64(encoded) == (10, 20)
    assert rb.image_dimensions_from_base64("not-base64!!!") is None


# ---------------------------------------------------------------------------
# Boundability assessment: images bounded-or-refused, tool use is a FACT only
# ---------------------------------------------------------------------------


def test_assess_boundability_sums_image_terms_when_all_measurable():
    survey = rb.ContentSurvey(
        text_bytes=100, image_dims=((100, 100), (200, 200)), unmeasurable_images=0,
    )
    result = rb.assess_boundability(survey)
    assert not result.refused
    assert result.extra_input_tokens == (
        rb.image_token_bound(100, 100) + rb.image_token_bound(200, 200)
    )


def test_assess_boundability_fails_closed_on_unmeasurable_image():
    survey = rb.ContentSurvey(text_bytes=100, image_dims=(), unmeasurable_images=1)
    result = rb.assess_boundability(survey)
    assert result.refused
    assert result.refusal_reason == "image_dimensions_unavailable"


# ---------------------------------------------------------------------------
# Budget enforcement is opt-in (section 7a/7b)
# ---------------------------------------------------------------------------


def test_compute_and_gate_off_for_unknown_tenant_without_touching_anything_shared():
    # No tenant_id at all -> unconditionally False, no lookup attempted.
    assert rb.dollar_pool_bound_should_compute(None) is False
    assert rb.dollar_pool_bound_should_compute("") is False
    assert rb.dollar_pool_bound_should_gate(None) is False
    assert rb.dollar_pool_bound_should_gate("") is False


def test_measurement_flag_forces_compute_but_never_gate(monkeypatch):
    """The `measured` state (contract section 0): no pool, flag on -> the
    bound is computed and recorded, but NEVER refuses admission. Collapsing
    compute/gate into one flag was a real bug an earlier version of this
    module shipped."""
    monkeypatch.setenv(rb.MEASURE_UNENFORCED_BOUND_ENV, "true")
    # No Dynamo table exists in this pure-unit test; the flag must short-
    # circuit BEFORE any lookup is attempted (that is the whole point: no
    # extra read for a rare, deliberately-enabled measurement flag either).
    assert rb.dollar_pool_bound_should_compute("any-tenant") is True
    # The flag does NOT also grant gating — gating is pool-existence only.
    assert rb.dollar_pool_bound_should_gate("any-tenant") is False


# ---------------------------------------------------------------------------
# Calibrated mode
# ---------------------------------------------------------------------------


def test_calibrated_bound_at_ratio_one_equals_strict_bound():
    rate = _rate()
    strict = rb.strict_reservation_microusd(rate, input_bytes=1000, max_output_tokens=50)
    calibrated = rb.calibrated_reservation_microusd(
        rate, input_bytes=1000, max_output_tokens=50, tokens_per_byte=1.0,
    )
    assert calibrated == strict


def test_calibrated_bound_shrinks_with_a_lower_ratio():
    rate = _rate()
    at_half = rb.calibrated_reservation_microusd(
        rate, input_bytes=10_000, max_output_tokens=0, tokens_per_byte=0.5,
    )
    at_full = rb.calibrated_reservation_microusd(
        rate, input_bytes=10_000, max_output_tokens=0, tokens_per_byte=1.0,
    )
    assert at_half < at_full


def test_calibration_store_defaults_to_one_until_set():
    store = rb.CalibrationStore()
    assert store.get("some-model") == 1.0
    store.set("some-model", 0.3)
    assert store.get("some-model") == 0.3
    store.reset()
    assert store.get("some-model") == 1.0
    with pytest.raises(ValueError):
        store.set("x", 0.0)


def test_calibration_breach_and_realized_ratio():
    store = rb.CalibrationStore()
    store.set("m", 0.5)
    # 100 realized tokens over 100 bytes -> ratio 1.0 > calibration 0.5: breach.
    ratio = rb.realized_tokens_per_byte(100, 100)
    assert ratio == 1.0
    orig = rb.calibration_store
    rb.calibration_store = store
    try:
        assert rb.calibration_breached("m", ratio) is True
        assert rb.calibration_breached("m", 0.4) is False
        assert rb.calibration_breached("m", None) is False
    finally:
        rb.calibration_store = orig


def test_realized_tokens_per_byte_none_when_no_bytes():
    assert rb.realized_tokens_per_byte(50, 0) is None
