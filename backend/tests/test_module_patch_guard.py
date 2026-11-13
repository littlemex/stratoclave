"""The guard in `tests/_module_patch_guard.py`, run against throwaway test files."""
from __future__ import annotations

import pathlib
import re
import subprocess
import sys
import textwrap

from mvp import authz
from tests._module_patch_guard import restore_and_report, snapshot

BACKEND = pathlib.Path(__file__).resolve().parents[1]
TESTS = pathlib.Path(__file__).resolve().parent


def _run(tmp_path: pathlib.Path, conftest: str, body: str) -> str:
    (tmp_path / "conftest.py").write_text(textwrap.dedent(f"""\
        import sys
        sys.path.insert(0, {str(BACKEND)!r})
        from tests._module_patch_guard import _application_functions_are_restored, monkeypatch  # noqa: F401
        import mvp.authz  # noqa: F401
    """) + textwrap.dedent(conftest))
    (tmp_path / "test_case.py").write_text(textwrap.dedent(body))
    out = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "no:xdist",
         "--rootdir", str(tmp_path), str(tmp_path)],
        cwd=tmp_path, capture_output=True, text=True)
    return out.stdout + out.stderr


def test_an_assignment_fails_that_test_and_the_next_test_sees_the_original(tmp_path):
    out = _run(tmp_path, "", """
        from mvp import authz
        ORIGINAL = authz.user_has_permission

        def test_leaks():
            authz.user_has_permission = lambda *a: True

        def test_after():
            assert authz.user_has_permission is ORIGINAL
    """)
    assert "left ['mvp.authz.user_has_permission'] replaced" in out, out
    assert re.search(r"2 passed, 1 error", out), out


def test_a_patch_from_an_autouse_fixture_through_monkeypatch_is_not_a_leak(tmp_path):
    out = _run(tmp_path, """
        import pytest

        @pytest.fixture(autouse=True)
        def _aaa_patches_first(monkeypatch):
            from mvp import authz
            monkeypatch.setattr(authz, "user_has_permission", lambda *a: True)
    """, """
        def test_uses_the_patch():
            from mvp import authz
            assert authz.user_has_permission("u", "p") is True
    """)
    assert re.search(r"1 passed", out) and "error" not in out, out


def test_the_helpers_see_a_write_through_the_function_globals():
    original = authz.user_has_permission
    taken = snapshot()
    authz.user_has_permission.__globals__["user_has_permission"] = lambda u, p: True
    try:
        assert restore_and_report(taken) == ["mvp.authz.user_has_permission"]
        assert authz.user_has_permission is original
    finally:
        authz.user_has_permission = original


def test_no_test_writes_through_a_function_globals():
    """The spelling both leaks used; `monkeypatch.setattr(module, name, ...)` reaches
    the same dictionary and is undone."""
    offenders = [f"{p.name}:{i}" for p in sorted(TESTS.glob("*.py")) if p.name != pathlib.Path(__file__).name
                 for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
                 if re.search(r"__globals__\s*\[[^\]]+\]\s*=", line)]
    assert offenders == [], offenders


def test_the_reload_exemptions_name_modules_that_exist():
    import importlib

    from tests._module_patch_guard import RELOADED_ON_PURPOSE

    for name in RELOADED_ON_PURPOSE:
        importlib.import_module(name)
