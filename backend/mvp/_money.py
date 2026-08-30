"""One reservation, one owner, one terminal transition.

Every inference route reserves budget before it calls a provider and must then
end that reservation exactly once. Before this module each route ended it by
hand, and the hand-written endings disagreed: five of them returned the
reservation on any failure, which is wrong for every failure that happened
*after* the request bytes left — measured on real Bedrock, a Converse call
abandoned at a 2 s client read timeout was billed 1,493 output tokens while its
caller received nothing (`docs/MEASUREMENTS.md`, and the policy rows in
`mvp/provider_outcome.py`). One route consulted the classifier that knows this;
the other eight did not, so the same failure cost a tenant different money
depending on which wire format it arrived on.

The fix is not more branches at each site. It is that **a route may not decide**.
A route reports what it observed and the hold decides, because the decision is a
function of the observation and of policy, never of the wire format:

    claim_settle(usage)                     # the provider said what it did
    claim_unobserved(exc | status | state)  # it did not
    claim_stream_interrupted(usage, …)      # it stopped part-way
    claim_not_submitted()                   # it never left this process
    close(usage, sent=…, …)                 # a generator is closing; decide from both

Each returns an `Ending` — the write, plus how to dispatch it — or None when another
ending won. There is deliberately no second, "convenient" way to end a hold: a
synchronous wrapper that claimed and wrote in one step had to invent a return value
for the lost-claim case, and what it returned was another ending's state.

Every ending without a usage report goes through `claim_unobserved`, which is the
only place a reservation is ever returned, so a route cannot answer the liability
question for itself. A new wire format adds no liability decision. The guard in
`tests/test_money_lifecycle_discipline.py` fails the build on the shapes it can
see — a route calling `refund` / `release_pool` / `settle_reservation_and_log`
directly, outside its `_open_hold` factory — which covers the direct call and not
an alias or a reflective one.

One thing deliberately stays outside: the Layer 5 external authorize/capture API
(`mvp/billing_authorize.py`). It holds budget for a **non-LLM** action, so there
is no provider attempt to classify and no usage block to settle against — its
terminal is the caller's own capture or void. Pulling it under this object would
be a shared name over two different lifecycles, which is the failure mode this
module exists to end, not to repeat.

**Exactly one claimed ending, dispatched at most once.** (Not "one write": a
return is two — the token credit, then the pool hold.) A
streaming route has four possible endings (invoke error, mid-stream error, clean
completion, client disconnect) and any of them can be re-entered by a generator
`finally` after a cancellation, so the latch, not the call site, is what makes the
write single. But a latch alone is not enough for an async caller: the money write
is blocking boto3 and has to be offloaded, and if the claim travelled with it into
the worker thread then a cancellation arriving before that thread started would
let the `finally` claim first — the write would still happen once, but it would be
the WRONG ending (a zero settle instead of a classified abandon). So the claim is
taken synchronously, on the caller's thread, and returns the write as a callable:

    commit = hold.claim_unobserved(exc=e)
    if commit is not None:                 # we own the ending; nobody else can
        await asyncio.shield(asyncio.to_thread(commit))

`settle` / `abandon` / `end_stream` / `disconnected` are the synchronous
convenience over that pair, for callers not holding an event loop.

**What that does not promise.** The claim is taken before the write, and is not
released if the write raises. That is deliberate — a write that raised may
still have committed, and re-running it could double-charge — but it means a
settle that fails is not retried here. The hold reaper is the backstop, and it
records the exposure with the hold's own facts rather than asserting a zero nobody
observed. Likewise `_return_reservation` is two writes (token credit, then the
pool hold), so a failure between them leaves the pool hold for the reaper.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Any, Callable, Optional

from . import provider_outcome as _outcome

logger = logging.getLogger(__name__)


def run_ending(ending: Optional["Ending"]) -> Optional[str]:
    """Write a claimed ending here, on this thread, if this caller won the claim.

    The whole of what a synchronous route needs, and the reason `Hold` has no
    synchronous `settle()` / `unobserved()` of its own: those had to invent a
    return value for the lost-claim case, and what they returned was another
    ending's state.
    """
    if ending is None:
        return None
    ending.run()
    return ending.state


@dataclass(frozen=True)
class Usage:
    """What the provider reported it did.

    Any object carrying these four attributes is accepted (the streaming paths
    pass their live `_converse_types.UsageAccumulator`), so this class exists for
    the non-streaming routes that read a usage block once and for tests.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    # `None` means the provider did not report the leg at all, which is a different
    # fact from reporting zero: some models never report prompt-cache counts, and
    # whether a model caches is the largest term in its economics. The charge is the
    # same either way; the record is not. It is also the DEFAULT, because a default of
    # 0 made the record depend on which call site remembered to pass the argument —
    # the OpenAI-compatible transport never parses these legs, so every settle on
    # that route was recording a measured zero for a field nobody read. A measured
    # zero has to be stated by the code that measured it.
    cache_read_tokens: Optional[int] = None
    cache_write_tokens: Optional[int] = None


#: The four tokens a settle prices. Read off the observation by name so an
#: accumulator and a `Usage` are interchangeable.
_TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
)

#: The legs a provider may not report at all. For these, `None` is a value and not
#: a missing number: some models never report prompt-cache counts, so "the provider
#: said none" and "the provider said nothing" are different facts and only one of
#: them is a measurement (contract C8.1). Input and output are always observed on
#: any path that settles, so they are counts.
_UNREPORTABLE_FIELDS = frozenset({"cache_read_tokens", "cache_write_tokens"})


def _snapshot_tokens(observation: Any) -> dict:
    """Freeze the usage an ending will charge, preserving "not reported".

    `int(x or 0)` over every field turned an unreported leg into a measured zero
    right here — at the seam every route passes through — so the absence the
    transports carefully preserve died one call before the ledger. Reported counts
    are clamped at zero as before; an unreportable leg that is `None` stays `None`.
    """
    out = {}
    for name in _TOKEN_FIELDS:
        raw = getattr(observation, name, 0)
        if raw is None and name in _UNREPORTABLE_FIELDS:
            out[name] = None
            continue
        out[name] = int(raw or 0)
    return out


class Ending:
    """A claimed ending, and the three ways it can reach the store.

    `run()` writes on this thread, `awaited()` writes off the event loop under a
    shield, `detached()` writes without waiting.

    Claiming and writing are separate acts (see the module docstring), so what a
    claim hands back is this: the write, plus the knowledge of how to dispatch it
    without dropping it. Three callers, three needs, one object — because the two
    mistakes that were made when each call site dispatched for itself were
    `await`ing inside a closing async generator and running blocking boto3 on the
    shared event loop.

    A claim that lost returns None instead of an `Ending`, so "did I win" and "how
    do I write" are the same question asked once.
    """

    def __init__(self, hold: "Hold", write: Callable[[], Any], state: str) -> None:
        self._hold = hold
        self._write = write
        self.state = state
        self._started = False

    @property
    def started(self) -> bool:
        """Whether the write has begun. A write that raised counts as begun."""
        return self._started

    def run(self) -> Any:
        """Perform the write, at most once however many times it is dispatched.

        The guard is taken when the write STARTS, not when it is dispatched, and
        that distinction is what makes the rescue path safe. Dispatching to an
        executor can be undone — a queued work item is cancellable, so a
        cancellation can drop a write that was already claimed — which means
        `Hold.dispatch_pending` has to be able to dispatch it again. It can, because
        whichever runner starts first wins here and the other returns without
        writing. A write that raises still counts as started: it may have committed,
        and re-running it could double-charge.
        """
        if not self._claim_write():
            return None
        return self._write()

    def _claim_write(self) -> bool:
        with self._hold._lock:
            if self._started:
                return False
            self._started = True
            if self._hold._pending is self:
                self._hold._pending = None
            return True

    async def awaited(self) -> Any:
        """Write off the event loop, shielded.

        Shielded because the claim is already taken: a cancellation at this await
        would otherwise drop the write, and no later ending can replace it. The
        shield keeps the inner task alive; if the work item is cancelled before it
        starts anyway, the generator's `finally` re-dispatches it.
        """
        import asyncio

        return await asyncio.shield(asyncio.to_thread(self.run))

    def detached(self) -> None:
        """Write without waiting, for a caller that cannot await.

        The disconnect ending is reached from a closing async generator, where
        awaiting is unsafe. Fire-and-forget onto the loop's executor: the worker
        normally outlives request teardown, and what it does not survive is process
        death or an executor already shutting down — the hold reaper is the backstop
        for those, which is why it records the hold's own facts rather than a zero.
        The future is discarded, so a raised write would be GC noise; it is logged
        as a first-class, alarmable line instead.
        """
        import asyncio

        def _logged() -> None:
            try:
                self.run()
            except Exception:
                logger.exception(
                    "detached_terminal_failed",
                    extra={"route": self._hold.route, "model_id": self._hold.model_id,
                           "hold_id": self._hold.hold_id, "outcome_state": self.state},
                )
                raise

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            _logged()
            return
        try:
            loop.run_in_executor(None, _logged)
        except RuntimeError:
            # The executor is shutting down. Writing inline blocks a loop that is
            # going away anyway, which is better than dropping a claimed ending.
            logger.warning(
                "detached_terminal_ran_inline",
                extra={"route": self._hold.route, "hold_id": self._hold.hold_id},
            )
            _logged()


def hold_departure_marker(context: Any) -> Optional[Callable[[str], bool]]:
    """The marker writer for one reservation, or None when there is nothing to mark.

    Built here rather than in each route so all three get the same thing, and built
    as a closure over the context so this module still holds no storage import — the
    repository is resolved at call time, inside the closure, on a path that only runs
    when an ending keeps its reservation.

    None when the reservation has no durable hold (a request with no dollar pool has
    nothing to retain), which is why the caller distinguishes "no writer" from "write
    failed": the first is a request this does not apply to, the second is a
    deployment that thinks retention is on and is wrong.
    """
    hold_sk = getattr(context, "hold_sk", None)
    tenant_id = getattr(context, "tenant_id", None) or getattr(context, "org_id", None)
    if not hold_sk or not tenant_id:
        return None

    def _mark(state: str) -> bool:
        from dynamo.tenant_budgets import TenantBudgetsRepository

        return TenantBudgetsRepository().hold_mark_departed(
            tenant_id=str(tenant_id), sk=str(hold_sk), state=str(state))

    return _mark


class Hold:
    """A reserved amount of budget, and the only object allowed to end it.

    `settle` and `release` are injected rather than imported so that the route
    modules keep their existing late-bound test seams (a suite that patches
    `mvp.chat_completions._settle_reservation_and_log` still observes the write).
    The *policy* is not injectable: which failures may be refunded is decided
    here, once, for every route.
    """

    def __init__(
        self,
        *,
        user: Any,
        tenants_repo: Any,
        reservation: int,
        model_id: str,
        settle: Callable[..., Any],
        release: Callable[[Any], Any],
        mark_departed: Optional[Callable[[str], bool]] = None,
        requested_model: Optional[str] = None,
        request_id: Optional[str] = None,
        route: Optional[str] = None,
        on_finalized: Optional[Callable[[str, Any], None]] = None,
    ) -> None:
        self.user = user
        self.tenants_repo = tenants_repo
        self.reservation = reservation
        self.model_id = model_id
        self.requested_model = requested_model
        self.request_id = request_id
        self.route = route
        self._settle = settle
        self._release = release
        # Records on the durable hold that a provider call departed and its outcome
        # was never seen. Injected like `settle`/`release` so this module keeps
        # knowing nothing about storage. Absent means the deployment cannot record
        # it, and then a retained reservation is unreachable — which `_keep_reservation`
        # says out loud rather than degrading in silence.
        self._mark_departed = mark_departed
        self._on_finalized = on_finalized
        self._lock = threading.Lock()
        self._finalized = False
        self._outcome_state: Optional[str] = None
        self._pending: Optional[Ending] = None

    # ---------------------------------------------------------------- identity

    @property
    def hold_id(self) -> Optional[str]:
        return getattr(self.tenants_repo, "hold_id", None)

    @property
    def tenant_id(self) -> Optional[str]:
        return getattr(self.user, "org_id", None)

    @property
    def claimed(self) -> bool:
        """Whether an ending has claimed this hold.

        Named for what it means: the ending is decided and nothing may replace it.
        It does NOT mean the write has happened — `Ending.started` is that, and the
        gap between the two is what `close()` exists to cover.
        """
        with self._lock:
            return self._finalized

    @property
    def outcome_state(self) -> Optional[str]:
        """The `provider_outcome` state this hold ended in, once it has ended."""
        return self._outcome_state

    def request_metadata(self) -> dict[str, str]:
        """The correlation marker to stamp on the provider call.

        The only handle that can attribute a charge the gateway never observed,
        so stamping it is a correctness requirement — see
        `provider_outcome.attempt_request_metadata`.
        """
        return _outcome.attempt_request_metadata(self.hold_id, self.tenant_id)

    # ------------------------------------------------- claim, then write later

    def claim_settle(
        self, observation: Any = None, *, status: str = "completed"
    ) -> Optional[Ending]:
        """Claim the ending for a settle. Returns the ending, or None if we lost.

        The token counts are snapshotted BEFORE the claim, for two reasons: the
        observability hook receives the live accumulator and a hook that mutated it
        must not be able to change what is charged, and a malformed observation
        must fail before it can consume the one ending this hold has.
        """
        if observation is None:
            observation = Usage()
        tokens = _snapshot_tokens(observation)
        if not self._claim(_outcome.SETTLED_FINAL):
            return None
        self._notify(status, observation)

        def _commit() -> None:
            self._settle(
                user=self.user,
                tenants_repo=self.tenants_repo,
                reservation=self.reservation,
                actual_input_tokens=tokens["input_tokens"],
                actual_output_tokens=tokens["output_tokens"],
                model_id=self.model_id,
                context=self.tenants_repo,
                actual_cache_read_tokens=tokens["cache_read_tokens"],
                actual_cache_write_tokens=tokens["cache_write_tokens"],
                requested_model=self.requested_model,
                # Keys the UsageLogs row on the request id so the offline VSR
                # reconciliation can join it to the reserve-time decision record.
                request_id=self.request_id,
            )

        return self._remember(Ending(self, _commit, _outcome.SETTLED_FINAL))

    def claim_unobserved(
        self,
        *,
        exc: Optional[BaseException] = None,
        status_code: Optional[int] = None,
        state: Optional[str] = None,
        observation: Any = None,
        status: Optional[str] = None,
    ) -> Optional[Ending]:
        """Claim the ending for an unobserved outcome. Returns the ending, or None.

        At most one of `exc`, `status_code`, `state` may be given: they are three
        ways of saying what was seen, and passing two would make the reading
        depend on an implicit precedence. Passing none is not an error — it is the
        unknown, which is expensive by default.
        """
        given = [x for x in (exc, status_code, state) if x is not None]
        if len(given) > 1:
            raise ValueError(
                "abandon takes at most one of exc / status_code / state; two "
                "readings of one attempt would resolve by implicit precedence"
            )
        resolved = self._resolve_state(exc=exc, status_code=status_code, state=state)
        if resolved == _outcome.SETTLED_FINAL:
            raise ValueError(
                "abandon() is for an unobserved outcome; call settle(usage) when "
                "the provider reported what it did"
            )
        if resolved not in _outcome.STATES:
            # Refused before the claim rather than after it: an unknown state is a
            # programming error, and consuming the hold's one ending on it would
            # leave the reservation with no terminal at all.
            raise ValueError(f"not an outcome state: {resolved!r}")
        if not self._claim(resolved):
            return None
        self._notify(status or self._status_for(resolved), observation or Usage())

        def _commit() -> str:
            liability = _outcome.liability_for(resolved)
            enforced = _outcome.unobserved_holds_enforced()
            returnable = _outcome.refunds_immediately(resolved) or not enforced
            if returnable:
                self._return_reservation()
            else:
                self._keep_reservation(resolved)
            logger.info(
                "provider_attempt_failed",
                extra={
                    "route": self.route,
                    "outcome_state": resolved,
                    "liability": liability,
                    "liability_policy_version": _outcome.LIABILITY_POLICY_VERSION,
                    "enforced": enforced,
                    "reservation_returned": returnable,
                    "hold_id": self.hold_id,
                    "model_id": self.model_id,
                    "error_class": type(exc).__name__ if exc is not None else None,
                    "upstream_status": status_code,
                },
            )
            return resolved

        return self._remember(Ending(self, _commit, resolved))

    def claim_stream_interrupted(
        self,
        observation: Any,
        *,
        provider_responded: bool,
        sent: bool = True,
        exc: Optional[BaseException] = None,
        status: str = "midstream_error",
    ) -> Optional[Ending]:
        """Claim the ending for a stream that did not finish. Or None if we lost.

        Four facts hide behind "the stream stopped", and they carry different
        liabilities, so this is the one place that separates them:

        - **It was never sent.** A failure before the upstream request went out
          cannot have been billed. `sent` is passed rather than inferred because a
          transport's own set-up (minting a bearer token, resolving an endpoint) can
          fail inside the same `except` that catches a read error, and reading that
          as an in-flight failure would hold a reservation for a request no provider
          ever saw.

        - **Usage arrived.** A partial stream is not a free request, so what
          arrived is charged. It is a lower bound on what the provider did, and
          deliberately so: the alternative is to charge a ceiling for output the
          caller demonstrably received part of. The stream also owes a UsageLogs
          row, which a refund would not write.
        - **The provider began responding, but no usage arrived.** On Converse the
          usage block is the final event, so a stream cut before it leaves the
          accumulator at zero while the request demonstrably reached the model
          service. Settling that zero is the "free tokens" defect in stream
          clothing. `provider_responded` is passed in by the caller because the
          token counters cannot answer it: it means events came back, NOT that
          tokens were billed, and it is used only to establish that the attempt was
          submitted.
        - **Nothing came back at all.** Then the question is the one every
          non-streaming failure asks, and the classifier answers it from `exc`.

        Only the last two are affected by `STRATOCLAVE_UNOBSERVED_HOLDS`; with the
        gate off a cut stream settles its zero exactly as it did before.
        """
        if not sent:
            return self.claim_not_submitted(observation)
        observed = any(
            int(v or 0) > 0 for v in _snapshot_tokens(observation).values()
        )
        if observed:
            return self.claim_settle(observation, status=status)
        if not _outcome.unobserved_holds_enforced():
            return self.claim_settle(observation, status=status)
        if provider_responded:
            # The attempt reached the model service — we watched it answer. The
            # amount is what is unknown, so the ceiling is held rather than a zero
            # invented.
            return self.claim_unobserved(
                state=_outcome.SUBMITTED_UNSETTLED, observation=observation, status=status
            )
        return self.claim_unobserved(exc=exc, observation=observation, status=status)

    def claim_not_submitted(self, observation: Any = None) -> Optional[Ending]:
        """Claim the ending for a request that was abandoned before it was sent.

        Distinct from a zero settle, which returns the same money but records the
        request as having been served for nothing. Nothing left this process, so
        there is nothing a provider could bill.
        """
        return self.claim_unobserved(
            state=_outcome.NOT_SUBMITTED, observation=observation,
            status="client_disconnect",
        )

    def close(
        self,
        observation: Any = None,
        *,
        sent: bool,
        provider_responded: bool,
        status: str = "client_disconnect",
    ) -> None:
        """End the hold from a generator that is closing. The one call a `finally` needs.

        Reached when the consumer stopped reading, or when a cancellation landed at
        an `await` that `except Exception` does not see. Two things have to happen
        here and both were written out by hand in three places before this existed:

        1. **Complete an interrupted write.** An ending claimed just above may have
           had its dispatch cancelled — the claim is not returnable, so the write
           cannot be optional.
        2. **Otherwise choose the ending from how far the request got.** Nothing sent
           means nothing could be billed. Sent means the consumer stopped reading a
           request that may have produced output, which is the same question a cut
           stream asks.

        Reading every close as a zero settle, which is what the routes did before,
        is what made a request abandoned before its provider call look like a served
        request that cost nothing.
        """
        if self.dispatch_pending():
            return
        if self.claimed:
            return
        ending = self.claim_stream_interrupted(
            observation if observation is not None else Usage(),
            provider_responded=provider_responded,
            sent=sent,
            status=status,
        )
        if ending is not None:
            # Awaiting in a closing async generator is unsafe.
            ending.detached()

    # ---------------------------------------------------------------- internals

    @staticmethod
    def _resolve_state(
        *,
        exc: Optional[BaseException],
        status_code: Optional[int],
        state: Optional[str],
    ) -> str:
        if state is not None:
            return state
        if exc is not None:
            return _outcome.classify_exception(exc)
        if status_code is not None:
            return _outcome.classify_http_status(status_code)
        # Nothing said what was seen. The safe reading of a programming error on a
        # money path is the expensive one.
        return _outcome.SUBMITTED_UNSETTLED

    @staticmethod
    def _status_for(state: str) -> str:
        """The observability status for a hold that ended without usage.

        Only reached when the caller did not name one: the streaming skeleton
        passes the status its own ending had before this object existed, so the
        span vocabulary is unchanged for the routes that emit spans.
        """
        if state == _outcome.NOT_SUBMITTED:
            return "invoke_error"
        if state == _outcome.REJECTED_PRE_INFERENCE:
            return "upstream_rejected"
        return "unobserved_outcome"

    def _claim(self, state: str) -> bool:
        with self._lock:
            if self._finalized:
                return False
            self._finalized = True
            self._outcome_state = state
            return True

    def _remember(self, ending: Ending) -> Ending:
        with self._lock:
            self._pending = ending
        return ending

    def dispatch_pending(self) -> bool:
        """Write an ending that was claimed and whose write never started.

        The endings claim before the frame that announces them goes out, so that a
        consumer closing on that frame cannot end the hold differently. Two windows
        follow from that ordering, and this closes both: the consumer can close
        between the claim and the dispatch, and a dispatched-but-queued write can be
        cancelled before it starts. The claim is not returnable, so the write cannot
        be optional. Every generator calls this from its `finally`.

        Safe to race with the original dispatch: `Ending.run` admits one writer.
        """
        with self._lock:
            ending = self._pending
            if ending is None or ending._started:
                return False
        ending.detached()
        return True

    def _notify(self, status: str, observation: Any) -> None:
        # Observability hook, money-neutral by construction: called ONLY by the
        # winner of the claim, after the charge has been snapshotted, and
        # swallowed on any exception. A systematic hook failure would otherwise be
        # invisible, so it is logged at debug.
        if self._on_finalized is None:
            return
        try:
            self._on_finalized(status, observation)
        except BaseException:  # noqa: BLE001 — a claim is already taken: a hook
            # that raised ANYTHING, cancellation included, must not cost this hold
            # its write. Observability never affects the request; that is the whole
            # contract of this hook.
            try:
                logger.debug(
                    "on_finalized_hook_failed", exc_info=True, extra={"status": status}
                )
            except Exception:
                pass

    def _keep_reservation(self, state: str) -> None:
        """Leave the reservation with the pool, and record WHY on the hold itself.

        Not returning it is only half of retaining it. The reaper meets this hold
        later with no memory of this moment, and its default is to hand the budget
        back and record that nothing was charged — the assertion measured to be
        false for a call that departed. So the reason is written down where the
        reaper will read it.

        This is the only place that fact is established. The hold is created before
        the call, when nothing knows whether one will depart; the classifier knows
        here, and only here, and only for an outcome it could classify. A task that
        dies with no ending at all leaves nothing, which is a stated residual rather
        than a case this covers.

        A failure is logged at error, not swallowed quietly: without the marker the
        retention silently becomes the reclaim it was turned on to prevent, and a
        feature that cannot fire is worse than one that is off, because the operator
        believes it is on."""
        sk = getattr(self.tenants_repo, "hold_sk", None)
        if self._mark_departed is None or not sk:
            logger.error(
                "unobserved_hold_departure_unrecordable",
                extra={"hold_id": self.hold_id, "outcome_state": state,
                       "reason": "no marker writer" if self._mark_departed is None
                                 else "no hold sk"},
            )
            return
        try:
            if not self._mark_departed(state):
                logger.error(
                    "unobserved_hold_departure_not_recorded",
                    extra={"hold_id": self.hold_id, "outcome_state": state,
                           "reason": "hold already ended"},
                )
        except BaseException:  # noqa: BLE001 — a claim is taken; this must not raise.
            logger.error(
                "unobserved_hold_departure_write_failed", exc_info=True,
                extra={"hold_id": self.hold_id, "outcome_state": state},
            )

    def _return_reservation(self) -> None:
        """Give the tokens back and drop the pool hold.

        The token side goes first: a pool release that raised would otherwise
        leave the tenant's token credit debited for a request that never ran. The
        two writes are not atomic, so a failure between them leaves the pool hold
        for the reaper — named in the module docstring rather than implied.
        """
        self.tenants_repo.refund(
            user_id=self.user.user_id,
            tenant_id=self.tenant_id,
            tokens=self.reservation,
        )
        self._release(self.tenants_repo)
