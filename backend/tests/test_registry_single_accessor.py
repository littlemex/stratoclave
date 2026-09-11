"""Guard: `mvp.models._REGISTRY` has exactly one importer -- `mvp.models`
itself (G1 of the promotion change).

`mvp.models.registry_entries()`'s own docstring claims callers "never
import the private `_REGISTRY`". Before G1 that was false: four modules
(`admin_pricing.py`, `openai_responses.py`, `anthropic.py`,
`routing/chains.py`) imported the tuple directly. This is the fail-closed
net that keeps the docstring's claim true going forward: a later change
adds a second SOURCE of registry entries (models discovered from a live
account and promoted, rather than hand-written into
`defaults/models.json`), and that second source can only be introduced
behind `registry_entries()`. A module that reaches around the accessor
via `_REGISTRY` would resolve a promoted model for serving while staying
blind to it for pricing/routing -- servable and unpriceable at once.

Follows the docstring-stripping convention `billing_guards.
source_without_docstrings` established for
`test_billing_write_discipline.py::_touches_budgets_table_as_a_raw_writer`:
a module that only NAMES `_REGISTRY` in prose (a comment or docstring
explaining what NOT to do) must not trip this; only a real
`from <...models> import _REGISTRY` does. `mvp.observability.
quota_reconciler` defines its OWN, unrelated, never-imported `_REGISTRY`
(a pool-check dispatch dict) -- a grep on the bare name finds it, a grep
on the import does not, and neither must this guard.
"""
from __future__ import annotations

import ast
from pathlib import Path

from tests import billing_guards

REPO_ROOT = Path(__file__).resolve().parents[2]
MVP_ROOT = REPO_ROOT / "backend" / "mvp"

# The one module allowed to hold `_REGISTRY` -- it is defined here.
_EXEMPT_RELPATH = "backend/mvp/models.py"


def _file_dotted_name(path: Path) -> tuple[str, bool]:
    """(dotted module name, is_package) for `path`, relative to `backend/`."""
    rel = path.relative_to(REPO_ROOT / "backend").with_suffix("")
    parts = rel.parts
    is_package = parts[-1] == "__init__"
    if is_package:
        parts = parts[:-1]
    return ".".join(parts), is_package


def _resolve_import_from(file_dotted: str, is_package: bool, node: ast.ImportFrom) -> str:
    """The absolute dotted module `node` imports from, resolving a relative
    import (`level` > 0) against `file_dotted`'s own enclosing package --
    the same resolution the import system performs at runtime. `level == 1`
    ("from .X import Y") resolves against the CURRENT package; each
    additional level strips one more package component."""
    if node.level == 0:
        return node.module or ""
    pkg_parts = file_dotted.split(".")
    if not is_package:
        pkg_parts = pkg_parts[:-1]
    up = node.level - 1
    if up:
        pkg_parts = pkg_parts[:-up] if up < len(pkg_parts) else []
    base = ".".join(pkg_parts)
    if node.module:
        return f"{base}.{node.module}" if base else node.module
    return base


def _registry_import_sites(source: str, file_dotted: str, is_package: bool) -> list[str]:
    """Enclosing qualnames of every `from <...models> import _REGISTRY[...]`
    in `source` -- module-level or function-local (`chains.py`'s original
    shape), plain or aliased. Parsed from the docstring-blanked source
    (`billing_guards.source_without_docstrings`) so this guard is built the
    same way as the suite's other structural guards; blanking a docstring's
    text cannot turn prose into a real `ImportFrom` node either way -- a
    docstring is a string constant, never parsed as statements -- but a
    fixed test below pins that property directly rather than relying on it
    silently."""
    try:
        code_only = billing_guards.source_without_docstrings(source)
    except SyntaxError:
        code_only = source
    tree = billing_guards.load(code_only)
    sites = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        if _resolve_import_from(file_dotted, is_package, node) != "mvp.models":
            continue
        for alias in node.names:
            if alias.name == "_REGISTRY":
                sites.append(billing_guards.enclosing_qualname(node))
    return sites


def test_only_models_py_imports_the_private_registry_tuple():
    """FAIL-CLOSED: no module under `backend/mvp/` other than `models.py`
    itself may `import _REGISTRY` from it. Read through
    `mvp.models.registry_entries()` instead."""
    offenders: dict[str, list[str]] = {}
    for path in MVP_ROOT.rglob("*.py"):
        rel = str(path.relative_to(REPO_ROOT))
        if rel == _EXEMPT_RELPATH:
            continue
        file_dotted, is_package = _file_dotted_name(path)
        sites = _registry_import_sites(path.read_text(), file_dotted, is_package)
        if sites:
            offenders[rel] = sites
    assert not offenders, (
        "these modules import the private `mvp.models._REGISTRY` tuple directly "
        f"instead of calling `registry_entries()`: {offenders}"
    )


# ------------------------------------- a mention in prose is not an import --

PLANTED_DOCSTRING_ONLY_MENTION = '''
"""This module resolves models through `mvp.models.registry_entries()`.
It must NEVER do `from mvp.models import _REGISTRY` directly -- that
private tuple is for `models.py` alone."""

from mvp.models import registry_entries


def handler():
    return list(registry_entries())
'''


def test_a_docstring_mention_of_the_import_does_not_trip_the_guard():
    """The false positive a bare-text/grep guard would produce, reproduced
    synthetically: the exact phrase `from mvp.models import _REGISTRY`
    appears in this module, but only inside its docstring, disclaiming it.
    The module DOES import from `mvp.models` for real (`registry_entries`),
    which is what makes this a real test of the docstring-stripping rather
    than a trivial "no import at all" case."""
    sites = _registry_import_sites(PLANTED_DOCSTRING_ONLY_MENTION, "mvp.planted", False)
    assert sites == [], (
        "a docstring-only mention of `from mvp.models import _REGISTRY` tripped the guard"
    )
    # Non-vacuity: a NAIVE raw-text check (the shape a plain grep/regex guard
    # would use, with no docstring-stripping) DOES fire on this fixture, so
    # the assertion above is exercising the false-positive case this test
    # exists to close, not vacuously true because the fixture never contains
    # the pattern at all.
    naive_hit = ("_REGISTRY" in PLANTED_DOCSTRING_ONLY_MENTION
                 and "import" in PLANTED_DOCSTRING_ONLY_MENTION
                 and "models" in PLANTED_DOCSTRING_ONLY_MENTION)
    assert naive_hit is True, (
        "the planted source does not even exercise the false-positive case this "
        "test is supposed to guard against -- fix the fixture, not the guard"
    )


PLANTED_REAL_TOP_LEVEL_IMPORT = '''
from mvp.models import _REGISTRY


def handler():
    return list(_REGISTRY)
'''


def test_a_real_top_level_import_still_trips_the_guard():
    """The other half of the same fix: a real, absolute
    `from mvp.models import _REGISTRY` at module top level -- the exact
    shape `admin_pricing.py` and `openai_responses.py` had before G1 --
    must still be caught."""
    sites = _registry_import_sites(PLANTED_REAL_TOP_LEVEL_IMPORT, "mvp.planted", False)
    assert sites == ["<module>"], sites


PLANTED_REAL_RELATIVE_FUNCTION_LOCAL_IMPORT = '''
def handler():
    from .models import _REGISTRY
    return list(_REGISTRY)
'''


def test_a_real_relative_function_local_import_still_trips_the_guard():
    """The shape `routing/chains.py` had before G1: a RELATIVE, FUNCTION-LOCAL
    import. Must resolve `.models` against the planted module's own package
    and still be caught, naming the enclosing function."""
    sites = _registry_import_sites(
        PLANTED_REAL_RELATIVE_FUNCTION_LOCAL_IMPORT, "mvp.planted", False)
    assert sites == ["handler"], sites


def test_an_aliased_import_still_trips_the_guard():
    """`from mvp.models import _REGISTRY as R` still imports the private
    name; aliasing it must not be a bypass."""
    source = '''
from mvp.models import _REGISTRY as R


def handler():
    return list(R)
'''
    sites = _registry_import_sites(source, "mvp.planted", False)
    assert sites == ["<module>"], sites


def test_quota_reconcilers_own_registry_does_not_trip_the_import_guard():
    """The trap this guard must not fall into: `mvp.observability.
    quota_reconciler` defines its OWN, unrelated `_REGISTRY` (a pool-check
    dispatch dict) -- never imported from `mvp.models`. Read directly from
    the real file, not a planted fixture, so a future edit to that file
    that accidentally started importing the real registry would be caught
    here rather than only in the repo-wide scan above."""
    path = MVP_ROOT / "observability" / "quota_reconciler.py"
    file_dotted, is_package = _file_dotted_name(path)
    sites = _registry_import_sites(path.read_text(), file_dotted, is_package)
    assert sites == [], (
        f"quota_reconciler.py's own unrelated `_REGISTRY` tripped the import guard: {sites}"
    )
