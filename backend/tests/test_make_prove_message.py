"""What `make prove` and `make verify-rating` print after their tests.

Both targets once described the missing cache-write leg as a live, pinned defect
after it had been fixed and its strict xfail removed, because the status was written
by hand in two places and nothing compared it with the file that decides it.

The fix keeps the status in one place. `make prove` prints only what its own proofs
establish and points at `make verify-rating`; `make verify-rating` runs the files it
talks about with `-rxX`, so pytest itself reports any xfail. These tests check the two
things that are decidable from the files: that `prove` carries no status of its own,
and that `verify-rating` mentions an expected xfail exactly when the differential file
pins one.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
MAKEFILE = REPO / "Makefile"
DIFFERENTIAL = REPO / "backend" / "tests" / "test_rating_differential.py"

pytestmark = pytest.mark.skipif(not MAKEFILE.is_file(), reason="needs the repository-root Makefile")


def _recipe(name: str) -> str:
    text = MAKEFILE.read_text(encoding="utf-8")
    m = re.search(rf"^{re.escape(name)}:.*\n((?:(?:\t.*)?\n)+?)(?=^\S|\Z)", text, flags=re.M)
    assert m, f"Makefile has no `{name}:` target"
    return m.group(1)


def _echoed(name: str) -> str:
    message = " ".join(re.findall(r'@echo "(.*)"', _recipe(name)))
    assert message, f"`make {name}` prints nothing these tests could read"
    return message


def _pins_a_strict_xfail(path: Path) -> bool:
    """True if code in the file (not a docstring) calls `xfail(..., strict=True)`:
    a decorator, `pytest.param(marks=...)`, or a direct call."""
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name == "xfail" and any(
            k.arg == "strict" and isinstance(k.value, ast.Constant) and k.value.value is True
            for k in node.keywords
        ):
            return True
    return False


def test_prove_points_at_verify_rating_for_the_premise():
    """`prove` defers the premise to the target that checks it, so its own text has
    no status that can go stale."""
    message = _echoed("prove")
    assert "make verify-rating" in message
    assert not re.search(r"\bxfail\b|\bdefect\b|\btoday\b", message, flags=re.I), (
        "`make prove` describes the premise's current state; keep that in "
        "`make verify-rating`, which runs the file that decides it")


def test_verify_rating_runs_what_it_reports_on():
    recipe = _recipe("verify-rating")
    for f in ("test_rating_differential.py", "test_billable_legs_registry.py",
              "test_make_prove_message.py"):
        assert f"tests/{f}" in recipe, f"`make verify-rating` talks about {f} but does not run it"
    assert "-rxX" in recipe, "`make verify-rating` must let pytest report xfails itself"


def test_verify_rating_mentions_an_xfail_exactly_when_one_is_pinned():
    mentions = "xfail" in _echoed("verify-rating").lower()
    pinned = _pins_a_strict_xfail(DIFFERENTIAL)
    assert mentions == pinned, (
        f"`make verify-rating` {'mentions' if mentions else 'does not mention'} an "
        f"expected xfail, but test_rating_differential.py {'pins' if pinned else 'pins no'} "
        "strict xfail; update the message in the Makefile")
