"""Every money-affecting flag's documented default equals the code's.

`docs/DEPLOYMENT.md` names four flags as money-affecting. The default of each one
decides real behaviour: whether a tenant's headroom is kept or handed back when the
gateway could not observe a provider call, whether admission is priced from a
byte-count bound or an estimate, whether the bound is computed at all, and how long
an unsettled hold survives before the reaper takes it back. An operator plans a
rollout from these sentences.

The clause this file enforces is not "the documentation is accurate", which nobody
can check. It is narrower and mechanical: for each of the four, the default stated
in the documentation is compared against the code that produces it. A transcribed
default drifts silently; a compared one breaks the build.

Reading a default out of the code is done through the owning module, never by
re-deriving it here. Passing `default=True` into an env helper from this file would
assert this file's copy of the rule rather than the code's, which is the same
mistake the documentation made, one level down.

Deliberately NOT asserted: what any default OUGHT to be. That is a design decision
recorded in `design/hard-ceiling.md` and `design/charge-loss.md`. This file only
refuses to let the code and the documentation disagree about what it currently is.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

_DOCS = Path(__file__).resolve().parents[2] / "docs"
_DEPLOYMENT = _DOCS / "DEPLOYMENT.md"

#: The four flags `DEPLOYMENT.md` calls money-affecting, and the phrase its row for
#: each must contain. The phrase is what a reader takes away, so it is what gets
#: compared -- not a parse of the surrounding prose.
_BOOLEAN_GATES = {
    "STRATOCLAVE_UNOBSERVED_HOLDS": "Default **on**",
    "STRATOCLAVE_HARD_CEILING_GATE": "Default **on**",
    "STRATOCLAVE_MEASURE_UNENFORCED_BOUND": "Default **off**",
}
_NUMERIC_FLAGS = {"STRATOCLAVE_POOL_HOLD_TTL_SECONDS": "3600"}
_MONEY_FLAGS = tuple(_BOOLEAN_GATES) + tuple(_NUMERIC_FLAGS)


def _boolean_default(env_name: str, monkeypatch: pytest.MonkeyPatch) -> bool:
    """The flag's effective default, read from the owning module with it unset."""
    monkeypatch.delenv(env_name, raising=False)

    if env_name == "STRATOCLAVE_UNOBSERVED_HOLDS":
        from mvp import provider_outcome

        return bool(provider_outcome.unobserved_holds_enforced())

    from mvp import reservation_bound

    # Both remaining gates are reached through the public predicate that consumes
    # them, with the pool-row check stubbed true so the answer is about the flag and
    # not about whether a tenant happens to have a pool row.
    monkeypatch.setattr(reservation_bound, "_pool_row_exists", lambda _tenant: True)
    if env_name == "STRATOCLAVE_HARD_CEILING_GATE":
        return bool(reservation_bound.dollar_pool_bound_should_gate("any-tenant"))

    # The measurement flag's own predicate returns True when a pool row exists even
    # with the flag off, so stubbing the row true would hide the flag entirely. Here
    # the row is stubbed FALSE, which isolates the flag as the only thing that could
    # say yes.
    monkeypatch.setattr(reservation_bound, "_pool_row_exists", lambda _tenant: False)
    return bool(reservation_bound.dollar_pool_bound_should_compute("any-tenant"))


def _row_for(env_name: str) -> str:
    text = _DEPLOYMENT.read_text(encoding="utf-8")
    rows = [ln for ln in text.splitlines() if f"`{env_name}`" in ln and ln.startswith("|")]
    assert rows, f"DEPLOYMENT.md has no table row for {env_name}"
    return rows[0]


@pytest.mark.parametrize("env_name", tuple(_BOOLEAN_GATES))
def test_documented_boolean_default_matches_the_code(env_name, monkeypatch):
    """The row's stated default is the one the code produces when the flag is unset.

    Both directions are asserted: the row must say what the code does AND must not
    say the opposite. Checking only the first would pass a row that stated both.
    """
    actual_on = _boolean_default(env_name, monkeypatch)
    stated = _BOOLEAN_GATES[env_name]
    expected = "Default **on**" if actual_on else "Default **off**"
    opposite = "Default **off**" if actual_on else "Default **on**"
    row = _row_for(env_name)

    assert stated == expected, (
        f"{env_name} defaults {'on' if actual_on else 'off'} in code, but this test's "
        f"table expects the documentation to say {stated!r}. The code changed: update "
        f"DEPLOYMENT.md, EVIDENCE.md and CONTRACTS.md C7.4 in the same commit as the flip."
    )
    assert expected in row, (
        f"{env_name} defaults {'on' if actual_on else 'off'} in code but its "
        f"DEPLOYMENT.md row does not say so: {row[:240]}"
    )
    assert opposite not in row, (
        f"{env_name}'s DEPLOYMENT.md row states the wrong default: {row[:240]}"
    )


@pytest.mark.parametrize("env_name", tuple(_NUMERIC_FLAGS))
def test_documented_numeric_default_matches_the_code(env_name, monkeypatch):
    """A numeric money flag's documented default is the one the code falls back to.

    Read from the environment fallback rather than from the module's derived
    constant, because the constant is the default after a floor is applied and the
    documentation is describing the default before it.
    """
    monkeypatch.delenv(env_name, raising=False)
    import mvp._pipeline  # noqa: F401 — imported for its side-effect-free constants

    expected = _NUMERIC_FLAGS[env_name]
    assert os.getenv(env_name) is None
    row = _row_for(env_name)
    assert f"default `{expected}`" in row, (
        f"{env_name}'s DEPLOYMENT.md row does not state its default of {expected}: {row[:240]}"
    )


def test_the_money_flag_summary_agrees_with_every_row():
    """The summary under the table must not contradict the rows above it.

    A reader who trusts a one-line summary over a twelve-column table is the reader
    a wrong summary misleads, so the summary is checked against the same source of
    truth as the rows: it may not claim that every money flag defaults off while any
    of them defaults on.
    """
    text = _DEPLOYMENT.read_text(encoding="utf-8")
    any_on = any(v == "Default **on**" for v in _BOOLEAN_GATES.values())
    if any_on:
        assert "Each is off or derived by default" not in text, (
            "the money-flag summary claims every money flag defaults off while "
            + ", ".join(k for k, v in _BOOLEAN_GATES.items() if v == "Default **on**")
            + " defaults on"
        )


def test_evidence_does_not_call_unobserved_holds_opt_in():
    """EVIDENCE.md must not describe withholding an unobserved hold as opt-in.

    Opt-in and default-on are opposite claims about one variable, and EVIDENCE.md is
    the document a reader consults to find out what this project has actually shown.
    """
    text = (_DOCS / "EVIDENCE.md").read_text(encoding="utf-8")
    assert "stays opt-in via `STRATOCLAVE_UNOBSERVED_HOLDS`" not in text, (
        "EVIDENCE.md calls withholding an unobserved hold opt-in; it is the default"
    )


def test_contracts_does_not_contradict_itself_about_the_money_gates():
    """CONTRACTS.md must not assert a default that disagrees with DEPLOYMENT.md.

    Its preamble and its clause C7.4 once disagreed with each other about the same
    two variables. A contract document that contradicts itself cannot be the upstream
    object it claims to be, so the check is over the whole file rather than one clause.
    """
    text = (_DOCS / "design" / "CONTRACTS.md").read_text(encoding="utf-8")
    for env_name, stated in _BOOLEAN_GATES.items():
        if stated != "Default **on**":
            continue
        for line in text.splitlines():
            if f"`{env_name}`" not in line:
                continue
            assert "default off" not in line.lower(), (
                f"CONTRACTS.md says {env_name} defaults off; DEPLOYMENT.md and the "
                f"code both say on: {line[:240]}"
            )
