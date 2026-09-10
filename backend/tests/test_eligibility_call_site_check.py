"""C5's second half -- the call-site check.

"A truth-table test on a helper passes even when reserve, pin and both
listings each carry their own copy, which is exactly what C5 exists to
prevent. **Tests call it**; a test reimplementing the scan passes with the
production function deleted."

Named callable under test, verbatim from the handoff:

    mvp.registry_checks.check_eligibility_has_one_implementation()
        -> None, or raises ValueError naming the offending files.

This file does NOT reimplement the scan (no local regex over `backend/mvp/`
written here) -- every assertion below calls the production function
directly. Non-vacuity is proven by TEMPORARILY dropping a real file
containing one of the three refusal-code literals into the real `backend/
mvp/` tree (outside `eligibility.py`) and asserting the check catches it,
then removing the file and asserting the check is clean again -- the file
is created and destroyed inside a single test via `try`/`finally` so this
suite never leaves it behind, including on failure.
"""
from __future__ import annotations

from pathlib import Path

import pytest

_MVP_DIR = Path(__file__).resolve().parent.parent / "mvp"

# The exact three literals named in the handoff's precedence list.
_REFUSAL_CODES = ("model_not_allowed", "model_not_entitled", "scope_not_allowed")


def _check():
    from mvp.registry_checks import check_eligibility_has_one_implementation

    return check_eligibility_has_one_implementation()


def test_the_check_is_a_callable_this_module_exports():
    # Import-time proof that the production function exists at the named
    # location -- collection itself fails loudly (not a silent skip) while
    # `mvp/eligibility.py` and this callable are still unimplemented, which
    # is the expected, documented state before the code author's PR3 lands.
    from mvp.registry_checks import check_eligibility_has_one_implementation

    assert callable(check_eligibility_has_one_implementation)


def test_clean_tree_passes():
    assert _check() is None


@pytest.mark.parametrize("code", _REFUSAL_CODES)
def test_a_stray_literal_outside_eligibility_py_is_caught(code):
    """Non-vacuity: a real file, in the real scanned tree, carrying one of
    the three refusal-code literals OUTSIDE `mvp/eligibility.py`, must make
    the check fail -- for EACH of the three codes independently, since a
    scan that only greps for one of them would silently miss the other two
    reimplemented elsewhere."""
    leak_path = _MVP_DIR / "_test_temp_eligibility_callsite_leak.py"
    assert not leak_path.exists(), (
        f"{leak_path} already exists -- a prior run of this test did not "
        f"clean up; remove it by hand before re-running"
    )
    leak_path.write_text(
        f'# test-injected leak, deleted by the test that wrote it\n'
        f'_LEAKED_REFUSAL_CODE = "{code}"\n'
    )
    try:
        with pytest.raises(ValueError) as ei:
            _check()
        message = str(ei.value)
        assert "_test_temp_eligibility_callsite_leak" in message or code in message, (
            f"the raised ValueError must name the offending file or the "
            f"leaked code so an operator can find it: {message!r}"
        )
    finally:
        leak_path.unlink()

    # And clean again immediately after removal -- the check is state-free
    # across calls, not something that latches once it has ever failed.
    assert _check() is None


def test_the_literals_are_permitted_inside_eligibility_py_itself():
    """Non-vacuous companion to the leak test: `mvp/eligibility.py` is where
    all three codes are SUPPOSED to live (returned from `refusal_for`), so
    the check must not flag its own module. If `mvp/eligibility.py` does not
    exist yet, this test is not meaningful and is skipped rather than made
    to fail on an unrelated ImportError/FileNotFoundError -- it exists to
    catch the check being over-broad, not to assert the module's existence
    (that is `test_eligibility_predicate.py`'s job)."""
    eligibility_path = _MVP_DIR / "eligibility.py"
    if not eligibility_path.exists():
        pytest.skip("mvp/eligibility.py does not exist yet in this worktree")
    source = eligibility_path.read_text()
    assert any(f'"{code}"' in source or f"'{code}'" in source for code in _REFUSAL_CODES), (
        "sanity check: mvp/eligibility.py exists but none of the three "
        "refusal-code literals appear in it -- test fixture assumption "
        "broke, not the production check"
    )
    assert _check() is None
