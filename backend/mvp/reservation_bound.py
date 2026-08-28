"""The hard-ceiling reservation bound (contract: CONTRACT-hard-ceiling.md).

`estimate_cost_microusd` (mvp/pricing.py) is a heuristic, not a bound: it prices
`char_count // 3` input tokens at the plain input rate and ignores cache-write
entirely. Two independent gaps follow, and both let settle record more than
admission checked:

  1. No cache-write leg — `rate_usage` bills four components (input, output,
     cache_read, cache_write); the estimate prices three. Read the shipped
     rate document (mvp/rates.py / the pricing config table) yourself for how
     cache_write compares to input — deliberately not quoted here, see the
     note on removed numbers below.
  2. `char_count // 3` is calibrated for English; a byte-level tokenizer can
     spend more than one token per multi-byte character, so a script whose
     characters tokenise close to one-for-one is under-reserved by roughly
     that same ratio, with no caching involved at all.

This module is the fix: a SOUND upper bound (`strict` mode — no assumption
about provider behaviour beats it, by construction, within the assumptions
section 4 states explicitly — see `ASSUMPTIONS_THE_GUARANTEE_RESTS_ON` below)
and a measured bound (`calibrated` mode — a much tighter reservation than
strict, and an alarm when a settle exceeds the calibration, but — per the
contract owner's explicit correction after review — NOT a ceiling guarantee:
the same argument that rules out a mixed strict/tool-use pool applies here too,
because a tokens-per-byte calibration cannot cover provider-injected tokens
either. Calibrated mode must never be DESCRIBED as hard; it is a monitored,
much cheaper reservation with a fail-closed alarm on drift, nothing more).
Everything here is a pure function over a `Rate` and token/byte counts; it
never touches Dynamo or the network — the reserve chokepoint
(`mvp/_pipeline.py`) is the only caller.

No specific token counts or cost ratios are quoted in this module's comments,
by design: an earlier contract draft did, and the note accompanying its
removal is direct — a number copied from the contract could let an
implementation special-case exactly what the (withheld) verification checks
and nothing else. Where a ratio would be illustrative, measure it instead.

Scope, per the contract owner's explicit decision (section 2): token
accounting itself is untouched — settle already records Bedrock's own usage
block, so every token the provider billed is captured regardless of source.
What this module fixes is only the RESERVE-side prediction, and the two things
that prediction cannot cover are treated differently, on purpose:

  * Images ARE in scope — token cost scales with pixel dimensions, which are
    NOT a declared field on a Bedrock Converse image part (raw bytes or an S3
    reference) and must be read from the image's own header
    (`image_dimensions_from_bytes` et al.). This is a missing TERM in the
    bound, not an exclusion, and it fails CLOSED: an image whose dimensions
    cannot be determined refuses the request (`assess_boundability`) rather
    than being admitted with the term skipped. An S3-referenced image is
    refused unconditionally (sizing-then-referencing is a
    time-of-check-to-time-of-use race — see `mvp.anthropic._build_bedrock_kwargs`,
    which does not even support an S3 source today, so this is already
    enforced structurally).
  * **Tool use needs no special rule, and this was checked rather than
    assumed** (contract section 4, correcting an earlier, incoherent draft of
    this module that tried to refuse or flag it): `_reject_server_side_tools`
    in `mvp/anthropic.py` already refuses Anthropic's server-executed tools
    (web search, web fetch, code execution, computer, bash, text editor) on
    every route, because Bedrock's Converse API cannot express them. Only
    CLIENT-side tools pass through, and their schemas travel in the payload
    the gateway sends — the byte survey counts them (see
    `mvp.anthropic._survey_and_hash_converse_kwargs`'s `toolConfig` term) —
    and a tool RESULT arrives in a later request whose bytes are also
    measured there. Nothing about tool use is unbounded on this route, so
    this module has no tool-use-specific branch at all.

Budget enforcement is opt-in and is a FOUR-STATE model, not a binary one
(contract section 0): `accounting` (no limit, nothing computed, nothing
refused), `measured` (no limit, but a flag is on so the bound IS computed and
meant to be recorded — see the caveat on recording in the implementation
report), `enforced` (a dollar pool exists, the bound gates admission), and
`shadow` (a pool exists but gating itself is switched off — NOT modelled by
this module; see `dollar_pool_bound_should_compute`'s docstring for exactly
what is missing). `dollar_pool_bound_should_compute` and
`dollar_pool_bound_should_gate` below are the two separate questions a route
must ask — deliberately two functions, not one, because collapsing them was
a real bug an earlier version of this module shipped (the measurement flag
making an unmeasurable image refuse a tenant that was never supposed to see
a refusal at all).

The guarantee itself is stated with its boundary, not as a slogan — see
`OPERATOR_FACING_MODE_EXPLANATION` below, which names BOTH of strict mode's
alarmed failure modes (a reservation-less charge, and a provider that
exceeded its output ceiling) rather than either omitting them or naming only
one.
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from typing import Optional

from .pricing import mtok_cost_for_rounding
from .rates import Rate


# ---------------------------------------------------------------------------
# The assumptions the guarantee rests on — stated, not glossed
# ---------------------------------------------------------------------------
# CONTRACT-hard-ceiling.md section 4: "Put these where a reader of the
# guarantee will see them." This is that place — imported and quoted wherever
# the strict-mode guarantee is documented (this module's own docstring above,
# and `mvp._pipeline`'s reserve chokepoint), so the guarantee is never stated
# without its conditions in the same breath.
ASSUMPTIONS_THE_GUARANTEE_RESTS_ON = (
    "The strict-mode ceiling guarantee rests on four assumptions. Each is "
    "either checked or explicitly left unchecked — none is silently assumed:\n"
    "1. The provider respects max_output_tokens * effort_multiplier for TOTAL "
    "output including reasoning tokens. CHECKED at settle, per request, with "
    "its own cause code on violation — but checking after the fact is "
    "verification, not prevention; the guarantee is conditional on this "
    "holding.\n"
    "2. The input-side counts PARTITION the prompt, so their sum is bounded "
    "by the payload's byte length. UNCHECKED: if a provider ever counted one "
    "token as both read and written (double-counted across "
    "cache_read/cache_write/fresh input), the bound breaks and this gateway "
    "has no way to detect that from the usage block alone.\n"
    "3. A hold is not reaped while its charge can still arrive. ENFORCED by "
    "the reap-timeout derivation in mvp._pipeline (request deadline + retry "
    "budget + clock-skew margin, asserted at startup) — but a settle that "
    "still arrives after a reap anyway is booked reservation-less, with its "
    "own cause code, and alarmed; it is not prevented outright, it is bounded "
    "to a named, alarmed exception.\n"
    "4. Every charge passes through reserve. AUDITED (see the bypass audit in "
    "the implementation report) rather than provable from this module alone —"
    " a single uncaught bypass anywhere in the gateway voids the guarantee "
    "regardless of how sound this bound is."
)


# ---------------------------------------------------------------------------
# 1. The sound bound (strict mode)
# ---------------------------------------------------------------------------
# Reserve is the wrong time to find out what the provider chose to cache: the
# gateway never sees that decision until settle. So instead of pricing the
# input-side tokens at the input rate and hoping nothing gets cache-written,
# the bound prices EVERY input-side token (fresh input, cache_read, AND
# cache_write — the three components a token that was actually SENT to the
# provider can land in) at the WORST of the three rates. The provider cannot
# cache-write content it was never sent, so bounding the total input-side
# token count once, at the worst rate, covers all three legs with no
# assumption about which one the provider picks:
#
#     fresh + cache_read + cache_write <= input_tokens <= input_bytes
#     cost  <= input_tokens * max(input_rate, cache_read_rate, cache_write_rate)
#
# `input_bytes` must be a BOUND on the token count, not an estimate of it — a
# UTF-8 byte count is sound because a byte-level tokenizer spends at least one
# byte per token (it can spend several on one multi-byte character, but never
# less than one), so `token_count <= byte_count` always. A character count is
# NOT sound for the same reason a 3-chars-per-token heuristic is not: one
# character can cost more than one token.


def worst_input_side_rate_microusd(rate: Rate) -> int:
    """The rate every input-side token is bounded at: the most expensive of the
    three legs a sent token can be billed under (fresh input / cache_read /
    cache_write). Reads the LIVE table rather than hard-coding which leg is
    worst, so a future rate edit that changes the ordering (or adds a fifth
    leg some day) cannot quietly reopen the cache-write gap this bound exists
    to close."""
    return max(
        rate.input_per_mtok_microusd,
        rate.cache_read_per_mtok_microusd,
        rate.cache_write_per_mtok_microusd,
    )


def output_bound_tokens(max_output_tokens: int, effort_multiplier: int = 1) -> int:
    """Bound on billable output tokens: `max_output_tokens * effort_multiplier`.

    Sound only to the extent the provider honours `max_output_tokens` (the
    contract calls this out explicitly: "already a bound provided the provider
    respects it" — this module does not re-derive that guarantee, it inherits
    it from the existing reservation behaviour)."""
    return max(int(max_output_tokens), 0) * max(int(effort_multiplier), 1)


def strict_reservation_microusd(
    rate: Rate,
    *,
    input_bytes: int,
    max_output_tokens: int,
    effort_multiplier: int = 1,
    extra_input_tokens: int = 0,
) -> int:
    """The SOUND upper bound on this request's settled cost, at `rate`.

    `input_bytes` is the UTF-8 byte count of every text part the gateway sends
    as input (contract item 1, "the token count itself must be bounded, not
    estimated"). `extra_input_tokens` folds in the image-dimension token term
    `assess_boundability` below computes; it is additive because those tokens
    are input-side too and share the same worst-rate pricing. Monotone in
    every argument (more bytes/tokens never lowers the bound) and total
    (defined for any non-negative input), which is what "no input may make the
    bound smaller than settle would record" requires.
    """
    input_tokens_bound = max(int(input_bytes), 0) + max(int(extra_input_tokens), 0)
    worst_rate = worst_input_side_rate_microusd(rate)
    input_cost = mtok_cost_for_rounding(input_tokens_bound, worst_rate, "ceil")
    output_cost = mtok_cost_for_rounding(
        output_bound_tokens(max_output_tokens, effort_multiplier),
        rate.output_per_mtok_microusd,
        "ceil",
    )
    return input_cost + output_cost


# ---------------------------------------------------------------------------
# 2. The calibrated bound
# ---------------------------------------------------------------------------
# Same shape, but the input-side token count is `ceil(bytes * tokens_per_byte)`
# for a MEASURED `tokens_per_byte` instead of `bytes` itself (i.e. instead of
# the maximally-conservative 1-token-per-byte assumption strict mode makes).
# Much cheaper in headroom than strict — but, per the contract owner's
# explicit correction, this is NOT a ceiling guarantee at any calibration
# accuracy: a tokens-per-byte ratio, however well measured, still cannot cover
# provider-injected tokens (the same reason a mixed strict/tool-use pool does
# not work). What calibrated mode actually offers is a much tighter
# reservation plus an alarm when a settle exceeds the calibration —
# `CalibrationStore` below is where that number lives and where every settle
# reports the realised ratio so drift alarms instead of silently widening the
# gap between reservation and reality.

# Fallback ratio for a (pricing_key) with no calibration on file: 1.0 degrades
# calibrated mode to the strict bound exactly (bytes * 1.0 == bytes) — a
# tenant switched into "calibrated" before anyone measured anything pays
# strict-mode headroom rather than being silently under-reserved. Calibrated
# mode only gets cheaper than strict once a real measurement lowers this.
_DEFAULT_TOKENS_PER_BYTE = 1.0

_CALIBRATION_ENV_PREFIX = "STRATOCLAVE_TOKENS_PER_BYTE_"


class CalibrationStore:
    """Per-`pricing_key` measured tokens-per-byte ratio for calibrated mode.

    In-memory + env-seeded: this is deliberately NOT wired to a DynamoDB admin
    table in this change (that would need its own hot-reload/versioning story
    mirroring `PricingConfigRepository`, which is out of scope here). The
    contract's own ship gate ("refuse to ship calibrated if the shadow run
    produces even one settle above the calibration") means these numbers are
    not meant to be guessed — they come from the shadow run described in the
    contract's Measurement section, which is an operational step this change
    does not perform. Until that run has produced real numbers, every key
    reads back the sound-but-expensive fallback above.
    """

    def __init__(self) -> None:
        self._ratios: dict[str, float] = {}

    def get(self, pricing_key: str) -> float:
        if pricing_key in self._ratios:
            return self._ratios[pricing_key]
        env_key = _CALIBRATION_ENV_PREFIX + pricing_key.upper().replace("-", "_")
        raw = os.getenv(env_key)
        if raw:
            try:
                v = float(raw)
                if v > 0:
                    return v
            except ValueError:
                pass
        return _DEFAULT_TOKENS_PER_BYTE

    def set(self, pricing_key: str, tokens_per_byte: float) -> None:
        if tokens_per_byte <= 0:
            raise ValueError("tokens_per_byte must be positive")
        self._ratios[pricing_key] = float(tokens_per_byte)

    def reset(self) -> None:
        """Test hook."""
        self._ratios.clear()


calibration_store = CalibrationStore()


# The guarantee, stated with its boundary rather than as a slogan
# (CONTRACT-hard-ceiling.md, final revision — supersedes an earlier draft's
# strict-vs-calibrated comparison, which does not apply while calibrated mode
# is phase 2 / out of scope, and a still-earlier draft that named only ONE
# visible failure mode). Kept as one importable string so every surface that
# states the guarantee to an operator (admin API response, docs, alarm
# runbook) quotes the same text rather than each writing its own paraphrase
# that drifts from what strict mode actually guarantees. Neither exception
# clause is optional trim — omitting either overstates the guarantee.
OPERATOR_FACING_MODE_EXPLANATION = (
    "For admitted requests in strict mode, the pool cannot be overspent, "
    "except for two alarmed, named failure modes: a charge marked "
    "reservation-less (its hold was reaped before the settle arrived), and a "
    "provider that exceeded the output ceiling it was given. Both are "
    "recorded with their own cause code and alarmed; there is no unnamed "
    "third way for strict mode to overspend."
)


def calibrated_reservation_microusd(
    rate: Rate,
    *,
    input_bytes: int,
    max_output_tokens: int,
    effort_multiplier: int = 1,
    extra_input_tokens: int = 0,
    tokens_per_byte: float,
) -> int:
    """The calibrated bound: same shape as `strict_reservation_microusd`, but the
    text-derived input token count is `ceil(input_bytes * tokens_per_byte)`
    instead of `input_bytes` itself. `extra_input_tokens` (envelope overhead) is
    NOT scaled by the calibration — it is already a token count, not bytes."""
    text_tokens_bound = math.ceil(max(int(input_bytes), 0) * max(tokens_per_byte, 0.0))
    input_tokens_bound = text_tokens_bound + max(int(extra_input_tokens), 0)
    worst_rate = worst_input_side_rate_microusd(rate)
    input_cost = mtok_cost_for_rounding(input_tokens_bound, worst_rate, "ceil")
    output_cost = mtok_cost_for_rounding(
        output_bound_tokens(max_output_tokens, effort_multiplier),
        rate.output_per_mtok_microusd,
        "ceil",
    )
    return input_cost + output_cost


def realized_tokens_per_byte(
    actual_input_side_tokens: int, input_bytes: int
) -> Optional[float]:
    """The tokens-per-byte ratio THIS settle actually realised, for the
    calibration drift signal (contract item 3: "every settle must report the
    realised tokens per byte so a stale calibration alarms"). None when
    `input_bytes` is 0 (nothing to divide by — an empty-input request reports
    no ratio rather than a division artefact)."""
    if input_bytes <= 0:
        return None
    return max(actual_input_side_tokens, 0) / input_bytes


def calibration_breached(
    pricing_key: str, realized_ratio: Optional[float]
) -> bool:
    """True iff `realized_ratio` exceeds the calibration on file for
    `pricing_key` — i.e. THIS settle alone proves the calibrated bound was not
    a bound for this request. Per the contract, this is a fail-closed alarm
    event, not a warning: calibrated mode's entire premise is that no settle
    ever exceeds the calibration, so a single breach means the bound currently
    in force for this tenant/model is wrong, right now, not just risky."""
    if realized_ratio is None:
        return False
    return realized_ratio > calibration_store.get(pricing_key)


# ---------------------------------------------------------------------------
# 3. Boundability: images are a missing term
# ---------------------------------------------------------------------------
# (Revised by the contract owner after adversarial review, twice: once to
# require header parsing instead of a declared dimension field, and once more
# to REMOVE tool use from this section entirely — section 4 established that
# tool use needs no special rule at all, because Bedrock Converse cannot
# express the provider-injected shapes that would be unbounded, and
# client-side tool schemas/results ARE measured. This comment describes only
# the current, final rule.)
#
# Images: token cost scales with pixel area, but the pixels are NOT a declared
# field on a Bedrock Converse image part — Converse carries an image as raw
# bytes or an S3 reference, so the dimensions have to be READ FROM THE IMAGE'S
# OWN HEADER (`image_dimensions_from_bytes` below parses PNG/GIF/JPEG/WEBP
# headers without decoding pixel data). That makes an image a MISSING TERM in
# the bound (`image_token_bound`), not an exclusion, PROVIDED the header
# parses. Three ways it does not, and all three refuse rather than admit with
# the term skipped or guessed:
#   * an unsupported/unrecognised format,
#   * a truncated or malformed header the parser cannot read fields from,
#   * a source this gateway does not fetch bytes for at all — an S3 reference.
#     This module's choice, stated plainly: refuse an S3-referenced image
#     rather than fetch it from the reserve path. Fetching would add a network
#     round-trip (and a new failure mode — timeout, access-denied, wrong
#     region) to the hot admission path for every image-bearing request; a
#     caller that wants the image bounded can send it inline instead.

# Pixels-per-token divisor for the image bound. The contract deliberately does
# not quote a number for this (numbers were removed from the contract on
# purpose — see the module docstring), so 750 here is THIS implementation's
# own placeholder, not a contract-derived constant: it is in the neighbourhood
# publicly documented for comparable multimodal tokenizers, but has not been
# calibrated against this gateway's actual provider billing. Named and
# overridable so an operator can correct it without a code change once the
# shadow run (contract: "Measurement required before this ships") produces a
# real number — and it only needs to be a BOUND (an over-estimate), not exact,
# for `image_token_bound` to stay sound.
PIXELS_PER_TOKEN = int(os.getenv("STRATOCLAVE_IMAGE_PIXELS_PER_TOKEN", "750"))


def image_token_bound(width_px: int, height_px: int) -> int:
    """Worst-case token count for one image of `width_px` x `height_px`,
    ceil(pixels / PIXELS_PER_TOKEN). Negative/zero dimensions bound to 0.

    Deliberately does NOT sanity-cap an absurd decoded dimension (a corrupt
    header claiming a billion-pixel image): the contract's own instruction is
    "a bound computed from an absurd dimension refuses the request anyway via
    the size rule" — i.e. section 2b's exact `bound > pool_limit` refusal is
    the backstop, not a second threshold duplicated here. Adding a cap in two
    places would let them drift; this function stays a pure, unclamped
    ceil-division and trusts the caller's admission check to catch the result.
    """
    w = max(int(width_px), 0)
    h = max(int(height_px), 0)
    pixels = w * h
    if pixels <= 0:
        return 0
    return -(-pixels // PIXELS_PER_TOKEN)  # ceil division, integer-only


@dataclass(frozen=True)
class ContentSurvey:
    """What a route handler found while walking a request's content, in the
    shape `assess_boundability` needs. Built by each route from its own body
    schema (Anthropic content blocks, OpenAI chat/messages, OpenAI Responses
    input) — this dataclass is the protocol-agnostic seam between "what shape
    is this request" and "what can the bound actually cover".

    `image_dims`: one `(width, height)` entry per image part whose pixel
    dimensions the route determined (by decoding the image bytes — see
    `image_dimensions_from_base64` — or from an explicit width/height the
    request itself carries). A dimension the route could NOT determine (a
    remote URL/file reference this gateway does not fetch, or undecodable
    data) must NOT be added here — it belongs in `unmeasurable_images` instead,
    which has no term and refuses (see `assess_boundability`).

    `unmeasurable_images`: count of image parts present in the request whose
    dimensions the route could not determine. Any non-zero value here means
    the request cannot be bounded and must be refused — the contract treats
    "dimensions were obtainable and weren't obtained" as a gap to close, not a
    carve-out.

    Tool use has no field here at all: section 4 established that it needs no
    special rule (see the module docstring's Scope section) — a client tool's
    schema is already inside `text_bytes` via the route's survey of
    `toolConfig`, and a server-executed tool cannot reach Bedrock Converse in
    the first place.
    """

    text_bytes: int = 0
    image_dims: tuple = field(default_factory=tuple)
    unmeasurable_images: int = 0


@dataclass(frozen=True)
class BoundabilityAssessment:
    """Result of `assess_boundability`.

    `refusal_reason` set (only ever for an unmeasurable image) means the
    caller MUST refuse before any upstream call — acceptance criterion 2's
    "never admitted under a bound that silently fails to cover it".
    Otherwise, `extra_input_tokens` is the bounded image-token term to fold
    into the reservation (`strict_reservation_microusd`'s /
    `calibrated_reservation_microusd`'s `extra_input_tokens=`).
    """

    refusal_reason: Optional[str] = None
    extra_input_tokens: int = 0

    @property
    def refused(self) -> bool:
        return self.refusal_reason is not None


def assess_boundability(survey: ContentSurvey) -> BoundabilityAssessment:
    """Turn a `ContentSurvey` into either a refusal or a bounded extra-token
    term. Called by every route BEFORE reserving, so an unmeasurable image is
    refused before any hold is created and before any upstream call is made."""
    if survey.unmeasurable_images > 0:
        # No sound term exists for a dimension the gateway does not have — this
        # is the one case left that is refused rather than bounded or flagged,
        # because (unlike tool use) the contract does not carve it out: the
        # dimensions are supposed to be obtainable from the request.
        return BoundabilityAssessment(refusal_reason="image_dimensions_unavailable")

    extra = sum(image_token_bound(w, h) for (w, h) in survey.image_dims)
    return BoundabilityAssessment(extra_input_tokens=extra)


# ---------------------------------------------------------------------------
# Image dimension decoding
# ---------------------------------------------------------------------------
# Pure-Python header parsers for the four formats Anthropic's Messages API
# accepts as inline image sources (PNG, JPEG, GIF, WEBP) — no Pillow
# dependency, because all that is needed is the width/height fields every one
# of these formats stores, uncompressed, in its first few dozen bytes. A
# format this cannot parse (or a source the route never gets bytes for at all,
# e.g. a remote URL) returns None, which `ContentSurvey.unmeasurable_images`
# must count and `assess_boundability` refuses.

def image_dimensions_from_base64(data_b64: str) -> Optional[tuple[int, int]]:
    """`(width, height)` decoded from a base64-encoded image, or None if the
    data does not decode or its format is not recognised."""
    import base64
    import binascii

    try:
        raw = base64.b64decode(data_b64, validate=False)
    except (binascii.Error, ValueError):
        return None
    return image_dimensions_from_bytes(raw)


def image_dimensions_from_bytes(raw: bytes) -> Optional[tuple[int, int]]:
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        return _png_dimensions(raw)
    if raw[:6] in (b"GIF87a", b"GIF89a"):
        return _gif_dimensions(raw)
    if raw[:2] == b"\xff\xd8":
        return _jpeg_dimensions(raw)
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return _webp_dimensions(raw)
    return None


def _png_dimensions(raw: bytes) -> Optional[tuple[int, int]]:
    # IHDR is always the first chunk, immediately after the 8-byte signature:
    # 4-byte length, 4-byte "IHDR", 4-byte width, 4-byte height (big-endian).
    if len(raw) < 24 or raw[12:16] != b"IHDR":
        return None
    width = int.from_bytes(raw[16:20], "big")
    height = int.from_bytes(raw[20:24], "big")
    return (width, height)


def _gif_dimensions(raw: bytes) -> Optional[tuple[int, int]]:
    # Logical screen descriptor immediately follows the 6-byte signature:
    # 2-byte width, 2-byte height (little-endian).
    if len(raw) < 10:
        return None
    width = int.from_bytes(raw[6:8], "little")
    height = int.from_bytes(raw[8:10], "little")
    return (width, height)


# SOF (start-of-frame) markers: the segment that carries JPEG's pixel
# dimensions. Excludes DHP (0xDE)/EXP (0xDF), which are rare and not "the"
# frame dimensions.
_JPEG_SOF_MARKERS = frozenset(
    {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
)
# Standalone markers carry NO length field and must be skipped by exactly 2
# bytes: TEM (0x01) and the RSTn restart markers (0xD0-0xD7). SOI/EOI (0xD8/
# 0xD9) are handled by the caller / loop termination.
_JPEG_STANDALONE_MARKERS = frozenset({0x01}) | set(range(0xD0, 0xD8))


def _jpeg_dimensions(raw: bytes) -> Optional[tuple[int, int]]:
    """Scan JPEG markers for the first SOF segment's height/width.

    JPEG has no single "header": dimensions live in whichever SOFn segment
    starts the frame, after any number of APPn/COM/quantization-table segments
    of varying length — so this walks the marker stream rather than reading a
    fixed offset.
    """
    n = len(raw)
    i = 2  # past the 2-byte SOI marker (0xFFD8), already checked by the caller.
    while i + 1 < n:
        if raw[i] != 0xFF:
            i += 1  # not a marker byte (e.g. entropy-coded fill) — skip.
            continue
        marker = raw[i + 1]
        if marker == 0xD9:  # EOI — end of image, no SOF found.
            return None
        if marker in _JPEG_STANDALONE_MARKERS:
            i += 2
            continue
        if i + 4 > n:
            return None
        seg_len = (raw[i + 2] << 8) + raw[i + 3]
        if marker in _JPEG_SOF_MARKERS:
            if i + 9 > n:
                return None
            height = (raw[i + 5] << 8) + raw[i + 6]
            width = (raw[i + 7] << 8) + raw[i + 8]
            return (width, height)
        if seg_len < 2:
            return None  # malformed segment length — cannot safely advance.
        i += 2 + seg_len
    return None


def _webp_dimensions(raw: bytes) -> Optional[tuple[int, int]]:
    """VP8 (lossy), VP8L (lossless) and VP8X (extended) container dimensions.

    All three encode dimensions differently; this covers the three chunk types
    the format defines rather than guessing one. Unrecognised/truncated data
    returns None (refused, not guessed)."""
    if len(raw) < 30:
        return None
    chunk = raw[12:16]
    if chunk == b"VP8X":
        # Extended: 24-bit (width-1) then 24-bit (height-1), little-endian,
        # starting 4 bytes into the chunk payload (after a 1-byte flags field
        # and 3 reserved bytes).
        w_minus1 = raw[24] | (raw[25] << 8) | (raw[26] << 16)
        h_minus1 = raw[27] | (raw[28] << 8) | (raw[29] << 16)
        return (w_minus1 + 1, h_minus1 + 1)
    if chunk == b"VP8L":
        # Lossless: a 1-byte signature (0x2F) then 14 bits width-1 / 14 bits
        # height-1 packed little-endian across 4 bytes.
        if raw[20] != 0x2F:
            return None
        bits = raw[21] | (raw[22] << 8) | (raw[23] << 16) | (raw[24] << 24)
        width = (bits & 0x3FFF) + 1
        height = ((bits >> 14) & 0x3FFF) + 1
        return (width, height)
    if chunk == b"VP8 ":
        # Lossy: dimensions sit at a fixed offset past the frame tag + the
        # 3-byte 0x9D012A start code, as two 16-bit little-endian fields whose
        # top 2 bits are a scale factor this bound ignores (it only needs the
        # 14-bit magnitude, which is already a worst case for scale=0).
        if raw[23:26] != b"\x9d\x01\x2a":
            return None
        if len(raw) < 30:
            return None
        w_field = raw[26] | (raw[27] << 8)
        h_field = raw[28] | (raw[29] << 8)
        return (w_field & 0x3FFF, h_field & 0x3FFF)
    return None


def utf8_byte_count(text: str) -> int:
    """UTF-8 byte count of `text` — the sound unit for the input-side token
    bound (contract item 1: "a token consumes at least one byte"). Not a
    character count: `len(text)` undercounts every multi-byte character (all of
    CJK, emoji, accented Latin) and is exactly the unsoundness this function
    exists to avoid re-introducing at a call site."""
    return len(text.encode("utf-8", errors="surrogatepass"))


# ---------------------------------------------------------------------------
# Canonical-payload surveyors, shared across routes (CONTRACT-hard-ceiling.md
# section 3a)
# ---------------------------------------------------------------------------
# One surveyor per WIRE SHAPE a route actually sends, not one per route: two
# routes (`/v1/chat/completions`'s Converse leg and `/v1/messages`) both send
# Bedrock Converse `kwargs`, so they share `survey_and_hash_converse_kwargs`
# rather than each walking the same dict shape with its own subtly-different
# copy. A route picks the surveyor that matches what it is ABOUT to hand its
# client library, never the request body it received (section 3a's own
# requirement) and never a shape it does not actually send.


def survey_and_hash_converse_kwargs(kwargs: dict) -> tuple["ContentSurvey", int, str]:
    """`(survey, payload_bytes, payload_hash)` for Bedrock CONVERSE `kwargs`
    (the shape both `/v1/messages` and `/v1/chat/completions`'s Converse leg
    build and send via `bedrock_runtime_client().converse(**kwargs)`).

    `payload_bytes` is the length of the CANONICAL NON-IMAGE BYTES — the exact
    concatenation of every UTF-8 byte string counted into `survey.text_bytes`,
    in wire order (system text first, then each message's content blocks in
    order, each part separated by a single NUL byte), EXCLUDING image payload
    bytes (section 3b: those are priced by dimension instead, so counting them
    here too would double-charge).

    `payload_hash`, per the contract's OWN correction of an earlier draft of
    this function, covers the ENTIRE serialised payload INCLUDING image bytes
    — a length-only pin would let a retry swap an image's bytes while keeping
    the same non-image length, and the length pin alone would not catch that.
    The hash therefore hashes `canonical_text + image bytes in encounter
    order`, not `canonical_text` alone.

    Every block type that contributes REAL, billable content to THIS request
    is counted, including tool schemas and multi-turn history:
      - `text` blocks: their UTF-8 bytes.
      - `image` blocks: NOT into the byte count (see above); their pixel
        dimensions feed the survey's image term instead.
      - `toolResult` blocks: recurse into their own content (text/images a
        prior tool call returned, now being fed back as input).
      - `toolUse` blocks: JSON-serialised (name + input args). These appear
        when multi-turn history echoes back a PRIOR turn's tool call — for
        THIS request they are genuine input bytes Bedrock will tokenise, not
        output; skipping them on the theory that they were "already bounded
        by that turn's max_output_tokens" is true for the turn that PRODUCED
        them but irrelevant here — each HTTP request reserves and settles
        independently, and skipping them would under-count THIS request's
        own input, which is exactly the unsoundness this module exists to
        avoid.
      - `reasoningContent` blocks: the `reasoningText.text` (and `signature`,
        if present) bytes — same multi-turn-history reasoning as `toolUse`.
      - `cachePoint` blocks: no real content (a zero-byte cache marker) —
        correctly contributes nothing.
      - `toolConfig` (top-level, not per-message): a client's declared tool
        schemas travel in the payload the gateway sends, so their
        JSON-serialised bytes are counted once, here, rather than not at all.

    The concatenation order is deterministic and documented here precisely so
    an independent verifier can reproduce it byte-for-byte from the same
    `kwargs` (acceptance criterion 7: "compare against an independent
    capture, not against the same code that recorded them").
    """
    import hashlib
    import json

    text_parts: list[bytes] = []
    hash_parts: list[bytes] = []  # text_parts' bytes, interleaved with image bytes
    image_dims: list[tuple[int, int]] = []
    unmeasurable_images = 0

    def _add_text(s: str) -> None:
        b = s.encode("utf-8", errors="surrogatepass")
        text_parts.append(b)
        hash_parts.append(b)

    def _walk(blocks) -> None:
        nonlocal unmeasurable_images
        for block in blocks:
            if not isinstance(block, dict):
                continue
            if "text" in block:
                text = block.get("text")
                if isinstance(text, str):
                    _add_text(text)
            elif "image" in block:
                raw = (block.get("image") or {}).get("source", {}).get("bytes")
                dims = image_dimensions_from_bytes(raw) if isinstance(raw, bytes) else None
                if dims is None:
                    unmeasurable_images += 1
                else:
                    image_dims.append(dims)
                if isinstance(raw, bytes):
                    # Hash-only (section 3a): image bytes are NOT in the
                    # length/text byte count, but the hash must still change
                    # if a retry swaps them while keeping every text part
                    # byte-identical.
                    hash_parts.append(raw)
            elif "toolResult" in block:
                _walk((block.get("toolResult") or {}).get("content", []) or [])
            elif "toolUse" in block:
                tu = block.get("toolUse") or {}
                _add_text(json.dumps(
                    {"name": tu.get("name", ""), "input": tu.get("input", {})},
                    sort_keys=True,
                ))
            elif "reasoningContent" in block:
                rt = (block.get("reasoningContent") or {}).get("reasoningText") or {}
                text = rt.get("text")
                if isinstance(text, str):
                    _add_text(text)
                sig = rt.get("signature")
                if isinstance(sig, str):
                    _add_text(sig)
            # cachePoint: a zero-content marker, contributes nothing.

    for sys_block in kwargs.get("system") or []:
        if isinstance(sys_block, dict) and isinstance(sys_block.get("text"), str):
            _add_text(sys_block["text"])
    for msg in kwargs.get("messages", []):
        _walk(msg.get("content", []) or [])
    tool_config = kwargs.get("toolConfig")
    if tool_config:
        # Client tool schemas travel in the payload the gateway sends, so the
        # byte term must cover them too — sorted keys so the same schema
        # always hashes/counts identically regardless of dict ordering.
        _add_text(json.dumps(tool_config, sort_keys=True))

    canonical_text = b"\x00".join(text_parts)
    canonical_for_hash = b"\x00".join(hash_parts)
    survey = ContentSurvey(
        text_bytes=len(canonical_text),
        image_dims=tuple(image_dims),
        unmeasurable_images=unmeasurable_images,
    )
    return (
        survey,
        len(canonical_text),
        hashlib.sha256(canonical_for_hash).hexdigest(),
    )


def survey_and_hash_openai_chat_payload(payload: dict) -> tuple["ContentSurvey", int, str]:
    """`(survey, payload_bytes, payload_hash)` for the OpenAI Chat Completions
    JSON `payload` `/v1/chat/completions` sends VERBATIM to bedrock-mantle
    (`mvp.chat_completions._mantle_chat_completion`) — a plain
    `{"messages": [...], "tools": [...], ...}` dict, NOT Converse `kwargs`.

    Mirrors `survey_and_hash_converse_kwargs`'s rules on the OpenAI wire
    shape: `content` is a string or a list of parts (`text` /
    `image_url` — the OpenAI spelling for an inline/remote image); an
    assistant message's `tool_calls` are genuine input bytes when echoed back
    in multi-turn history (same reasoning as `toolUse` in the Converse
    surveyor); a `tool` role message's `content` is a tool result being fed
    back as input; top-level `tools` (function schemas) are counted once.

    `image_url.url` is only measurable when it is an inline base64 data URI
    (`data:image/...;base64,<data>`) — a remote `https://` URL is the same
    time-of-check-to-time-of-use problem section 3b refuses for an S3
    reference, and this gateway does not fetch it.
    """
    import base64
    import hashlib
    import json

    text_parts: list[bytes] = []
    hash_parts: list[bytes] = []
    image_dims: list[tuple[int, int]] = []
    unmeasurable_images = 0

    def _add_text(s: str) -> None:
        b = s.encode("utf-8", errors="surrogatepass")
        text_parts.append(b)
        hash_parts.append(b)

    def _add_image_url(url) -> None:
        nonlocal unmeasurable_images
        if not isinstance(url, str) or not url.startswith("data:"):
            unmeasurable_images += 1
            return
        try:
            header, b64data = url.split(",", 1)
        except ValueError:
            unmeasurable_images += 1
            return
        if ";base64" not in header:
            unmeasurable_images += 1
            return
        try:
            raw = base64.b64decode(b64data, validate=False)
        except Exception:  # noqa: BLE001
            unmeasurable_images += 1
            return
        dims = image_dimensions_from_bytes(raw)
        if dims is None:
            unmeasurable_images += 1
        else:
            image_dims.append(dims)
        hash_parts.append(raw)

    def _content(value) -> None:
        if isinstance(value, str):
            _add_text(value)
        elif isinstance(value, list):
            for part in value:
                if not isinstance(part, dict):
                    continue
                ptype = part.get("type")
                if ptype == "text":
                    text = part.get("text")
                    if isinstance(text, str):
                        _add_text(text)
                elif ptype == "image_url":
                    _add_image_url((part.get("image_url") or {}).get("url"))
                elif "text" in part and isinstance(part.get("text"), str):
                    # A part with no/other `type` but a text field (some
                    # clients omit `type` for plain text parts) — count it
                    # rather than silently drop real content.
                    _add_text(part["text"])

    for msg in payload.get("messages", []) or []:
        if not isinstance(msg, dict):
            continue
        _content(msg.get("content"))
        for tc in msg.get("tool_calls") or []:
            if not isinstance(tc, dict):
                continue
            fn = tc.get("function") or {}
            args = fn.get("arguments", "")
            if not isinstance(args, str):
                args = json.dumps(args, sort_keys=True)
            _add_text(json.dumps(
                {"name": fn.get("name", ""), "arguments": args}, sort_keys=True,
            ))

    tools = payload.get("tools")
    if tools:
        _add_text(json.dumps(tools, sort_keys=True))

    canonical_text = b"\x00".join(text_parts)
    canonical_for_hash = b"\x00".join(hash_parts)
    survey = ContentSurvey(
        text_bytes=len(canonical_text),
        image_dims=tuple(image_dims),
        unmeasurable_images=unmeasurable_images,
    )
    return (
        survey,
        len(canonical_text),
        hashlib.sha256(canonical_for_hash).hexdigest(),
    )


def survey_and_hash_openai_responses_payload(payload: dict) -> tuple["ContentSurvey", int, str]:
    """`(survey, payload_bytes, payload_hash)` for the OpenAI Responses API
    JSON `payload` `/v1/responses` sends VERBATIM to bedrock-mantle
    (`mvp.openai_responses`) — `{"input": [...], "tools": [...], ...}`.

    `input` is a string, or a list of items each shaped either as a message
    (`{"role":..., "content": [...]}`, content parts `input_text` /
    `output_text` / `input_image` / `input_file`) or as a function-call /
    function-call-output item echoed back in multi-turn history (both are
    genuine input bytes for THIS request — same reasoning as `toolUse` in the
    Converse surveyor). `instructions` is the Responses API's system-prompt
    equivalent. `input_image.image_url` follows the same inline-data-URI-only
    measurability rule as the Chat Completions surveyor; `input_file` has no
    documented dimension-style formula to bound by, so a file part makes the
    request unmeasurable rather than silently skipped.
    """
    import base64
    import hashlib
    import json

    text_parts: list[bytes] = []
    hash_parts: list[bytes] = []
    image_dims: list[tuple[int, int]] = []
    unmeasurable_images = 0

    def _add_text(s: str) -> None:
        b = s.encode("utf-8", errors="surrogatepass")
        text_parts.append(b)
        hash_parts.append(b)

    def _add_image_url(url) -> None:
        nonlocal unmeasurable_images
        if not isinstance(url, str) or not url.startswith("data:"):
            unmeasurable_images += 1
            return
        try:
            header, b64data = url.split(",", 1)
        except ValueError:
            unmeasurable_images += 1
            return
        if ";base64" not in header:
            unmeasurable_images += 1
            return
        try:
            raw = base64.b64decode(b64data, validate=False)
        except Exception:  # noqa: BLE001
            unmeasurable_images += 1
            return
        dims = image_dimensions_from_bytes(raw)
        if dims is None:
            unmeasurable_images += 1
        else:
            image_dims.append(dims)
        hash_parts.append(raw)

    def _content_blocks(blocks) -> None:
        nonlocal unmeasurable_images
        for block in blocks:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype in ("input_text", "output_text", "text"):
                text = block.get("text")
                if isinstance(text, str):
                    _add_text(text)
            elif btype == "input_image":
                image_url = block.get("image_url")
                if image_url:
                    _add_image_url(image_url)
                else:
                    # A file_id/detail-only reference with no inline data —
                    # this gateway cannot size it, same as a remote URL.
                    unmeasurable_images += 1
            elif btype == "input_file":
                # No published token formula for an arbitrary file attachment
                # (contract 3b: use the most conservative published formula
                # or refuse — no formula is published for this shape at all).
                # Refuse rather than silently admit unbounded.
                unmeasurable_images += 1
            elif isinstance(block.get("text"), str):
                _add_text(block["text"])

    def _input_item(item) -> None:
        if isinstance(item, str):
            _add_text(item)
            return
        if not isinstance(item, dict):
            return
        content = item.get("content")
        if isinstance(content, str):
            _add_text(content)
        elif isinstance(content, list):
            _content_blocks(content)
        # function_call / function_call_output items (multi-turn tool-use
        # history): genuine input bytes for THIS request, same reasoning as
        # Converse `toolUse` — count whatever text/arguments they carry.
        for key in ("arguments", "output", "name", "call_id"):
            val = item.get(key)
            if isinstance(val, str) and item.get("type") in (
                "function_call", "function_call_output",
            ):
                _add_text(val)

    instructions = payload.get("instructions")
    if isinstance(instructions, str):
        _add_text(instructions)

    raw_input = payload.get("input")
    if isinstance(raw_input, str):
        _add_text(raw_input)
    elif isinstance(raw_input, list):
        for item in raw_input:
            _input_item(item)

    tools = payload.get("tools")
    if tools:
        _add_text(json.dumps(tools, sort_keys=True))

    canonical_text = b"\x00".join(text_parts)
    canonical_for_hash = b"\x00".join(hash_parts)
    survey = ContentSurvey(
        text_bytes=len(canonical_text),
        image_dims=tuple(image_dims),
        unmeasurable_images=unmeasurable_images,
    )
    return (
        survey,
        len(canonical_text),
        hashlib.sha256(canonical_for_hash).hexdigest(),
    )


# ---------------------------------------------------------------------------
# Budget enforcement is opt-in (CONTRACT-hard-ceiling.md section 0/7a/7b)
# ---------------------------------------------------------------------------
# The principle: the only reason to serialise requests on a shared item is to
# PROVE a limit was not exceeded. Measurement needs no such proof, so
# measurement needs no serialisation — and, sharper still, no WORK. Surveying
# the canonical payload (walking every message, parsing every image header)
# is real per-request work with no purpose for a tenant whose bound can never
# gate anything, so a ROUTE must decide whether to pay for it BEFORE running
# it, not after.
#
# Four states (contract section 0), on the DOLLAR-POOL dimension specifically
# (this module says nothing about the per-user TOKEN quota dimension, a
# separate, pre-existing mechanism — `dynamo.user_tenants.reserve` /
# `UNLIMITED_CREDIT` — this change deliberately does not touch; see the
# implementation report for why changing it is out of scope here):
#   - accounting: no pool, no measurement flag -> COMPUTE=False, GATE=False.
#   - measured:   no pool, measurement flag ON -> COMPUTE=True,  GATE=False.
#   - enforced:   a pool exists                -> COMPUTE=True,  GATE=True.
#   - shadow:     a pool exists, comparing the bound against the LEGACY
#                 estimate without changing admission -> NOT modelled by these
#                 two functions. Doing that properly needs the reservation
#                 that actually gates the pool to keep using the legacy
#                 estimate while the sound bound is computed and recorded
#                 ONLY for comparison — a parallel/dual-cost code path this
#                 change does not build (see the implementation report).
#                 `dollar_pool_bound_should_compute` returning True whenever a
#                 pool exists means today every enforced tenant is
#                 IMMEDIATELY gated the moment this ships; there is no
#                 separate "shadow first" rollout switch.
MEASURE_UNENFORCED_BOUND_ENV = "STRATOCLAVE_MEASURE_UNENFORCED_BOUND"


def _pool_row_exists(tenant_id: Optional[str]) -> bool:
    if not tenant_id:
        return False
    try:
        from dynamo.tenant_budgets import TenantBudgetsRepository, current_period

        return TenantBudgetsRepository().get(tenant_id, current_period()) is not None
    except Exception:  # noqa: BLE001 — fail toward "no pool", never toward extra work
        return False


def dollar_pool_bound_should_compute(tenant_id: Optional[str]) -> bool:
    """True iff the hard-ceiling byte-survey/bound should be computed AT ALL
    for `tenant_id`'s current request — the `measured` OR `enforced` states,
    never `accounting`. This is the gate a ROUTE must check BEFORE surveying
    the payload; checking afterward would already have paid the cost this
    exists to avoid.

    True when either:
      - a measurement flag is explicitly on (`STRATOCLAVE_MEASURE_UNENFORCED_BOUND`,
        default OFF) — the `measured` state: an operator deliberately paying
        the survey cost to collect shadow-run data (contract section 9b)
        without any pool existing yet; or
      - the tenant has an active dollar pool row for the current period (the
        `enforced` state) — see the module note above for why this function
        cannot yet also express `shadow` (pool exists, bound recorded, but
        NOT what admission actually uses).

    False for `accounting` (neither condition holds), INCLUDING on a lookup
    failure — a transient Dynamo blip must not make the request PAY the
    survey cost speculatively; the caller falls back to the pre-existing
    heuristic path exactly as if this change did not exist, which is no worse
    than before this change shipped.
    """
    if not tenant_id:
        return False
    if os.getenv(MEASURE_UNENFORCED_BOUND_ENV, "").strip().lower() in (
        "1", "true", "yes", "on",
    ):
        return True
    return _pool_row_exists(tenant_id)


def dollar_pool_bound_should_gate(tenant_id: Optional[str]) -> bool:
    """True iff a computed bound may actually REFUSE this request — the
    `enforced` state only. The `measured` state (no pool, flag on) computes
    and records the SAME bound but must never refuse on it: there is no
    dollar limit for it to protect. Deliberately a SEPARATE function from
    `dollar_pool_bound_should_compute` (which the measurement flag also makes
    True) — collapsing them back into one flag is exactly the bug an earlier
    version of this module shipped: the flag made the survey run AND made an
    unmeasurable image refuse a tenant that was never supposed to see a
    refusal from this work at all.
    """
    return _pool_row_exists(tenant_id)
