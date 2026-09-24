"""`fresh_copy` must leave the module everyone else holds exactly as it was.

The two tools it replaced each broke a different invariant below, and each produced a
failure that appeared only when some unrelated test happened to run first -- one of which
sat in the suite for weeks recorded as "order-dependent, not root-caused".
"""
from __future__ import annotations

import sys


def test_the_real_module_object_is_not_replaced():
    """Deleting from `sys.modules` and importing again created a second module object,
    and modules imported earlier kept reading the first."""
    import mvp.models as models
    from tests.module_isolation import fresh_copy

    before = sys.modules["mvp.models"]
    fresh_copy(models)
    assert sys.modules["mvp.models"] is before


def test_classes_keep_their_identity():
    """`reload` re-executed into the same dict and replaced every class, so an
    `isinstance` against a class imported by name went False for the rest of the run."""
    import mvp.models as models
    from mvp.models import ModelEntry
    from tests.module_isolation import fresh_copy

    fresh_copy(models)
    assert models.ModelEntry is ModelEntry
    assert all(isinstance(e, ModelEntry) for e in models.load_registry())


def test_a_module_bound_at_import_still_reads_the_live_registry():
    """The exact split behind the pricing reader missing an activated model:
    `mvp.admin_pricing` bound `registry_entries` at import and must still be reading the
    module activation invalidates."""
    import mvp.admin_pricing as admin_pricing
    import mvp.models as models
    from tests.module_isolation import fresh_copy

    fresh_copy(models)
    assert admin_pricing.registry_entries.__globals__ is sys.modules["mvp.models"].__dict__


def test_the_copy_leaves_no_entry_behind():
    import mvp.models as models
    from tests.module_isolation import fresh_copy

    names_before = set(sys.modules)
    copy = fresh_copy(models)
    assert copy.__name__ not in sys.modules
    assert set(sys.modules) - names_before == set()
