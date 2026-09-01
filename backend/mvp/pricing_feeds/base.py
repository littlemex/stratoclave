"""What a price feed is, and what it is allowed to do to the charging path.

A *feed* answers one question for a set of model ids: "what does the provider
currently publish for these?". It returns parsed cards; it does not decide pricing
keys, does not merge, and does not know about the ledger. The composite source owns
all of that, so a new provider is a new feed and nothing else.

Two rules hold for every feed, and both exist because a feed talks to a remote API
that can change shape without notice:

- **A feed never raises out of `fetch()`.** Network errors, permission errors, a
  renamed field, an unparseable price: all of them are recorded in the result and
  reported as *absence*. The layers underneath a feed (last-known-good snapshot,
  then the bundled floor) are what keep charging correct, and they can only do that
  if absence is expressible.
- **A feed reports what it could not read.** `unparsed` and `errors` are not
  decoration: a grammar change shows up as a rise in `unparsed` with no drop in
  reachability, which is the only signal that distinguishes "AWS renamed a
  dimension" from "this model has no cache pricing".
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Callable, Optional, Protocol, runtime_checkable

from .dimensions import RateDimension

# model id -> {(region_code_or_None, RateDimension): USD per million tokens}
Card = dict[tuple[Optional[str], RateDimension], Decimal]

# The deployment's own region. Read by `agreement.py` (where the API is
# region-independent, so this is purely an operational choice of which endpoint to
# call) and by `price_list.py` (as the fallback when no candidate regions were
# passed in). Named once, here, rather than as a string literal in each module, so
# the two cannot drift into reading two different environment variables under a
# comment that claims they read one.
STRATOCLAVE_REGION_ENV = "STRATOCLAVE_REGION"

# A client built with a zero or negative timeout either raises at construction
# (botocore requires a positive `connect_timeout`) or gives up before the socket
# opens. The between-call check (`out_of_time`) is what actually stops a spent pass
# from starting a new call, so this floor only has to keep a client from being built
# with a timeout botocore will refuse.
MIN_CLIENT_TIMEOUT_SECONDS = 1.0
# Used only when a feed is asked with no deadline at all — a fetch on the request
# path always carries one; this is for a direct call (a script, a test) that does
# not.
DEFAULT_CLIENT_TIMEOUT_SECONDS = 30.0


@dataclass(frozen=True)
class FeedRequest:
    """Everything a feed needs to answer, in one value.

    One object rather than a growing argument list, so a feed that needs another piece of
    deployment context adds a field here instead of the composite learning which feed
    wants what.

    - `model_ids`: the ids the PRICE APIs know the models by, already resolved from the
      registry (an inference-profile prefix stripped, or a declared billing id used).
    - `regions`: the regions this deployment can dispatch to, so a feed with a
      per-region catalogue does not download the world.
    - `deadline`: an instant to stop at, comparable against `clock()`, or `None` for no
      bound. An instant rather than a duration so it survives being passed down a chain.
    - `clock`: a zero-argument callable returning a float on the **same time source**
      `deadline` was computed on, defaulting to `time.monotonic`. `deadline` and `clock`
      must come from one source — comparing a monotonic deadline against `time.time()`
      (or the reverse) does not raise, it just answers wrong, which is worse: a monotonic
      deadline read against wall time can be arbitrarily far from now in either
      direction, so `out_of_time()` can come back true on the first check of a fresh
      pass, or never come back true at all.
    """

    model_ids: frozenset[str]
    regions: frozenset[str] = frozenset()
    deadline: Optional[float] = None
    clock: Callable[[], float] = time.monotonic

    def out_of_time(self) -> bool:
        return self.deadline is not None and self.clock() >= self.deadline

    def remaining_seconds(self) -> float:
        """Seconds left in the budget, for sizing a client's own connect/read timeout.

        Floored at `MIN_CLIENT_TIMEOUT_SECONDS` rather than left to reach zero or go
        negative, and read on `clock` rather than `time.time()` for the same reason
        `out_of_time()` is: the deadline is only meaningful against the source it was
        computed on.
        """
        if self.deadline is None:
            return DEFAULT_CLIENT_TIMEOUT_SECONDS
        return max(self.deadline - self.clock(), MIN_CLIENT_TIMEOUT_SECONDS)


@dataclass
class FeedResult:
    """One feed's answer for the models it was asked about."""

    cards: dict[str, Card] = field(default_factory=dict)
    # Model ids this feed established are not its business (for the agreement feed:
    # the API answered "agreement not supported", which is how AWS says "this model
    # is billed by AWS, not through Marketplace"; for the Price List feed: a full,
    # untruncated scan that never turned up a row for the model). Distinct from a
    # model the feed merely failed to read, because the composite uses it to decide
    # whether a truncated feed still owes an answer for that model. Mutually
    # exclusive with `incomplete_models` for the same reason: a model this feed has
    # established as not its own is never also one it still owes an answer for.
    out_of_scope: set[str] = field(default_factory=set)
    # Names the feed recognised as prices but could not map to a RateDimension. Counted per
    # feed rather than logged per name: a renamed grammar produces thousands.
    unparsed: int = 0
    # A bounded sample of the unparsed names, so an operator can see the new shape
    # without turning on debug logging.
    unparsed_samples: list[str] = field(default_factory=list)
    # Model ids the provider refused to describe for THIS account rather than for a
    # data reason. Kept apart from `errors` because the two demand different actions
    # and only one of them means something is broken: an authorization refusal is a
    # permission to grant, while an unreadable response is a parser or an outage. It
    # also keeps the live grammar check honest — a model the account may not read is
    # not evidence that a rate name changed shape.
    not_authorized: set[str] = field(default_factory=set)
    # The feed stopped before it had asked about everything — a spent deadline, a page
    # cap. Set by the feed rather than inferred by the caller, because only the feed knows
    # whether what it returned is all there was.
    truncated: bool = False
    # Models whose answer this feed knows may be partial: a region it could not read, a
    # page cap, a spent deadline. Per model rather than one flag for the pass, because a
    # pagination limit in an offer that prices nothing we asked about must not freeze the
    # price of a model another feed read completely — that turns a safety clamp into a
    # permanent over-charge.
    incomplete_models: set[str] = field(default_factory=set)
    # Why a SPECIFIC model produced nothing, keyed by model id. Separate from `errors`
    # because that list is capped — a burst of failures would push the tenth one out and
    # leave a model reading as "no feed priced this", which is the least useful sentence a
    # report can carry. Bounded by the number of models asked about, which the registry
    # bounds.
    model_errors: dict[str, str] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    # How many errors did not fit in the list above. A capped list that does not say it
    # was capped is a report that lies by omission.
    errors_dropped: int = 0

    def note_unparsed(self, name: str, *, sample_limit: int = 5) -> None:
        self.unparsed += 1
        if len(self.unparsed_samples) < sample_limit:
            self.unparsed_samples.append(str(name)[:120])

    def note_error(self, message: str, *, limit: int = 10) -> None:
        if len(self.errors) < limit:
            self.errors.append(message[:300])
        else:
            self.errors_dropped += 1

    def note_model_error(self, model_id: str, message: str) -> None:
        """Record why one model produced nothing, and keep it in `errors` too while there
        is room."""
        self.model_errors[model_id] = message[:300]
        self.note_error(f"{model_id}: {message}")


@runtime_checkable
class Feed(Protocol):
    """Structural, so a deployment can register a feed without importing a base
    class — the same reason `PriceSource` in `mvp.price_sources` is structural."""

    name: str

    def fetch(self, request: FeedRequest) -> FeedResult:
        """Prices for `request.model_ids`, stopping politely at `request.deadline`.

        Honouring the deadline is part of the contract rather than a courtesy: the first
        fetch after a cold start with no snapshot happens on the request path, and an
        unbounded pass there is a stall a caller pays for.
        """
        ...
