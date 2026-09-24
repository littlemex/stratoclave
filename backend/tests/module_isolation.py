"""Re-running a module's top-level code without replacing the module everyone else holds.

Several tests need to know what a module does AT IMPORT under a given environment -- a
registry built with a region variable set, a TTL floor applied to a mis-set value, a
composed registry started while its store is unreachable. The obvious tools for that are
`importlib.reload(module)` and deleting the module from `sys.modules` and importing it
again. Both corrupt the rest of the run, in two different ways:

* `reload` re-executes into the SAME module dict, so every class and function in it is
  replaced by a new object. Any module or test that did `from mvp.models import
  ModelEntry` still holds the old class, and `isinstance(entry, ModelEntry)` is False for
  every entry built afterwards. Measured: running the store-unreachable activation test
  before `test_the_shipped_document_loads_and_is_the_one_in_effect` fails the latter.
* Deleting from `sys.modules` creates a SECOND module object. Modules imported earlier
  keep reading the first; modules that import lazily afterwards reach the second. For the
  model registry that splits the process in two: `mvp.admin_pricing` bound
  `registry_entries` at import and keeps reading the orphaned registry, while activation's
  lazy `invalidate_composed_registry` clears the new one -- so an activated model is
  absent from the pricing reader however many times the cache is invalidated. That is the
  failure `test_a_pricing_reader_sees_the_activated_model_under_its_declared_rate_key`
  recorded as order-dependent and not root-caused, including the observation that
  explicit invalidation did not fix it.

`fresh_copy(module)` does neither. It executes the module's source into a NEW module
object under a unique name of its own, so the copy runs its top-level code under
whatever the test has arranged, the real module's `sys.modules` entry is never touched,
and nothing else in the process can observe the copy.
Relative imports inside the copy resolve against the real package, so it shares every
OTHER module with the run, which is what "this module, started now" means.
"""
from __future__ import annotations

import importlib.util
import itertools
import sys
from types import ModuleType

_serial = itertools.count()


def fresh_copy(module: ModuleType) -> ModuleType:
    """A throwaway re-execution of `module`, invisible to the rest of the process."""
    origin = module.__spec__.origin if module.__spec__ else None
    if not origin:
        raise ValueError(f"{module.__name__} has no source file to re-execute")
    package = module.__name__.rpartition(".")[0]
    # A name under the same package, so relative imports resolve; a unique one, so two
    # copies in one test never alias; never inserted into `sys.modules`.
    name = f"{package}._isolated_{module.__name__.rpartition('.')[2]}_{next(_serial)}"
    spec = importlib.util.spec_from_file_location(name, origin)
    copy = importlib.util.module_from_spec(spec)
    # Registered under its OWN unique name only while its top-level code runs, then
    # removed. `dataclasses` resolves a class's annotations through
    # `sys.modules[cls.__module__]` at definition time, so a module that defines one
    # cannot execute unregistered. The name is unique, so this never touches the real
    # module's entry -- which is the whole point.
    sys.modules[name] = copy
    try:
        spec.loader.exec_module(copy)
    finally:
        sys.modules.pop(name, None)
    return copy
