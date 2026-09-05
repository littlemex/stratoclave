"""A third-party library's DEBUG output must not reach the log sink.

`setup_logging` puts the root logger at DEBUG outside production, so a `development`
deployment inherited that for `botocore` too — and botocore at DEBUG prints every request
and response body it exchanges with DynamoDB verbatim. Whole rows from the users,
tenants, user-tenants and permissions tables were going into a log group retained for
ninety days, email addresses among them, which is the plaintext clause C12.4 forbids.

`mask_sensitive_data` cannot catch it. The address sits inside a serialised body in the
message position rather than in a field, which is precisely the blind spot that let the
audit writer violate the same clause.

Two properties are pinned here, and the second is the one that closes the class rather
than the instance:

1. the named third-party loggers are not at DEBUG after `setup_logging`, in either
   environment;
2. a body-shaped record emitted by one of them does not reach a handler — asserted by
   emitting it and observing that it is filtered, not by reading a level number, because
   the level is the mechanism and the absence from the sink is the property.
"""
from __future__ import annotations

import logging

import pytest

from core.logging import (
    NOISY_THIRD_PARTY_LOGGERS,
    THIRD_PARTY_LOG_FLOOR,
    setup_logging,
)

#: A redacted stand-in for what botocore actually printed: a DynamoDB response body with
#: an address inside it. The address here is fictional and the point is its SHAPE.
_BODY_SHAPED_RECORD = (
    'Response body:\nb\'{"Count":1,"Items":[{"account_id":{"S":"000000000000"},'
    '"email":{"S":"someone@example.com"},"invited_role":{"S":"user"}}]}\''
)


@pytest.fixture(autouse=True)
def _restore_logger_levels():
    """Levels are process-global, so put every touched logger back afterwards."""
    saved = {name: logging.getLogger(name).level for name in NOISY_THIRD_PARTY_LOGGERS}
    saved["__root__"] = logging.getLogger().level
    yield
    for name, level in saved.items():
        if name == "__root__":
            logging.getLogger().setLevel(level)
        else:
            logging.getLogger(name).setLevel(level)


@pytest.mark.parametrize("environment", ["development", "production", "staging"])
@pytest.mark.parametrize("name", NOISY_THIRD_PARTY_LOGGERS)
def test_third_party_logger_is_not_at_debug(environment, name):
    """Every environment, not only production.

    `staging` is included deliberately: `setup_logging` branches on equality with
    "production", so any other value takes the development path. A deployment that calls
    itself staging gets the DEBUG root, and would have leaked exactly as development did.
    """
    logging.getLogger(name).setLevel(logging.NOTSET)
    setup_logging(environment=environment)
    effective = logging.getLogger(name).getEffectiveLevel()
    assert effective >= THIRD_PARTY_LOG_FLOOR, (
        f"{name} is at {logging.getLevelName(effective)} under environment "
        f"{environment!r}; at DEBUG it prints provider request and response bodies "
        f"verbatim into the log sink"
    )


@pytest.mark.parametrize("environment", ["development", "production"])
def test_a_body_shaped_debug_record_does_not_reach_a_handler(environment, caplog):
    """The property, not the mechanism.

    Asserting a level number would pass against a future refactor that set the level and
    then added a handler bypassing it. This emits the record botocore actually emitted and
    requires that nothing receives it.
    """
    setup_logging(environment=environment)
    with caplog.at_level(logging.DEBUG):
        logging.getLogger("botocore.endpoint").debug(_BODY_SHAPED_RECORD)
    leaked = [r for r in caplog.records if "someone@example.com" in r.getMessage()]
    assert not leaked, (
        "a botocore DEBUG record carrying a serialised body reached a handler; it would "
        "be written to the log group with the address in it"
    )


def test_the_floor_touches_only_the_named_loggers():
    """The fix must not silence what the development default exists for.

    Flooring the third-party loggers is only correct if this gateway's own DEBUG still
    comes through; otherwise dropping the root to INFO would have been equivalent, and it
    is not — it would cost the local debugging the development default exists for.

    Asserted as "which loggers had their level changed" rather than by reading this
    gateway's effective level, because under pytest the root logger belongs to pytest:
    `basicConfig` is a no-op once handlers exist, so an assertion about the root would be
    measuring the harness rather than `setup_logging`.
    """
    from core import logging as core_logging

    probe = "mvp.chat_completions"
    logging.getLogger(probe).setLevel(logging.NOTSET)
    before = {
        name: logging.getLogger(name).level
        for name in (*NOISY_THIRD_PARTY_LOGGERS, probe)
    }

    core_logging._floor_third_party_loggers()

    after = {name: logging.getLogger(name).level for name in before}
    changed = {n for n in before if before[n] != after[n]}
    assert probe not in changed, (
        f"{probe} had its level changed by the third-party floor, which is outside what "
        f"the floor is allowed to touch"
    )
    assert changed <= set(NOISY_THIRD_PARTY_LOGGERS), (
        f"the floor changed loggers outside its declared set: "
        f"{changed - set(NOISY_THIRD_PARTY_LOGGERS)}"
    )


def test_an_operator_can_still_raise_a_third_party_logger_deliberately():
    """The floor is a default, not a lock.

    Debugging a provider call sometimes genuinely needs the trace. Setting the level after
    `setup_logging` has to keep working, or the fix trades one problem for another.
    """
    setup_logging(environment="development")
    logging.getLogger("botocore").setLevel(logging.DEBUG)
    assert logging.getLogger("botocore").getEffectiveLevel() == logging.DEBUG
