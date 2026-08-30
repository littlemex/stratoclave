"""One definition of which admission limits are enforced, closed from both sides.

This is the limits-side counterpart of `tests/test_billable_legs_registry.py`.
There, a rate column that charged money with no corresponding leg in
`BILLABLE_LEGS` was a leg the reservation bound could not see — the estimate
priced three legs while the rater charged four, and a request that wrote prompt
cache settled above what was reserved for it. Here the analogous defect is a
limit kind that is configured (an admin sets a value somewhere) but contributes
no item to the admission `TransactWriteItems` — the operator believes the limit
is enforced, and it is not, because nothing forced the declaration and the code
to move together.

`mvp.reserve_limits.RESERVE_LIMITS` is the one declaration. Two tests close it
from both directions:

  - every DECLARED limit has a builder that is actually reachable at the module
    path it claims (a stale or typo'd declaration is caught here) — this is the
    weak direction, symmetric with `test_every_billable_rate_column_has_a_leg`;
  - every RESERVE-direction transaction-item builder that AST discovery finds in
    the three limit-owning modules is named in the declaration — this is the
    strong direction, and the one that actually catches the future bug: add a
    fourth limit's builder without declaring it, and this fails; it does NOT
    require anyone to remember to update a hand-list, because there is no
    hand-list, only a name pattern + a module sweep.

Boundary of the strong direction (state plainly, do not oversell it):

  - Discovery is a SOURCE-LEVEL name-pattern sweep (AST, `ast.parse` over
    `inspect.getsource`) of the three modules named in `RESERVE_LIMITS` today
    (`dynamo.tenant_budgets`, `dynamo.user_tenants`, `mvp.routing.quota`). A
    builder added in a FOURTH module this sweep does not know about is invisible
    to it — the same limit as `BILLABLE_LEGS`, which only sees `RateSnapshot`'s
    own fields.
  - The pattern is `^(?:build_)?reserve_txn_items?$` (case-sensitive, matched
    against the function/method's bare name). This is the naming convention
    ALL THREE existing RESERVE-direction builders already follow
    (`reserve_txn_item` x2, `build_reserve_txn_items`), chosen the same way
    `_billable_rate_fields()` chooses `*_per_mtok_microusd` over hand-listing
    `input_per_mtok_microusd`, `output_per_mtok_microusd`, etc. A builder that
    enforces a limit but is named outside this convention will not be found by
    this sweep and will not fail the build — it will simply not be checked.
  - It deliberately does NOT match `reserve_commit_txn_items` (the
    pending-protocol two-phase commit point in `dynamo.tenant_budgets`, a
    DIFFERENT flow from the inline admission transaction this file is about) or
    `reserve_event_txn_item` (the ledger RESERVE event, not a limit). Both were
    checked by hand against the pattern below and confirmed excluded; a name
    that drifts close enough to the pattern by accident is exactly the kind of
    thing this test cannot see coming and a reader should re-check by hand.
  - This sweep is about the RESERVE / admission direction only. Settle, release,
    refund and reversal builders (`settle_txn_item`, `release_quota`,
    `marker_credit_back_txn_item`, ...) are out of scope by construction (the
    name pattern excludes them) and this test makes no claim about them. See
    the module docstring discussion below for whether that direction should
    also be closed.
"""
from __future__ import annotations

import ast
import importlib
import inspect
import re

import pytest

from mvp.reserve_limits import KNOWN_LIMIT_MODULES, RESERVE_LIMITS, LimitKind


#: The naming convention every RESERVE-direction transaction-item builder in
#: this codebase currently follows. See the module docstring above for why this
#: pattern and not a hand-listed set of names, and for exactly which existing
#: functions it was checked against to confirm they are (or are not) matched.
_BUILDER_NAME_RE = re.compile(r"^(?:build_)?reserve_txn_items?$")

#: Every module under `backend/mvp/` and `backend/dynamo/`, discovered from the
#: filesystem rather than listed.
#:
#: The sweep must NOT be a hand-maintained module list, and it must NOT be
#: `{k.module_name for k in RESERVE_LIMITS}`. Both look adequate and both leave the
#: hole this registry exists to close:
#:
#:   * Derived from the declarations, deleting a kind's declaration also deletes its
#:     module from the sweep, so the strong direction stops looking exactly where the
#:     now-undeclared builder still sits.
#:   * Hand-listed, a fourth limit added in a fourth module is invisible — and "a
#:     fourth limit is added" is the entire scenario this file was written for. A
#:     detector that covers only the cases that already exist catches nothing new.
#:
#: So the sweep is the whole of both packages. `KNOWN_LIMIT_MODULES` survives as a
#: non-vacuity floor: whatever the filesystem returns, those three must be in it, or
#: the discovery itself has broken.
def _swept_module_names() -> tuple[str, ...]:
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1]
    names: set[str] = set()
    for package in ("mvp", "dynamo"):
        for path in sorted((root / package).rglob("*.py")):
            if "__pycache__" in path.parts or path.name == "__init__.py":
                continue
            names.add(".".join(path.relative_to(root).with_suffix("").parts))
    missing = [m for m in KNOWN_LIMIT_MODULES if m not in names]
    assert not missing, (
        f"filesystem discovery did not find {missing}, which hold the three limit "
        f"builders that exist today — the sweep is broken, not merely empty")
    return tuple(sorted(names))


def _discover_builder_qualnames(module_name: str) -> set[str]:
    """Names of every function/method in `module_name` matching the
    RESERVE-direction naming convention, found by parsing the module's own
    source rather than importing and hand-listing its attributes — a function
    can be discovered here without anyone having named it in a list that goes
    stale. Qualified as `"function_name"` for a module-level def, or
    `"ClassName.method_name"` for a def one level inside a class body (every
    builder in these three modules is at one of those two nesting depths)."""
    module = importlib.import_module(module_name)
    tree = ast.parse(inspect.getsource(module))
    found: set[str] = set()

    class _Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self._class_stack: list[str] = []

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            self._class_stack.append(node.name)
            self.generic_visit(node)
            self._class_stack.pop()

        def _visit_func(self, node) -> None:
            if _BUILDER_NAME_RE.match(node.name):
                found.add(".".join([*self._class_stack, node.name]))
            # Do not descend into a matched builder looking for further nested
            # defs to re-parent under it; none of these builders nest a def.

        visit_FunctionDef = _visit_func
        visit_AsyncFunctionDef = _visit_func

    _Visitor().visit(tree)
    return found


# ---------------------------------------------------------------------------
# weak direction: every declared kind resolves to a real, reachable builder
# ---------------------------------------------------------------------------


def test_every_declared_limit_has_a_reachable_builder():
    """A kind declared with a stale module path or a typo'd qualname would
    already fail at import time (`LimitKind` resolves eagerly) — this test
    exists so that failure is attributed to a limit-registry problem, not read
    as some unrelated import error, and so it is asserted even if resolution
    is ever made lazy."""
    for kind in RESERVE_LIMITS:
        resolved = _resolve_for_test(kind)
        assert callable(resolved)
        assert resolved is kind.builder


def _resolve_for_test(kind: LimitKind):
    module = importlib.import_module(kind.module_name)
    obj = module
    for part in kind.builder_qualname.split("."):
        obj = getattr(obj, part)
    return obj


def test_declared_limit_names_are_unique():
    names = [k.name for k in RESERVE_LIMITS]
    assert len(names) == len(set(names))


# ---------------------------------------------------------------------------
# strong direction: every RESERVE-direction builder the sweep finds is declared
# ---------------------------------------------------------------------------


def test_every_reserve_txn_item_builder_is_declared():
    """The direction that actually catches the future bug: a fourth limit kind
    whose builder was added to one of the swept modules without a matching
    `LimitKind` in `RESERVE_LIMITS` fails HERE, not in a behavioural test that
    only samples the limits that already exist.

    Asserted per module, as a set equality, so the failure names exactly which
    module drifted and whether the drift is a builder with no declaration or a
    declaration with no builder.
    """
    declared_by_module: dict[str, set[str]] = {}
    for kind in RESERVE_LIMITS:
        declared_by_module.setdefault(kind.module_name, set()).add(kind.builder_qualname)

    for module_name in _swept_module_names():
        discovered = _discover_builder_qualnames(module_name)
        declared = declared_by_module.get(module_name, set())
        undeclared = discovered - declared
        missing = declared - discovered
        assert not undeclared, (
            f"{module_name} has a RESERVE-direction transaction-item builder "
            f"{sorted(undeclared)} with no LimitKind in RESERVE_LIMITS — a limit "
            f"that is enforced but not declared, or a name-pattern false positive "
            f"that needs a closer look either way"
        )
        assert not missing, (
            f"{module_name} declares a builder {sorted(missing)} in RESERVE_LIMITS "
            f"that the AST sweep could not find — a stale declaration, or the "
            f"builder was renamed/removed"
        )


def test_the_naming_convention_excludes_the_known_lookalikes():
    """Pins the two names the module docstring claims are checked-by-hand and
    excluded: a regression that widens the pattern to catch them would silently
    change what this file is asserting about."""
    assert not _BUILDER_NAME_RE.match("reserve_commit_txn_items")
    assert not _BUILDER_NAME_RE.match("reserve_event_txn_item")
    assert not _BUILDER_NAME_RE.match("hold_put_txn_item")
    assert not _BUILDER_NAME_RE.match("settle_txn_item")
    assert not _BUILDER_NAME_RE.match("release_txn_item")
    assert _BUILDER_NAME_RE.match("reserve_txn_item")
    assert _BUILDER_NAME_RE.match("build_reserve_txn_items")


@pytest.mark.parametrize("module_name", sorted(_swept_module_names()))
def test_sweep_covers_a_real_module(module_name):
    """Guards the sweep itself: if `RESERVE_LIMITS` ever ends up with a
    `module_name` that does not import, the closure test above would raise on
    collection with a confusing traceback; this pins the failure to the
    registry."""
    assert importlib.import_module(module_name) is not None
