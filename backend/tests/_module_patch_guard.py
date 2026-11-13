"""Fail a test that leaves a function of the application rebound.

A test that replaces a module-level function by assignment (`mod.f = stub`, or a
write through `f.__globals__`) instead of through `monkeypatch` leaves every later
test in the same xdist worker running against the stub. Which test that reaches
depends on scheduling, so it shows as an intermittent failure somewhere else.

Watched: every module-level callable of the application's packages that are loaded
when a test starts. A name added during a test, a rebound non-callable and a patched
class attribute are not seen. Reloading an application module inside a test rebinds
its callables and is reported, unless the module is listed in `RELOADED_ON_PURPOSE`.
"""
from __future__ import annotations

import sys
from types import ModuleType
from typing import Iterator

import pytest

PACKAGES = ("mvp", "dynamo", "core", "billing")

#: Modules a test reloads on purpose, with the reason. A reload rebinds every function,
#: which this guard would report.
RELOADED_ON_PURPOSE: dict[str, str] = {
    "core.rate_limit": "test_rate_limit_xff.py reloads it under each RATE_LIMIT_TRUSTED_HOPS value",
}

Snapshot = list[tuple[ModuleType, dict[str, object]]]


def _watched() -> list[ModuleType]:
    return [m for name, m in list(sys.modules.items())
            if isinstance(m, ModuleType) and name.split(".", 1)[0] in PACKAGES
            and name not in RELOADED_ON_PURPOSE]


def snapshot() -> Snapshot:
    return [(m, {k: v for k, v in vars(m).items() if callable(v) and not k.startswith("__")})
            for m in _watched()]


def restore_and_report(taken: Snapshot) -> list[str]:
    """Put back every callable rebound since `taken`; return their names."""
    leaked = []
    for module, callables in taken:
        current = vars(module)
        for name, original in callables.items():
            if current.get(name) is not original:
                leaked.append(f"{module.__name__}.{name}")
                current[name] = original
    return leaked


@pytest.fixture(autouse=True)
def _application_functions_are_restored() -> Iterator[None]:
    """Its teardown runs after `monkeypatch` has undone its patches, because the
    `monkeypatch` override below depends on this fixture."""
    taken = snapshot()
    yield
    leaked = restore_and_report(taken)
    if leaked:
        pytest.fail(f"left {leaked} replaced; patch it with monkeypatch so it is restored",
                    pytrace=False)


@pytest.fixture
def monkeypatch(_application_functions_are_restored, monkeypatch):
    """The built-in `monkeypatch`, ordered inside the guard so its undo runs first
    and a patch made through it is never reported as a leak, even from an autouse
    fixture that is set up before the guard."""
    return monkeypatch
