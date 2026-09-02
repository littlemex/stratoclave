#!/usr/bin/env python3
"""Static reachability audit: which `DYNAMODB_*` env vars can a scheduled
Lambda's handler actually cause `dynamo.client.get_dynamodb_resource().Table(...)`
to read a name from, and does the CDK stack that deploys it set them all?

Background: every table-name resolver in this codebase follows the same
shape -- `os.getenv("DYNAMODB_<X>_TABLE", "stratoclave-<x>")`, or the
`dynamo/client.py::table_name(env_var, fallback)` helper that wraps the same
call with the env var name passed as a literal. An UNSET var is never an
error; it silently repoints the table at a DIFFERENT deployment's table named
`stratoclave-<x>`. The ECS service task passes every table env var explicitly
(`iac/bin/iac.ts`) and never hits this. A scheduled Lambda stack that hand-picks
a SUBSET of tables for its handler can miss one -- exactly what happened to
`quota-reconciler-stack.ts` (`DYNAMODB_QUOTA_EVENTS_TABLE`, fixed on a real
AccessDeniedException) and `certificate-scheduler-stack.ts`
(`DYNAMODB_TENANTS_TABLE`, fixed alongside this script).

This module answers, for one (module, function) entrypoint: "walk everything
that function's code can reach -- its own body, every module-level name it
imports (transitively, following package `__init__.py` re-exports), every
sibling top-level name in the same module it calls, the `__init__` of every
class whose instance it constructs plus only the specific methods it calls
on that instance -- and collect every `DYNAMODB_*` string literal passed as
the first argument to `os.getenv(...)` or to `dynamo.client.table_name(...)`."

Reachability is by NAME, not by whole-module or whole-class inclusion.
Two false positives surfaced while writing this and are why it works the way
it does, not the simpler way tried first:
  * Whole-MODULE inclusion (importing a module for one function pulls in
    every top-level constant in that file) falsely required
    `DYNAMODB_OBSERVABILITY_TABLE` for the certificate scheduler: it imports
    `_safe_key_token` from `mvp.observability.store`, a file that ALSO
    happens to define `_TABLE_NAME` at module level, which the scheduler
    never reads.
  * Whole-CLASS inclusion (constructing `Foo()` pulls in every method `class
    Foo` defines) falsely required `DYNAMODB_USER_TENANTS_TABLE`:
    `TenantsRepository.archive()` reads that table, but the certificate
    scheduler only ever calls `TenantsRepository().list_all(...)` -- a
    different method the scheduler's handler never reaches.
This script instead tracks, per scanned function body, which class an
instance variable was constructed from, and chases only `__init__` plus the
specific method names actually called on it (or on an inline
`ClassName().method()` chain). Package `__init__.py` re-exports (`from
.tenants import TenantsRepository` inside `dynamo/__init__.py`) are followed
to the module that actually defines the name, the same way Python's own
import resolves them.

A THIRD gap surfaced auditing `mvp.observability.quota_reconciler.handler`
itself: `reconcile_all()` dispatches through a `_REGISTRY` dict that
`@register_check(...)`-decorated functions populate as a side effect of being
IMPORTED, then calls every registered function by iterating the dict -- never
by a name any static call-graph walk can see. Six checks are registered this
way in `quota_reconciler.py` itself, and three more in `mvp/grants.py`,
reached only through `missing_declared_checks()`'s `import mvp.grants`
(kept for its side effect alone; the bound name `mvp` is never used
afterward). `grant_target_row_exists` (one of the three) is exactly the read
of `DYNAMODB_QUOTA_EVENTS_TABLE` the real pool-reconciler
AccessDeniedException came from -- so this script special-cases ONE named
decorator (`REGISTRY_DECORATOR_NAMES`, currently `{"register_check"}`):
importing a module for ANY reason (bare `import X`, `from . import X`, or
`from X import specific_name`) is modeled as also running every
`@register_check`-decorated function defined at that module's top level,
because Python actually does execute a module's whole top level on import,
decorators included.

KNOWN BLIND SPOTS (report these, don't paper over them):
  * The decorator-registry handling above is NAMED, not general: it knows
    `register_check` because that is the one actual registry in this
    codebase's real bug history. A differently-named plugin-registration
    decorator introduced later needs its name added to
    `REGISTRY_DECORATOR_NAMES`, or this script silently under-reports again
    -- it will not warn you that a registry it doesn't know about exists.
  * A dynamically computed attribute (`getattr(x, name)`) or a module-level
    proxy (`__getattr__` on a module) is invisible to this walk.
  * `importlib.import_module(name)` with a non-literal `name`, and any
    factory that returns a repository object without a syntactically visible
    `ClassName(...)` construction, are invisible. None of the four
    scheduled-Lambda handlers audited here use either pattern (checked by
    hand when this script was written); a future one might, silently.
  * A method inherited from a base class (not defined directly in the
    subclass body) is not followed -- recorded in `unresolved`, not silently
    dropped. None of the four handlers audited here construct a class with
    inherited (as opposed to locally overridden) repository methods.
  * Decorators and metaclasses are not modeled; a decorator that swaps in a
    different callable at runtime would not be seen.
  * This finds every literal env var NAME reachable from the entrypoint. It
    does not further verify the VALUE is passed to an actual `.Table(...)`
    call (vs. computed and discarded) -- reachability of the getenv call is
    the bar, which is the same or a stricter bar than what actually runs.

Usage:
    python3 scripts/scheduled_lambda_env_audit.py <module.dotted.path> <function_name> [<function_name> ...]

Prints one JSON object to stdout:
    {"env_vars": [...], "dynamodb_env_vars": [...], "modules_visited": [...],
     "names_visited": [...], "unresolved": [...]}
"""
from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent


def _module_path(dotted: str) -> tuple[Path, bool] | None:
    """Returns (path, is_package). `is_package` is True for a package's
    `__init__.py`, whose OWN relative imports resolve against itself, not
    against its parent (see `ModuleIndex.package`)."""
    rel = Path(*dotted.split("."))
    candidate = BACKEND_DIR / rel.with_suffix(".py")
    if candidate.is_file():
        return candidate, False
    candidate_pkg = BACKEND_DIR / rel / "__init__.py"
    if candidate_pkg.is_file():
        return candidate_pkg, True
    return None


#: See `ModuleIndex.registry_decorated`'s comment: named, not general. Add a
#: name here if a new decorator-based plugin registry is introduced.
REGISTRY_DECORATOR_NAMES = {"register_check"}

#: A worklist item's `name` field when it means "run this module's import-time
#: side effects" (decorator registrations) rather than "resolve this specific
#: top-level name". Importing a module for ANY reason -- a bare `import X`, a
#: module-alias `from . import X`, or even `from X import specific_name` --
#: executes X's entire top-level code once, including every decorator
#: application; this sentinel is how that gets modeled without conflating it
#: with an actual Python identifier (leading/trailing double-underscore + odd
#: spacing keeps it un-collidable with any real name).
MODULE_IMPORT_SIDE_EFFECTS = "  <module-import-side-effects>  "


def _resolve_relative(package: str, node: ast.ImportFrom) -> str:
    """`from ...module import X` -> the absolute dotted module `...module`
    resolves to, given the PACKAGE of the module doing the importing
    (`ModuleIndex.package`, already package-vs-plain-module aware)."""
    if node.level == 0:
        return node.module or ""
    base_parts = package.split(".") if package else []
    # level=1 is "this package" (no trim); each extra level trims one more.
    trim = node.level - 1
    if trim:
        base_parts = base_parts[:-trim] if trim <= len(base_parts) else []
    base = ".".join(p for p in base_parts if p)
    if node.module:
        return f"{base}.{node.module}" if base else node.module
    return base


class ModuleIndex:
    """One module's AST, parsed once.

    `top_level`: FunctionDef/AsyncFunctionDef/ClassDef/Assign/AnnAssign names
    DEFINED in this file.
    `class_methods[ClassName][method]`: per-class method index, so a
    construction chases just `__init__` and a call chases just that method
    -- never the whole class body (see module docstring).
    `reexports[name] = (target_module, target_name_or_None)`: names this
    module imports at MODULE level (not inside a function) and thereby makes
    available to ITS importers under the same name -- e.g.
    `dynamo/__init__.py`'s `from .tenants import TenantsRepository`.
    `target_name_or_None` is None for a bare module-alias re-export
    (`import X` / `from . import X`); none of the four audited handlers'
    reexport chains hit that case, so it is recorded but deliberately not
    chased further (see KNOWN BLIND SPOTS).
    """

    _cache: dict[str, "ModuleIndex | None"] = {}

    def __init__(self, dotted: str, path: Path, is_package: bool):
        self.dotted = dotted
        self.path = path
        self.is_package = is_package
        self.package = dotted if is_package else (
            dotted.rsplit(".", 1)[0] if "." in dotted else "")
        self.tree = ast.parse(path.read_text(), filename=str(path))
        self.top_level: dict[str, ast.AST] = {}
        self.classes: dict[str, ast.ClassDef] = {}
        self.class_methods: dict[str, dict[str, ast.AST]] = {}
        self.reexports: dict[str, tuple[str, "str | None"]] = {}
        # Functions decorated with a KNOWN plugin-registration decorator
        # (`REGISTRY_DECORATOR_NAMES`) run their registration side effect on
        # IMPORT, not on any syntactically visible call -- they are later invoked
        # by iterating a dict/list the decorator populated, which no static walk
        # over the caller's own body can see. `register_check` is the ONE name
        # this script knows about, because it is the actual pattern
        # `mvp/observability/quota_reconciler.py`'s reconciler and
        # `mvp/grants.py`'s three grant-aware checks use, and finding it (by
        # hand, reading the real code) is what surfaced a THIRD env var this
        # audit would otherwise have silently missed for the quota-reconciler
        # entrypoint. See the module docstring's blind-spots note: a
        # differently-named registry introduced later needs its name added
        # here, or this script under-reports again, silently.
        self.registry_decorated: list[str] = []

        for stmt in self.tree.body:
            if isinstance(stmt, ast.ClassDef):
                self.top_level[stmt.name] = stmt
                self.classes[stmt.name] = stmt
                methods: dict[str, ast.AST] = {}
                for member in stmt.body:
                    if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        methods[member.name] = member
                self.class_methods[stmt.name] = methods
            elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.top_level[stmt.name] = stmt
                for dec in stmt.decorator_list:
                    dec_name = None
                    if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Name):
                        dec_name = dec.func.id
                    elif isinstance(dec, ast.Name):
                        dec_name = dec.id
                    if dec_name in REGISTRY_DECORATOR_NAMES:
                        self.registry_decorated.append(stmt.name)
            elif isinstance(stmt, ast.Assign):
                for t in stmt.targets:
                    if isinstance(t, ast.Name):
                        self.top_level[t.id] = stmt
            elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                self.top_level[stmt.target.id] = stmt
            elif isinstance(stmt, ast.ImportFrom):
                target_module = _resolve_relative(self.package, stmt)
                for alias in stmt.names:
                    if alias.name == "*":
                        continue
                    local_name = alias.asname or alias.name
                    if stmt.module is None:
                        submodule = (f"{target_module}.{alias.name}"
                                     if target_module else alias.name)
                        self.reexports[local_name] = (submodule, None)
                    else:
                        self.reexports[local_name] = (target_module, alias.name)
            elif isinstance(stmt, ast.Import):
                for alias in stmt.names:
                    local_name = alias.asname or alias.name.split(".")[0]
                    self.reexports[local_name] = (alias.name, None)

    @classmethod
    def load(cls, dotted: str) -> "ModuleIndex | None":
        if dotted in cls._cache:
            return cls._cache[dotted]
        found = _module_path(dotted)
        if found is None:
            cls._cache[dotted] = None
            return None
        path, is_package = found
        idx = cls(dotted, path, is_package)
        cls._cache[dotted] = idx
        return idx


def resolve_definition(dotted: str, name: str,
                        _seen: "set[tuple[str, str]] | None" = None) -> "tuple[str, str] | None":
    """Follow module-level re-exports until `name` is actually DEFINED
    (a top-level function/class/assign), the way Python's own import
    machinery would when you `from dynamo import TenantsRepository` and
    `dynamo/__init__.py` itself only does `from .tenants import TenantsRepository`.
    Returns (defining_module, name) or None if unresolvable."""
    seen = _seen if _seen is not None else set()
    if (dotted, name) in seen:
        return None
    seen.add((dotted, name))
    idx = ModuleIndex.load(dotted)
    if idx is None:
        return None
    if name in idx.top_level:
        return (dotted, name)
    target = idx.reexports.get(name)
    if target is None:
        return None
    target_module, target_name = target
    if target_name is None:
        return None  # bare module-alias re-export chain; not chased (see blind spots)
    return resolve_definition(target_module, target_name, seen)


ENV_LITERAL_FUNCS = {"getenv", "environ_get", "table_name"}


def _literal_str(node: ast.AST) -> "str | None":
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _call_target_name(call: ast.Call) -> "str | None":
    """The bare function/attribute name of a Call, e.g. `os.getenv(...)` -> 'getenv',
    `table_name(...)` -> 'table_name'."""
    f = call.func
    if isinstance(f, ast.Attribute):
        return f.attr
    if isinstance(f, ast.Name):
        return f.id
    return None


WorkItem = tuple[str, str, "str | None"]  # (module, name, method_or_None)


def audit(entrypoints: list[tuple[str, str]]) -> dict:
    """`entrypoints`: list of (dotted_module, function_name)."""
    env_vars: set[str] = set()
    visited: set[WorkItem] = set()
    unresolved: list[str] = []
    worklist: list[WorkItem] = [(m, f, None) for m, f in entrypoints]

    def scan_subtree(module_idx: ModuleIndex, subtree: ast.AST) -> None:
        # Three kinds of local (this-subtree-scoped) bindings a name might need
        # resolving through: a module object (`from . import signals`, `import
        # dynamo.client`), a symbol's home module (`from dynamo.tenants import
        # TenantsRepository`), and an instance variable's class (`repo =
        # TenantsRepository()`). All three are populated as we walk, and consumed
        # by later nodes in the SAME walk. `ast.walk` is breadth-first BY DEPTH:
        # every statement at this subtree's own nesting level is visited (and so
        # every binding at that level recorded) before any node nested inside any
        # of them -- which is exactly the dependency order straight-line handler
        # bodies need (an import or `x = Foo()` at the top of a function body is
        # always a sibling AST node of, and therefore visited before, an
        # `x.method()` call further down the SAME body).
        local_module_aliases: dict[str, str] = {}
        imported_symbol_module: dict[str, str] = {}
        local_instance_vars: dict[str, tuple[str, str]] = {}  # var -> (home_module, class_name)

        def resolve_class_home(class_name: str) -> "str | None":
            if class_name in module_idx.classes:
                return module_idx.dotted
            home = imported_symbol_module.get(class_name)
            if home is None:
                return None
            resolved = resolve_definition(home, class_name)
            return resolved[0] if resolved else None

        def push_construction(class_name: str) -> "str | None":
            home = resolve_class_home(class_name)
            if home is None:
                return None
            idx = ModuleIndex.load(home)
            if idx is not None and "__init__" in idx.class_methods.get(class_name, {}):
                worklist.append((home, class_name, "__init__"))
            return home

        for node in ast.walk(subtree):
            if isinstance(node, ast.ImportFrom):
                target_module = _resolve_relative(module_idx.package, node)
                for alias in node.names:
                    if alias.name == "*":
                        unresolved.append(
                            f"{module_idx.dotted}: `from {node.module or '.'} import *` "
                            "-- wildcard imports are not resolved")
                        continue
                    local_name = alias.asname or alias.name
                    if node.module is None:
                        # `from . import signals` (or `from .. import x`): `signals`
                        # IS the submodule, bound as a module alias, not a symbol.
                        submodule = f"{target_module}.{alias.name}" if target_module else alias.name
                        local_module_aliases[local_name] = submodule
                        worklist.append((submodule, MODULE_IMPORT_SIDE_EFFECTS, None))
                    else:
                        imported_symbol_module[local_name] = target_module
                        worklist.append((target_module, alias.name, None))
                        worklist.append((target_module, MODULE_IMPORT_SIDE_EFFECTS, None))
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    local_name = alias.asname or alias.name.split(".")[0]
                    local_module_aliases[local_name] = alias.name
                    # `import mvp.grants` for its registration side effect ALONE,
                    # with `mvp` (the bound name) never subsequently referenced,
                    # is exactly `mvp/observability/quota_reconciler.py`'s real
                    # shape (`missing_declared_checks`) -- see
                    # `MODULE_IMPORT_SIDE_EFFECTS`'s docstring.
                    worklist.append((alias.name, MODULE_IMPORT_SIDE_EFFECTS, None))

            elif isinstance(node, ast.Assign):
                # `var = ClassName(...)` -- remember the instance's class so a
                # LATER `var.method(...)` chases only that method, not the class.
                if (len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
                        and isinstance(node.value, ast.Call)
                        and isinstance(node.value.func, ast.Name)):
                    class_name = node.value.func.id
                    home = push_construction(class_name)
                    if home:
                        local_instance_vars[node.targets[0].id] = (home, class_name)

            elif isinstance(node, ast.Call):
                fname = _call_target_name(node)
                if fname in ENV_LITERAL_FUNCS and node.args:
                    lit = _literal_str(node.args[0])
                    if lit:
                        env_vars.add(lit)
                f = node.func
                if isinstance(f, ast.Name):
                    if f.id in module_idx.classes or f.id in imported_symbol_module:
                        # A bare/unassigned construction, e.g. `TenantsRepository()`
                        # with no chained call and no assignment: only `__init__`
                        # runs. The chained-call shape (`ClassName().method()`) is
                        # caught by the `elif isinstance(f, ast.Attribute)` branch
                        # below, since ITS `func` is an Attribute, not this Name.
                        push_construction(f.id)
                    else:
                        # A bare call to a top-level function/constant, whether
                        # local to this module or reached via a specific-symbol
                        # import recorded above.
                        home = imported_symbol_module.get(f.id, module_idx.dotted)
                        worklist.append((home, f.id, None))
                elif (isinstance(f, ast.Attribute) and isinstance(f.value, ast.Call)
                      and isinstance(f.value.func, ast.Name)):
                    # `ClassName().method(...)` -- construct-and-call in one
                    # expression, the dominant repository-call idiom here.
                    class_name = f.value.func.id
                    home = resolve_class_home(class_name)
                    if home:
                        push_construction(class_name)
                        worklist.append((home, class_name, f.attr))
                    else:
                        unresolved.append(
                            f"{module_idx.dotted}: {class_name}().{f.attr}(...) -- "
                            "defining module of the class could not be resolved")

            elif isinstance(node, ast.Attribute):
                if isinstance(node.value, ast.Name):
                    if node.value.id in local_module_aliases:
                        # `signals._TABLE_NAME` / `mod.helper` where `mod` is a
                        # module object bound above.
                        worklist.append((local_module_aliases[node.value.id], node.attr, None))
                    elif node.value.id in local_instance_vars:
                        # `repo.list_all(...)` -- chase only THIS method on the
                        # class `repo` was constructed from.
                        home, class_name = local_instance_vars[node.value.id]
                        worklist.append((home, class_name, node.attr))

            elif isinstance(node, ast.Name) and not isinstance(node.ctx, ast.Store):
                # A bare reference to another top-level name in THIS module
                # (sibling function/constant) -- e.g. `_default_credit_fallback()`
                # calling `default_tenant_credit()`. Classes are excluded: a bare
                # `ClassName` Name reference with no call is either a type
                # annotation or an `isinstance` check, never a reason to pull in
                # a constructor this walk has no evidence actually runs.
                if node.id in module_idx.top_level and node.id not in module_idx.classes:
                    worklist.append((module_idx.dotted, node.id, None))

    while worklist:
        dotted, name, method = worklist.pop()
        key = (dotted, name, method)
        if key in visited:
            continue
        visited.add(key)

        if name == MODULE_IMPORT_SIDE_EFFECTS:
            idx = ModuleIndex.load(dotted)
            if idx is None:
                unresolved.append(f"{dotted} (module not found under backend/ -- "
                                  "stdlib/third-party or unresolvable dotted path; its "
                                  "import-time side effects, if any, are unauditable)")
                continue
            for fn_name in idx.registry_decorated:
                worklist.append((dotted, fn_name, None))
            continue

        resolved = resolve_definition(dotted, name)
        if resolved is None:
            label = f"{dotted}.{name}" + (f".{method}" if method else "")
            unresolved.append(f"{label} (module or top-level name not found under backend/ -- "
                              "stdlib/third-party, a bare module-alias re-export chain, "
                              "or a builtin)")
            continue
        actual_module, actual_name = resolved
        idx = ModuleIndex.load(actual_module)
        assert idx is not None  # resolve_definition only returns a hit it already loaded

        if method is not None:
            target = idx.class_methods.get(actual_name, {}).get(method)
            if target is None:
                unresolved.append(
                    f"{actual_module}.{actual_name}.{method} (method not found directly on "
                    "the class -- possibly inherited from a base class this walk does not "
                    "follow, or defined dynamically)")
                continue
        else:
            target = idx.top_level.get(actual_name)
            if isinstance(target, ast.ClassDef):
                # Only reached for an entrypoint that names a class directly
                # (none of the four scheduled-Lambda handlers do -- they are all
                # plain functions); scan just __init__, on the same "construction
                # implies __init__, not the whole class" rule as everywhere else.
                init = idx.class_methods.get(actual_name, {}).get("__init__")
                if init is not None:
                    scan_subtree(idx, init)
                continue
        scan_subtree(idx, target)

    return {
        "env_vars": sorted(env_vars),
        "dynamodb_env_vars": sorted(v for v in env_vars if v.startswith("DYNAMODB_")),
        "modules_visited": sorted({d for d, _, _ in visited}),
        "names_visited": sorted(
            f"{d}.{n}" + (f".{m}" if m else "") for d, n, m in visited),
        "unresolved": sorted(set(unresolved)),
    }


def main() -> None:
    if len(sys.argv) < 3:
        print(__doc__, file=sys.stderr)
        sys.exit(2)
    module = sys.argv[1]
    functions = sys.argv[2:]
    result = audit([(module, fn) for fn in functions])
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
