"""
Guard engine protecting the axioms of the Z3 pooled-billing proofs.

The proofs are only sound under axioms about how the code talks to DynamoDB.
This module provides pure-AST checks that FAIL CLOSED:

  A2  every mutation of pool_reserved_microusd / pool_settled_microusd goes
      through transact_write_items (the CAS-serialized path).
  A5  settle transacts carry a *stable* ClientRequestToken; reserve carries
      a *fresh* one per attempt.
  R   reaper reclaim / hold Delete transact items stay guarded by
      attribute_exists(...).

Nothing here imports boto3 or touches AWS.  Everything operates on source
text, so the self-tests can feed planted violations.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

# ---------------------------------------------------------------- constants

COUNTER_ATTRS = frozenset({"pool_reserved_microusd", "pool_settled_microusd"})
# fragments that indicate someone assembling a counter name dynamically
COUNTER_FRAGMENTS = ("reserved_microusd", "settled_microusd")

WRITE_APIS = frozenset({
    "put_item", "update_item", "delete_item",
    "batch_write_item", "batch_writer",
    "transact_write_items", "transact_write",
    "execute_statement", "execute_transaction", "batch_execute_statement",
})
TRANSACTIONAL_APIS = frozenset({"transact_write_items"})
NON_TRANSACTIONAL_APIS = WRITE_APIS - TRANSACTIONAL_APIS

FRESHNESS_CALLS = frozenset({
    "uuid4", "uuid1", "token_hex", "token_urlsafe", "urandom",
    "time", "time_ns", "monotonic", "monotonic_ns",
    "random", "randbytes", "getrandbits", "randint",
    # project wrapper that returns a fresh uuid4 (see _fresh_idempotency_token)
    "_fresh_idempotency_token",
})

# ---------------------------------------------------------------- AST utils

def load(source: str) -> ast.Module:
    tree = ast.parse(source)
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            child._guard_parent = node  # type: ignore[attr-defined]
    return tree


def enclosing_qualname(node: ast.AST) -> str:
    parts = []
    cur = getattr(node, "_guard_parent", None)
    while cur is not None:
        if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            parts.append(cur.name)
        cur = getattr(cur, "_guard_parent", None)
    return ".".join(reversed(parts)) or "<module>"


def enclosing_function(node: ast.AST):
    cur = getattr(node, "_guard_parent", None)
    while cur is not None:
        if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return cur
        cur = getattr(cur, "_guard_parent", None)
    return None


def string_constants(node: ast.AST) -> list[str]:
    return [n.value for n in ast.walk(node)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)]


def _callee_name(fn: ast.AST):
    if isinstance(fn, ast.Attribute):
        return fn.attr
    if isinstance(fn, ast.Name):
        return fn.id
    return None


def _names_in(node: ast.AST) -> set[str]:
    out = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Name):
            out.add(n.id)
        elif isinstance(n, ast.Attribute):
            out.add(n.attr)
    return out

# ------------------------------------------------------------- write sites

@dataclass
class WriteSite:
    module: str
    qualname: str
    api: str
    lineno: int
    call: ast.Call
    strings: tuple
    kwarg_names: frozenset
    has_star_kwargs: bool

    @property
    def fingerprint(self):
        return (self.module, self.qualname, self.api)


def collect_write_sites(tree: ast.Module, module: str):
    """All DynamoDB-shaped write calls, incl. getattr-dispatch.  Second
    return value: hard violations found during collection (dynamic dispatch
    we cannot verify -> fail closed)."""
    sites, hard = [], []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        api = None
        if isinstance(node.func, ast.Attribute) and node.func.attr in WRITE_APIS:
            api = node.func.attr
        elif isinstance(node.func, ast.Call) and _callee_name(node.func.func) == "getattr":
            g = node.func
            second = g.args[1] if len(g.args) >= 2 else None
            if isinstance(second, ast.Constant) and isinstance(second.value, str):
                if second.value in WRITE_APIS:
                    api = second.value           # getattr(c, "update_item")(...)
            else:
                hard.append(
                    f"{module}:{node.lineno} {enclosing_qualname(node)}: "
                    f"dynamically-named method dispatch via getattr(...) is "
                    f"invoked here; the write-discipline guard cannot verify "
                    f"it. Restructure to a direct call (fail-closed).")
        if api is None:
            continue
        sites.append(WriteSite(
            module=module,
            qualname=enclosing_qualname(node),
            api=api,
            lineno=node.lineno,
            call=node,
            strings=tuple(string_constants(node)),
            kwarg_names=frozenset(kw.arg for kw in node.keywords if kw.arg),
            has_star_kwargs=any(kw.arg is None for kw in node.keywords),
        ))
    return sites, hard


def counter_touching_qualnames(tree: ast.Module) -> dict:
    """qualname -> [linenos] of string constants mentioning a counter attr."""
    out: dict = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if any(attr in node.value for attr in COUNTER_ATTRS):
                out.setdefault(enclosing_qualname(node), []).append(node.lineno)
    return out

# --------------------------------------------------------------- A2 checks

def check_nontransactional_counter_writes(sites, counter_funcs, module,
                                          preserving_put_allowlist,
                                          readonly_counter_update_allowlist=frozenset()):
    """HARD rule.  A non-transactional write that (a) mentions a counter attr
    in its own arguments, or (b) lives in a function that mentions one, is a
    violation -- with two reviewed exceptions, each of which must still pass a
    structural check that the write does not MUTATE a protected counter:

      * a *preserving* put_item (set_pool_limit's create branch);
      * a *counter-read-only* update_item (set_pool_limit's ceiling-CAS /
        legacy-repair branches): the function READS pool_reserved/pool_settled
        in Python to compute a value, but its DynamoDB UpdateExpression must not
        ADD/SET/REMOVE either protected counter. A2 governs mutations, not reads.
    """
    v = []
    for s in sites:
        if s.api not in NON_TRANSACTIONAL_APIS:
            continue
        touches = (any(attr in st for st in s.strings for attr in COUNTER_ATTRS)
                   or s.qualname in counter_funcs)
        if not touches:
            continue
        if s.api == "put_item" and s.qualname in preserving_put_allowlist:
            v.extend(check_preserving_put(s))
            continue
        if s.api == "update_item" and s.qualname in readonly_counter_update_allowlist:
            v.extend(check_non_mutating_counter_update(s))
            continue
        v.append(
            f"{s.module}:{s.lineno} {s.qualname}: non-transactional {s.api} "
            f"in code that references the pool money counters. AXIOM A2 of "
            f"the Z3 proofs requires ALL mutations of "
            f"pool_reserved_microusd/pool_settled_microusd to go through "
            f"transact_write_items with a ConditionExpression. This write "
            f"voids the no-over-admission proof.")
    return v


def check_non_mutating_counter_update(site: WriteSite):
    """A reviewed non-transactional update_item is allowed ONLY if it does not
    name either protected counter in ANY of its own DynamoDB expression strings
    (UpdateExpression / ConditionExpression / attribute maps). If a counter name
    is absent from the call, the write cannot ADD/SET/REMOVE it — so it is not a
    mutation A2 governs. Reading the counter in Python to compute a *different*
    attribute's value (e.g. legacy headroom repair) is fine and invisible here."""
    bad = sorted({st for st in site.strings
                  for a in COUNTER_ATTRS if a in st})
    if bad:
        return [
            f"{site.module}:{site.lineno} {site.qualname}: allow-listed "
            f"non-transactional update_item names a protected counter in "
            f"{bad!r}. An UpdateExpression/ConditionExpression over "
            f"pool_reserved/pool_settled MUST be transactional (A2 regression); "
            f"a read-only update may not mutate the counters."]
    return []


def check_preserving_put(site: WriteSite):
    """set_pool_limit is allowed to put_item the pool row ONLY as a
    read-then-rewrite that carries the previously read counter values.
    Regression checks: (1) the function must still read; (2) no dict literal
    or subscript-assign in the function may bind a counter attr to a
    CONSTANT (e.g. 0)."""
    v = []
    func = enclosing_function(site.call)
    if func is None:
        return [f"{site.module}:{site.lineno}: preserving put_item at module "
                f"level -- cannot verify, restructure into a function."]
    # A read may be a raw DynamoDB get_item/query OR a repo-level accessor
    # (`self.get(...)`) that wraps one. Recognise both so a preserving put
    # built on the repo API isn't a false positive.
    reads = [n for n in ast.walk(func)
             if isinstance(n, ast.Call)
             and _callee_name(n.func) in ("get_item", "query", "get", "pool_summary")]
    if not reads:
        v.append(
            f"{site.module}:{site.lineno} {site.qualname}: allow-listed "
            f"preserving put_item no longer reads the row first; it cannot "
            f"be preserving pool_reserved/pool_settled (A2 regression).")
    for n in ast.walk(func):
        if isinstance(n, ast.Dict):
            for k, val in zip(n.keys, n.values):
                if (isinstance(k, ast.Constant) and k.value in COUNTER_ATTRS
                        and isinstance(val, ast.Constant)):
                    v.append(
                        f"{site.module}:{n.lineno} {site.qualname}: Item maps "
                        f"{k.value!r} to constant {val.value!r}; a preserving "
                        f"put must carry the previously-read value "
                        f"(A2 regression).")
        if isinstance(n, ast.Assign):
            for t in n.targets:
                if (isinstance(t, ast.Subscript)
                        and isinstance(t.slice, ast.Constant)
                        and t.slice.value in COUNTER_ATTRS
                        and isinstance(n.value, ast.Constant)):
                    v.append(
                        f"{site.module}:{n.lineno} {site.qualname}: assigns "
                        f"constant to Item[{t.slice.value!r}] "
                        f"(A2 regression).")
    return v


def check_write_inventory(sites, allowed_sites):
    """FAIL-CLOSED: every write call must match a reviewed fingerprint."""
    v = []
    for s in sites:
        if s.fingerprint not in allowed_sites:
            v.append(
                f"UNREVIEWED WRITE SITE {s.fingerprint!r} at "
                f"{s.module}:{s.lineno}.\n"
                f"  Every DynamoDB write in the billing path must be reviewed "
                f"against proof axioms A2/A5 and its fingerprint added to the "
                f"allowlist in the guard test. Paste after review:\n"
                f"      {s.fingerprint!r},")
    return v


def check_counter_registry(counter_funcs, registry, module):
    """FAIL-CLOSED: every function that even *mentions* a counter attribute
    name must be consciously registered.  This closes the indirection hole
    (helper builds the UpdateExpression, a different function writes it)."""
    v = []
    for qn, lines in sorted(counter_funcs.items()):
        # `registry` is the per-module set of allowed qualnames (the caller
        # already selected COUNTER_FUNCTIONS[module]).
        if qn not in registry:
            v.append(
                f"{module}:{lines[0]}: {qn!r} references a pool money counter "
                f"attribute name but is not registered in COUNTER_FUNCTIONS. "
                f"Review it against A2, then register (module, qualname).")
    return v


def dynamic_counter_name_suspects(tree: ast.Module, module: str):
    """Best-effort net for dynamically assembled counter names."""
    v = []
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            const = "".join(x.value for x in node.values
                            if isinstance(x, ast.Constant)
                            and isinstance(x.value, str))
            dynamic = any(isinstance(x, ast.FormattedValue)
                          for x in node.values)
            if (dynamic and "microusd" in const and "pool" in const
                    and not any(a in const for a in COUNTER_ATTRS)):
                v.append(f"{module}:{node.lineno}: f-string appears to "
                         f"assemble a pool *_microusd attribute name "
                         f"dynamically ({const!r}); the write-discipline "
                         f"guard cannot track this. Use the literal name.")
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            s = node.value
            if (any(f in s for f in COUNTER_FRAGMENTS)
                    and not any(a in s for a in COUNTER_ATTRS)):
                v.append(f"{module}:{node.lineno}: string {s!r} is a fragment "
                         f"of a counter attribute name; suspected dynamic "
                         f"name construction (fail-closed).")
    return v

# -------------------------------------------------- budget-row destruction

def _mentions_budget_row(node: ast.AST) -> bool:
    return ("budget_sk" in _names_in(node)
            or any("BUDGET#" in s for s in string_constants(node)))


def check_no_budget_row_deletion(tree, module, sites):
    """Deleting the BUDGET row zeroes both counters outside any modeled
    transition -- proof-voiding whether transactional or not."""
    v = []
    for kind, d, lineno in transact_item_dicts(tree):
        if kind == "Delete" and _mentions_budget_row(d):
            v.append(f"{module}:{lineno}: transact Delete item targets the "
                     f"BUDGET row; deleting the pool row destroys the money "
                     f"counters outside every modeled transition.")
    for s in sites:
        if s.api == "delete_item" and _mentions_budget_row(s.call):
            v.append(f"{s.module}:{s.lineno} {s.qualname}: delete_item "
                     f"targets the BUDGET row (see above).")
    return v

# ------------------------------------------------------------- A5 (tokens)

def _func_params(func) -> set:
    a = func.args
    names = [x.arg for x in (a.posonlyargs + a.args + a.kwonlyargs)]
    if a.vararg:
        names.append(a.vararg.arg)
    if a.kwarg:
        names.append(a.kwarg.arg)
    return set(names)


def _assignments_to(func, name):
    out = []
    for n in ast.walk(func):
        if isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Name) and t.id == name:
                    out.append(n.value)
        elif (isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name)
              and n.target.id == name and n.value is not None):
            out.append(n.value)
    return out


# Calls that deterministically DERIVE a stable token from their arguments
# (same inputs -> same token), so a lost-ack retry dedupes. Distinct from
# FRESHNESS_CALLS (which return a new value each call).
STABLE_DERIVING_CALLS = frozenset({"_derived_token", "uuid5", "uuid3"})


def classify_token_expr(expr, func, depth: int = 4) -> str:
    """'fresh' | 'stable' | 'constant' | 'unknown'.  Heuristic; 'unknown'
    fails closed and points at the runtime contract test."""
    if expr is None or depth == 0:
        return "unknown"
    # A deterministic-derivation call is stable (top-level only — a fresh
    # component nested inside would still make the result fresh).
    if isinstance(expr, ast.Call) and _callee_name(expr.func) in STABLE_DERIVING_CALLS:
        return "stable"
    for n in ast.walk(expr):
        if isinstance(n, ast.Call) and _callee_name(n.func) in FRESHNESS_CALLS:
            return "fresh"
    if isinstance(expr, ast.Constant):
        return "constant"
    if isinstance(expr, ast.JoinedStr):
        kinds = {classify_token_expr(x.value, func, depth - 1)
                 for x in expr.values if isinstance(x, ast.FormattedValue)}
        if not kinds:
            return "constant"
        if "fresh" in kinds:
            return "fresh"
        return "stable" if kinds <= {"stable", "constant"} else "unknown"
    if isinstance(expr, ast.Name):
        if func is not None:
            assigns = _assignments_to(func, expr.id)
            if len(assigns) == 1:
                return classify_token_expr(assigns[0], func, depth - 1)
            if not assigns and expr.id in _func_params(func):
                # caller-supplied token: treated stable; caller freshness is
                # covered by the runtime contract test.
                return "stable"
        return "unknown"
    if isinstance(expr, ast.Attribute):
        return "stable"
    if isinstance(expr, ast.BinOp):
        kinds = {classify_token_expr(expr.left, func, depth - 1),
                 classify_token_expr(expr.right, func, depth - 1)}
        if "fresh" in kinds:
            return "fresh"
        return "unknown" if "unknown" in kinds else "stable"
    return "unknown"


def check_transact_tokens(sites, expected_kinds):
    v = []
    for s in sites:
        if s.api != "transact_write_items":
            continue
        # A site explicitly reviewed as "none" is an idempotent SET (not a
        # money ADD) outside the settle path, where a lost-ack retry is
        # harmless — it may legitimately carry no token. Still must be
        # consciously registered (fail-closed for anything unregistered).
        if expected_kinds.get(s.qualname) == "none":
            continue
        if "ClientRequestToken" not in s.kwarg_names:
            if s.has_star_kwargs:
                v.append(f"{s.module}:{s.lineno} {s.qualname}: "
                         f"transact_write_items(**kwargs) -- cannot verify "
                         f"ClientRequestToken statically (A5, fail-closed). "
                         f"Pass the token as an explicit keyword.")
            else:
                v.append(f"{s.module}:{s.lineno} {s.qualname}: "
                         f"transact_write_items WITHOUT ClientRequestToken. "
                         f"AXIOM A5: settle-path transacts need a token for "
                         f"lost-ack dedupe; the settle-once proof is void "
                         f"without it.")
            continue
        expected = expected_kinds.get(s.qualname)
        if expected is None:
            v.append(f"{s.module}:{s.lineno}: transact site {s.qualname!r} "
                     f"not registered in EXPECTED_TOKEN_KIND (fail-closed). "
                     f"Decide fresh vs stable per A5, then register.")
            continue
        tok = next(kw.value for kw in s.call.keywords
                   if kw.arg == "ClientRequestToken")
        kind = classify_token_expr(tok, enclosing_function(s.call))
        if kind == "constant":
            v.append(f"{s.module}:{s.lineno} {s.qualname}: hard-coded "
                     f"ClientRequestToken -- every call dedupes against "
                     f"every other call. Certainly a bug.")
        elif kind == "unknown":
            v.append(f"{s.module}:{s.lineno} {s.qualname}: could not "
                     f"statically classify token as fresh/stable "
                     f"(expected {expected!r}). Simplify the expression or "
                     f"cover this site in the runtime A5 contract test, then "
                     f"update the registry.")
        else:
            # `expected` may be a single kind or a set/tuple of allowed kinds
            # (a qualname with >1 transact site can legitimately mix fresh +
            # stable — e.g. settle mints a fresh primary token AND a derived
            # stable token for its settled-only fallback).
            allowed = {expected} if isinstance(expected, str) else set(expected)
            if kind not in allowed:
                v.append(f"{s.module}:{s.lineno} {s.qualname}: token classified "
                         f"{kind!r} but A5 requires one of {sorted(allowed)!r} "
                         f"here. (reserve=fresh per attempt; settle=stable for "
                         f"dedupe.)")
    return v

# ------------------------------------------------ attribute_exists latches

def transact_item_dicts(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for k, val in zip(node.keys, node.values):
                if (isinstance(k, ast.Constant)
                        and k.value in ("Put", "Update", "Delete",
                                        "ConditionCheck")
                        and isinstance(val, ast.Dict)):
                    yield k.value, val, node.lineno


def check_delete_conditions(tree, module):
    """Every transact Delete item must carry an attribute_exists condition
    (hold delete / reaper idempotency latch)."""
    v = []
    for kind, d, lineno in transact_item_dicts(tree):
        if kind != "Delete":
            continue
        cond = None
        for k, val in zip(d.keys, d.values):
            if isinstance(k, ast.Constant) and k.value == "ConditionExpression":
                cond = val
        if cond is None:
            # The condition may be attached after the literal via a subscript
            # assign (`item["Delete"]["ConditionExpression"] = "attribute_exists(sk)"`).
            # Accept that iff the ENCLOSING function contains an
            # attribute_exists(...) literal targeting the Delete — otherwise it
            # is a genuinely unconditioned delete.
            func = enclosing_function(d)
            func_strs = string_constants(func) if func is not None else []
            if any("attribute_exists" in s for s in func_strs):
                continue
            v.append(f"{module}:{lineno}: transact Delete item has NO "
                     f"ConditionExpression (and no attribute_exists in the "
                     f"enclosing builder). The settle-once / reaper "
                     f"idempotency proofs require attribute_exists(...) as "
                     f"the latch.")
        elif not any("attribute_exists" in s for s in string_constants(cond)):
            v.append(f"{module}:{lineno}: transact Delete ConditionExpression "
                     f"has no literal attribute_exists(...). If the condition "
                     f"is built indirectly, inline the literal so the guard "
                     f"can see it (fail-closed).")
    return v


def check_required_conditions(tree, module, requirements):
    """requirements: {qualname: [substr, ...]} -- each substring must appear
    in some string constant inside that function (e.g. the reserve CAS must
    still pin pool_reserved_microusd)."""
    v = []
    by_qualname = {}
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            qn = enclosing_qualname(n)
            qn = f"{qn}.{n.name}" if qn != "<module>" else n.name
            by_qualname[qn] = n
    for qn, substrs in requirements.items():
        func = by_qualname.get(qn)
        if func is None:
            v.append(f"{module}: required-condition function {qn!r} not "
                     f"found -- renamed or moved? Re-review the condition "
                     f"and update REQUIRED_CONDITIONS (fail-closed).")
            continue
        strings = string_constants(func)
        for sub in substrs:
            if not any(sub in s for s in strings):
                v.append(f"{module}: function {qn!r} no longer contains "
                         f"required condition fragment {sub!r}. The CAS/"
                         f"idempotency guard the proof depends on may have "
                         f"been dropped.")
    return v

# ---------------------------------------------------------------- pipeline

def analyze_module(source, module, *, allowed_sites, preserving_puts,
                   counter_registry, readonly_counter_updates=frozenset()):
    tree = load(source)
    sites, hard = collect_write_sites(tree, module)
    counter_funcs = counter_touching_qualnames(tree)
    v = list(hard)
    v += check_nontransactional_counter_writes(
        sites, counter_funcs, module, preserving_puts,
        readonly_counter_updates)
    v += check_write_inventory(sites, allowed_sites)
    v += check_counter_registry(counter_funcs, counter_registry, module)
    v += check_no_budget_row_deletion(tree, module, sites)
    v += check_transact_tokens(sites, EXPECTED_TOKEN_KIND.get(module, {}))
    v += check_delete_conditions(tree, module)
    v += check_required_conditions(tree, module,
                                   REQUIRED_CONDITIONS.get(module, {}))
    v += dynamic_counter_name_suspects(tree, module)
    return v


# ------------------------------------------------------------------ config
# analyze_module's signature is intentionally frozen (three registries that
# every caller must think about). The two remaining registries are keyed by
# module path and are set by the test-suite before analyze_module runs:
#
#   billing_guards.REQUIRED_CONDITIONS[module]  = {qualname: [substr, ...]}
#   billing_guards.EXPECTED_TOKEN_KIND[module]  = {qualname: "fresh"
#                                                            | "stable:<name>"
#                                                            | "derived:<fmt>"}
#
# Fail-closed note: a module absent from these dicts contributes *no*
# expectations here -- the test-suite's unknown-module test is what makes
# that safe (any file touching the budgets table must be in SCANNED_FILES).
REQUIRED_CONDITIONS: dict = {}
EXPECTED_TOKEN_KIND: dict = {}
