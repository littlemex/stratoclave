"""C12.7: the per-request usage row's attribute set is pinned (issue #237).

The gateway answers "where do our prompts go" with "not in the usage row",
because the one per-request record it keeps for accounting carries counts, ids,
a cost figure and a one-way email digest, and there is no attribute for a body.
A property nobody checks is one the next change can remove, so the set of
attributes the row's one writer can produce is pinned below, and every table the
deployment creates has to be named in `docs/design/request-bodies.md`, which
says what each holds.

What this checks, and what it does not:

- It is a tripwire, not a body detector. It fails on ANY new attribute, a
  harmless one included, so that every addition is looked at by a person, and
  the failure names the attribute.
- It checks attribute NAMES. Several values are strings a caller or a
  provider influences (`requested_model_id`, `task_tag`, the reason codes);
  that they are short codes is how the callers are written, not something the
  writer enforces. That is why C12.7 is rated B.
- The static reader fails closed: any use of `item` inside `record` that it
  does not recognise (an `update`, a spread, a computed key) fails the test.
- The one-writer check follows the repository instance and its table handle
  through names and attributes in each module. A repository passed in as a
  function parameter is not followed.

`ttl` is in the set because the writer writes it. It is operational, not a
fact about the request, and an export of these rows (#235) may leave it out;
the export's column list is a different list.
"""
from __future__ import annotations

import ast
import inspect
import pathlib
import re
import time

import pytest

BACKEND = pathlib.Path(__file__).resolve().parents[1]
REPO = BACKEND.parent
USAGE_LOGS = BACKEND / "dynamo" / "usage_logs.py"
DYNAMODB_STACK = REPO / "iac" / "lib" / "dynamodb-stack.ts"
REQUEST_BODIES_DOC = REPO / "docs" / "design" / "request-bodies.md"

#: Every directory under `backend/` that holds Python, and whether this file sweeps it.
SOURCE_PACKAGES = frozenset({"mvp", "dynamo", "core", "billing", "bootstrap", "middleware",
                             "migrations", "scripts"})
NOT_SWEPT = frozenset({"tests"})
SOURCE_MODULES = ("main.py", "limits.py")

#: What a module other than the repository may do with the usage table handle.
READ_METHODS = frozenset({"query", "get_item", "scan", "name"})

#: Every attribute `UsageLogsRepository.record` can write, and what it is.
USAGE_ROW_ATTRIBUTES: dict[str, str] = {
    "tenant_id": "partition key",
    "timestamp_log_id": "sort key: ISO time and the request id",
    "user_id": "the gateway's own user id",
    "user_email_hash": "one-way pii: digest of the email (C12.1)",
    "model_id": "the model that served the request",
    "requested_model_id": "the model the caller asked for",
    "input_tokens": "count",
    "output_tokens": "count",
    "total_tokens": "count",
    "cache_read_tokens": "count, when the provider reported it",
    "cache_write_tokens": "count, when the provider reported it",
    "cost_microusd": "the settled charge",
    "measured_bound_microusd": "the reservation bound the request was priced at",
    "fallback_reason": "a reason code",
    "metering_fault": "a reason code",
    "task_tag": "caller-asserted tag, at most 64 characters of the id grammar",
    "task_tag_source": "how the tag was resolved, a code",
    "recorded_at": "write time",
    "request_id": "the gateway's correlation id",
    "ttl": "operational: expiry, 90 days after write",
}

_PUT_ANCHOR = "        self._table.put_item(Item=item)"


def _record(tree: ast.AST) -> ast.FunctionDef:
    cls = next(n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == "UsageLogsRepository")
    return next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "record")


def _written_attributes(fn: ast.FunctionDef) -> tuple[set[str], list[str]]:
    """(attribute names `record` writes, uses of `item` this reader does not recognise).

    Recognised: the one `item: ... = {<str constants>: ...}` literal, `item["const"]
    = ...`, `Item=item`, and `return item`. Anything else is reported, so a write
    shape this reader does not understand fails instead of passing."""
    names: set[str] = set()
    unrecognised: list[str] = []
    recognised: set[int] = set()
    for node in ast.walk(fn):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in targets:
                if isinstance(t, ast.Name) and t.id == "item":
                    recognised.add(id(t))
                    if not isinstance(node.value, ast.Dict) or any(
                            not (isinstance(k, ast.Constant) and isinstance(k.value, str))
                            for k in node.value.keys):
                        unrecognised.append(f"line {node.lineno}: item built from something other "
                                            "than a dict literal of string keys")
                    else:
                        names |= {k.value for k in node.value.keys}
                if isinstance(t, ast.Subscript) and isinstance(t.value, ast.Name) and t.value.id == "item":
                    recognised.add(id(t.value))
                    if isinstance(t.slice, ast.Constant) and isinstance(t.slice.value, str):
                        names.add(t.slice.value)
                    else:
                        unrecognised.append(f"line {node.lineno}: item[...] with a computed key")
        if isinstance(node, ast.keyword) and node.arg == "Item" and isinstance(node.value, ast.Name) \
                and node.value.id == "item":
            recognised.add(id(node.value))
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Name) and node.value.id == "item":
            recognised.add(id(node.value))
    for node in ast.walk(fn):
        if isinstance(node, ast.Name) and node.id == "item" and id(node) not in recognised:
            unrecognised.append(f"line {node.lineno}: item used in a way this reader does not read")
    return names, unrecognised


def test_the_writer_can_produce_exactly_the_pinned_attributes():
    written, unrecognised = _written_attributes(_record(ast.parse(USAGE_LOGS.read_text(encoding="utf-8"))))
    assert unrecognised == [], unrecognised
    added = sorted(written - set(USAGE_ROW_ATTRIBUTES))
    removed = sorted(set(USAGE_ROW_ATTRIBUTES) - written)
    assert not added, (
        f"the usage row gained {added}. If none of them can hold request or response "
        "content, add each to USAGE_ROW_ATTRIBUTES with what it is; if one can, the "
        "gateway would start holding bodies in this row (C12.7) and that is a design "
        "change, not a test update")
    assert not removed, f"USAGE_ROW_ATTRIBUTES names attributes the writer no longer writes: {removed}"


#: A value for every keyword `record` accepts. Kept complete by the test below.
_EVERY_ARGUMENT = dict(
    tenant_id="t-1", user_id="u-1", user_email="u@example.com", model_id="m",
    input_tokens=3, output_tokens=4, cache_read_tokens=1, cache_write_tokens=2,
    request_id="req-1", cost_microusd=5, requested_model_id="m-req",
    measured_bound_microusd=6, fallback_reason="quota_exhausted",
    metering_fault="no_final_usage", task_tag="build-42", task_tag_source="header")


def test_the_dynamic_check_passes_every_argument_record_accepts():
    from dynamo.usage_logs import UsageLogsRepository

    params = inspect.signature(UsageLogsRepository.record).parameters
    assert not [p for p in params.values()
                if p.kind in (p.VAR_KEYWORD, p.VAR_POSITIONAL)], "record takes **kwargs or *args"
    assert set(params) - {"self"} == set(_EVERY_ARGUMENT), (
        "record's signature changed; give the new parameter a value in _EVERY_ARGUMENT")


def test_a_row_written_with_every_argument_has_only_pinned_attributes(dynamodb_mock):
    """The same property through the real writer and the real table, plus the
    90-day expiry `request-bodies.md` states."""
    from dynamo.usage_logs import UsageLogsRepository

    repo = UsageLogsRepository()
    before = time.time()
    item = repo.record(**_EVERY_ARGUMENT)
    stored = dynamodb_mock.Table(repo._table.name).get_item(
        Key={"tenant_id": "t-1", "timestamp_log_id": item["timestamp_log_id"]})["Item"]
    assert set(stored) == set(USAGE_ROW_ATTRIBUTES), sorted(set(stored) ^ set(USAGE_ROW_ATTRIBUTES))
    assert abs(int(stored["ttl"]) - (before + 90 * 86400)) < 120


@pytest.mark.parametrize("injected, expected", [
    ('item["prompt"] = "..."', "names"),
    ('item.update(prompt="...")', "unrecognised"),
    ('item |= {"prompt": "..."}', "unrecognised"),
    ('item[key] = "..."', "unrecognised"),
    ('item = {**item, "prompt": "..."}', "unrecognised"),
])
def test_the_reader_catches_each_way_of_adding_an_attribute(injected, expected):
    text = USAGE_LOGS.read_text(encoding="utf-8")
    assert text.count(_PUT_ANCHOR) == 1, "the injection anchor moved; update _PUT_ANCHOR"
    src = text.replace(_PUT_ANCHOR, f"        key = 'prompt'\n        {injected}\n{_PUT_ANCHOR}", 1)
    names, unrecognised = _written_attributes(_record(ast.parse(src)))
    if expected == "names":
        assert sorted(names - set(USAGE_ROW_ATTRIBUTES)) == ["prompt"]
    else:
        assert unrecognised, injected


def test_every_python_directory_under_backend_is_either_swept_or_named_as_not():
    present = {p.name for p in BACKEND.iterdir() if p.is_dir() and not p.name.startswith((".", "__"))
               and any(p.rglob("*.py"))}
    assert present <= SOURCE_PACKAGES | NOT_SWEPT, sorted(present - SOURCE_PACKAGES - NOT_SWEPT)
    assert SOURCE_PACKAGES <= present, sorted(SOURCE_PACKAGES - present)
    top = {p.name for p in BACKEND.glob("*.py")}
    assert top == set(SOURCE_MODULES), sorted(top ^ set(SOURCE_MODULES))


def _source_files():
    for package in sorted(SOURCE_PACKAGES):
        yield from sorted((BACKEND / package).rglob("*.py"))
    for name in SOURCE_MODULES:
        yield BACKEND / name


def _handle_problems(rel: str, tree: ast.AST) -> list[str]:
    """Uses of the usage table handle in a module other than the repository that
    are not reads. Follows `UsageLogsRepository(...)` and its `._table` through names
    and attributes assigned anywhere in the module."""
    parents: dict[int, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[id(child)] = node

    def makes_repo(expr) -> bool:
        return isinstance(expr, ast.Call) and getattr(expr.func, "id", getattr(expr.func, "attr", "")) \
            == "UsageLogsRepository"

    repos: set[str] = set()
    tables: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in targets:
                if makes_repo(node.value):
                    repos.add(ast.unparse(t))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
            v = node.value
            if isinstance(v, ast.Attribute) and v.attr == "_table" and (
                    makes_repo(v.value) or ast.unparse(v.value) in repos):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                tables |= {ast.unparse(t) for t in targets}

    problems = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute):
            continue
        receiver = node.value
        is_handle = (node.attr == "_table" and (makes_repo(receiver) or ast.unparse(receiver) in repos))
        if is_handle:
            parent = parents.get(id(node))
            if isinstance(parent, ast.Attribute):
                if parent.attr not in READ_METHODS:
                    problems.append(f"{rel}:{node.lineno}: ._table.{parent.attr} on the usage table")
            elif isinstance(parent, (ast.Assign, ast.AnnAssign)) and parent.value is node:
                pass  # bound to a name; its uses are checked below
            else:
                problems.append(f"{rel}:{node.lineno}: the usage table handle escapes into an expression")
        elif ast.unparse(receiver) in tables and node.attr not in READ_METHODS:
            problems.append(f"{rel}:{node.lineno}: .{node.attr} on the usage table handle")
    return problems


def test_record_is_the_only_write_to_the_usage_table():
    """One writer is what makes the pinned set the whole story."""
    tree = ast.parse(USAGE_LOGS.read_text(encoding="utf-8"))
    write_methods = {"put_item", "update_item", "delete_item", "batch_writer", "batch_write_item",
                     "transact_write_items", "execute_statement"}
    owner_of: dict[int, str] = {}
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for call in ast.walk(fn):
                owner_of.setdefault(id(call), fn.name)
    writes = [owner_of.get(id(c), "<module>") for c in ast.walk(tree)
              if isinstance(c, ast.Call) and getattr(c.func, "attr", "") in write_methods]
    assert writes == ["record"], writes

    problems = []
    for path in _source_files():
        rel = path.relative_to(BACKEND).as_posix()
        if rel == "dynamo/usage_logs.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        if rel != "dynamo/client.py" and any(
                (isinstance(n, ast.Name) and n.id == "usage_logs_table_name")
                or (isinstance(n, ast.Constant) and isinstance(n.value, str)
                    and re.fullmatch(r"[A-Za-z0-9_-]+-usage-logs", n.value))
                for n in ast.walk(tree)):
            problems.append(f"{rel}: names the usage table itself")
        problems += _handle_problems(rel, tree)
    assert problems == [], problems


@pytest.mark.parametrize("src", [
    "repo = UsageLogsRepository()\nrepo._table.put_item(Item={})\n",
    "def f():\n    table = UsageLogsRepository()._table\n    table.update_item(Key={})\n",
    "class C:\n    def __init__(self):\n        self._usage = UsageLogsRepository()\n"
    "    def f(self):\n        self._usage._table.delete_item(Key={})\n",
    "def f():\n    send(UsageLogsRepository()._table)\n",
])
def test_the_one_writer_check_sees_each_shape(src):
    assert _handle_problems("x.py", ast.parse(src)), src


def test_the_one_writer_check_allows_reads():
    src = ("repo = UsageLogsRepository()\nrepo._table.query(KeyConditionExpression=1)\n"
           "t = UsageLogsRepository()._table\nt.scan()\nname = t.name\n")
    assert _handle_problems("x.py", ast.parse(src)) == []


def test_every_table_the_deployment_creates_is_named_in_the_request_bodies_doc():
    """A new table is the likeliest place for request-derived content to land; it
    cannot be added without someone writing down what it holds."""
    stack = DYNAMODB_STACK.read_text(encoding="utf-8")
    suffixes = re.findall(r"tableName: `\$\{prefix\}-([a-z-]+)`", stack)
    assert len(suffixes) > 20, f"found only {len(suffixes)} tables; the pattern no longer matches the stack"
    doc = REQUEST_BODIES_DOC.read_text(encoding="utf-8")
    missing = sorted(s for s in set(suffixes) if f"`<prefix>-{s}`" not in doc)
    assert missing == [], f"request-bodies.md does not say what these tables hold: {missing}"
