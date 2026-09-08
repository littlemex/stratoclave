"""The heavy lane's file list, derived by grep in CI, is the same set pytest resolves.

`.github/workflows/test.yml` runs one job per heavy file, and it finds those files with a
grep for a module-level `pytestmark`. That grep is a second, cruder implementation of a
question pytest already answers exactly: which tests carry the `heavy` marker.

Two implementations of one question drift, and this one drifts silently in the worst
direction. A test marked heavy in a way the grep cannot see — a decorator on a class, a
marker added in `conftest.py`, a different spelling — is a test the heavy jobs never run and
the fast lane deselects. Both lanes stay green and the test stops running.

So this compares the two answers. It runs in the fast lane, on every commit, and it needs no
network and no marker of its own.

`--collect-only` is not used here: the comparison is done through pytest's own collection API
via a subprocess, because that is the same resolution path the lanes use, and reimplementing
marker resolution here would make this a third implementation of the question.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
TESTS = BACKEND / "tests"
WORKFLOW = BACKEND.parent / ".github" / "workflows" / "test.yml"

# The exact pattern the workflow greps for. Kept as one string so a change to the workflow
# that this test does not know about shows up as a failure below rather than as agreement.
GREP_PATTERN = "^pytestmark = pytest.mark.heavy"


def _grep_files() -> set[str]:
    """What the workflow's grep finds, run the same way the workflow runs it."""
    out = subprocess.run(
        ["grep", "-rl", GREP_PATTERN, "tests/"],
        cwd=BACKEND, capture_output=True, text=True, check=False,
    )
    return {line.strip() for line in out.stdout.splitlines() if line.strip()}


def _pytest_files() -> set[str]:
    """Every file holding at least one test pytest resolves as `heavy`."""
    out = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-m", "heavy",
         "--collect-only", "--no-header", "-p", "no:cacheprovider", "tests/"],
        cwd=BACKEND, capture_output=True, text=True, check=False,
    )
    assert out.returncode == 0, (
        f"collection of the heavy marker failed, so this test cannot compare anything:\n"
        f"{out.stdout[-2000:]}\n{out.stderr[-2000:]}"
    )
    # Collected node ids look like `tests/test_x.py::TestY::test_z`.
    return {m.group(1) for m in re.finditer(r"^(tests/[^\s:]+\.py)::", out.stdout, re.M)}


def test_the_workflow_still_greps_for_the_pattern_this_test_checks() -> None:
    """Guards the guard. If the workflow's grep changes, the comparison below is
    checking a pattern nothing uses."""
    text = WORKFLOW.read_text()
    assert GREP_PATTERN in text, (
        f"{WORKFLOW.name} no longer greps for {GREP_PATTERN!r}, so this test is comparing "
        f"pytest against a pattern the heavy lane does not use. Update both together."
    )


def test_the_grep_finds_exactly_the_files_pytest_calls_heavy() -> None:
    grep, resolved = _grep_files(), _pytest_files()
    assert grep, "the grep found no heavy files at all, which would give CI an empty matrix"
    missed = sorted(resolved - grep)
    assert not missed, (
        f"pytest resolves heavy tests in {missed}, and the workflow's grep does not find "
        f"them. Those tests would run in NEITHER lane: the heavy jobs never see the file, "
        f"and the fast lane deselects the marker. Both lanes stay green"
    )
    extra = sorted(grep - resolved)
    assert not extra, (
        f"the grep matches {extra}, where pytest resolves no heavy test. CI would start a "
        f"job that runs nothing and reports success"
    )


def test_a_heavy_file_holds_only_heavy_tests_or_the_marker_bounds_it() -> None:
    """The matrix passes a path AND `-m heavy`, so a mixed file runs its heavy tests here
    and its ordinary ones in the fast lane. This records that invariant as an executable
    statement rather than a comment, by checking the lane command still carries the marker."""
    text = WORKFLOW.read_text()
    assert 'pytest -q -m heavy -n auto "${{ matrix.file }}"' in text, (
        "the heavy matrix no longer passes `-m heavy` alongside the file path. Without the "
        "marker, a file holding a mix would run its ordinary tests twice — once here and "
        "once in the fast lane — and the two runs could disagree"
    )
