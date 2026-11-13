"""C13.5: this gateway serves one OpenAI-compatible surface, `bedrock-runtime`'s
`/openai/v1`. `bedrock-mantle` is not a supported transport.

`bedrock-mantle` has its own endpoint, its own IAM action set
(`bedrock-mantle:*`) and its own quota (`iac/lib/ecs-stack.ts`'s migration
comment records the move). A future change could plausibly propose calling it
again — "it already exists, let's also use it" is a cheap-sounding idea whose
real cost (a second transport with different observability, see
`mvp/_openai_transport.py`'s module comment) this clause is what makes someone
read before doing it.

Two independent checks, because they fail for different reasons:

- `base_url` is checked structurally (its AST, not a sample of regions), so no
  call it could ever produce reaches anywhere but `bedrock-runtime`.
- Every source and prose file this sweep covers is scanned, line by line, for
  the name itself. A comment or a docstring that explains history (this file,
  `_openai_transport.py`, `ecs-stack.ts`) is allowed, counted, and pinned by
  count so a new mention still has to be noticed; a name reachable from code —
  a string literal, an identifier, an f-string piece, raw prose in a document
  that is not one of the named exceptions — is never allowed.

**Named residuals.** Nothing here proves `async_client`/`sync_client` actually
call `base_url` rather than a hand-built host; a second, differently-named
transport (not called `bedrock-mantle`) would not be caught by a sweep for this
one name; and the sweep is literal and line-based, so a host assembled by
concatenating two string literals that are each, alone, clean (`"bedrock-" +
"mantle"`), or split across two physical lines, is not seen. That shape has no
legitimate reason to exist in this codebase and would itself be a strange thing
to write by accident.
"""
from __future__ import annotations

import ast
import pathlib
import re

from mvp import _openai_transport as transport

REPO = pathlib.Path(__file__).resolve().parents[2]
BACKEND = REPO / "backend"
EXCLUDE_DIR_NAMES = {".venv", "venv", "site-packages", "node_modules", "build",
                     "__pycache__", "cdk.out", "dist", ".git"}

#: Markdown files allowed to name `bedrock-mantle`: the changelog recording this
#: change, the contract clause stating it is unsupported, and the upstream
#: pricing-dimension document that quotes AWS's own (unrelated) naming collision.
MD_EXCEPTIONS = {"CHANGELOG.md", "docs/design/CONTRACTS.md", "docs/design/price-feeds.md"}

#: file -> how many protected (comment or docstring) lines in it are allowed to
#: name the retired transport as history. Pinned by count rather than by line
#: position, so an unrelated edit elsewhere in the file does not make this test
#: fail for the wrong reason; a count that no longer matches means a mention was
#: added or removed, either of which this test should see.
HISTORY_LINE_COUNT: dict[str, int] = {
    "backend/mvp/_openai_transport.py": 4,
    "iac/lib/ecs-stack.ts": 5,
}


def _excluded(path: pathlib.Path) -> bool:
    return bool(EXCLUDE_DIR_NAMES & set(path.parts))


def _docstring_line_ranges(source: str) -> set[int]:
    """Every line number inside a module, class or function docstring: its first
    statement, when that is a bare string expression."""
    lines: set[int] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.body and isinstance(node.body[0], ast.Expr) \
                and isinstance(node.body[0].value, ast.Constant) and isinstance(node.body[0].value.value, str):
            c = node.body[0].value
            lines.update(range(c.lineno, getattr(c, "end_lineno", c.lineno) + 1))
    return lines


_HASH_COMMENT = re.compile(r"^\s*#")
_SLASH_COMMENT = re.compile(r"^\s*//|^\s*\*(?!/)")


def _protected_lines(path: pathlib.Path, text: str) -> set[int]:
    """Line numbers this sweep allows to name the transport as history."""
    if path.suffix == ".py":
        protected = _docstring_line_ranges(text)
        protected.update(i for i, line in enumerate(text.splitlines(), 1) if _HASH_COMMENT.match(line))
        return protected
    if path.suffix in (".sh",):
        return {i for i, line in enumerate(text.splitlines(), 1) if _HASH_COMMENT.match(line)}
    if path.suffix in (".ts", ".rs"):
        return {i for i, line in enumerate(text.splitlines(), 1) if _SLASH_COMMENT.match(line)}
    return set()  # .json, .txt, etc: no comment syntax, nothing is protected


def _scan(paths: list[pathlib.Path], needle_re: "re.Pattern[str]") -> tuple[list[str], dict[str, int]]:
    """(offender `file:line` strings, {file: count of protected hits})."""
    offenders: list[str] = []
    protected_hits: dict[str, int] = {}
    for path in paths:
        text = path.read_text(encoding="utf-8")
        protected = _protected_lines(path, text)
        try:
            rel = path.relative_to(REPO).as_posix()
        except ValueError:
            rel = str(path)  # outside REPO: a synthetic fixture under tmp_path
        for lineno, line in enumerate(text.splitlines(), 1):
            if not needle_re.search(line):
                continue
            if lineno in protected:
                protected_hits[rel] = protected_hits.get(rel, 0) + 1
            else:
                offenders.append(f"{rel}:{lineno}")
    return offenders, protected_hits


NEEDLE = re.compile(re.escape("bedrock-mantle"))
BARE_MANTLE = re.compile(r"(?<!dis)mantle", re.IGNORECASE)


def _own_body_source(fn) -> str:
    """A function's body with any leading docstring stripped, re-rendered so two
    functions that do the same thing in different formatting compare equal."""
    body = fn.body
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
            and isinstance(body[0].value.value, str):
        body = body[1:]
    return "\n".join(ast.dump(s) for s in body)


def test_base_url_has_exactly_the_body_that_makes_it_a_pure_function_of_region():
    """Not a sample of regions: the function's AST, so a branch, an env read or a
    second template anywhere in its body is caught regardless of which region
    would have exposed it. `_ENDPOINT_TEMPLATE` itself is checked the same way —
    a module-level `_ENDPOINT_TEMPLATE = os.environ.get("X", "https://...")` would
    pass the value check below in a test environment with that variable unset, so
    the assignment's own shape (one assignment, a bare string constant) is what
    actually rules that out."""
    tree = ast.parse(pathlib.Path(transport.__file__).read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "base_url")
    expected = ast.parse("def f(region):\n    return _ENDPOINT_TEMPLATE.format(region=region)").body[0]
    assert fn.decorator_list == []
    assert _own_body_source(fn) == _own_body_source(expected)

    assignments = [n for n in ast.walk(tree) if isinstance(n, ast.Assign)
                   and any(isinstance(t, ast.Name) and t.id == "_ENDPOINT_TEMPLATE" for t in n.targets)]
    assert len(assignments) == 1, assignments
    value = assignments[0].value
    assert isinstance(value, ast.Constant) and isinstance(value.value, str), ast.dump(value)
    assert value.value == "https://bedrock-runtime.{region}.amazonaws.com/openai/v1"
    assert transport._ENDPOINT_TEMPLATE == value.value


def test_base_url_resolves_to_that_template_for_an_unregistered_region_too():
    """A behavioural pin beside the structural one above: even a region this
    deployment has never heard of still resolves to `bedrock-runtime`, never to
    a fallback host."""
    assert transport.base_url("nowhere-9") == "https://bedrock-runtime.nowhere-9.amazonaws.com/openai/v1"


def test_scan_protects_a_comment_and_a_docstring_and_catches_everything_else(tmp_path):
    """Pins what `_scan` does on text it has never seen, independent of any real
    file in the tree. Written under `tmp_path` rather than into `tests/`, so an
    xdist worker never sees a half-written fixture and a killed run leaves
    nothing behind."""
    py = (
        'def f():\n'
        '    """mentions bedrock-mantle for history only."""\n'
        '    # also mentions bedrock-mantle in a comment\n'
        '    x = "bedrock-mantle"\n'
        '    return f"https://{x}.example/v1"\n'
    )
    tmp = tmp_path / "_scan_fixture.py"
    tmp.write_text(py, encoding="utf-8")
    offenders, protected = _scan([tmp], NEEDLE)
    assert offenders == [f"{tmp}:4"]
    assert protected == {str(tmp): 2}


def test_no_python_source_under_backend_or_scripts_names_bedrock_mantle_outside_history():
    """Covers all of `backend/` (production code and its own local-dev scripts)
    and the repository's `scripts/` (local-stack helpers), excluding the test
    suite itself, which is allowed to quote the name for exactly the purpose
    this file does. A `#` comment or a docstring may name the hyphenated form as
    history, up to the pinned count; anywhere else the literal text
    `bedrock-mantle` appears on a line — a string literal, an f-string piece, or
    a bare identifier built from it, such as a reintroduced `_mantle_transport` —
    it may not, regardless of count. (A Python identifier cannot itself contain
    a hyphen, so this is a textual check on the source line, not a check that no
    Python *name* could ever evoke the retired transport under a different
    spelling; that residual is named at the top of this file.)"""
    paths = []
    for root, exclude in ((BACKEND, "tests"), (REPO / "scripts", None)):
        for path in sorted(root.rglob("*.py")):
            if _excluded(path) or (exclude and exclude in path.relative_to(root).parts):
                continue
            paths.append(path)
    assert len(paths) > 50, f"the sweep found only {len(paths)} .py files; it is not seeing the tree"
    offenders, protected = _scan(paths, NEEDLE)
    assert offenders == [], offenders
    assert protected == {"backend/mvp/_openai_transport.py": HISTORY_LINE_COUNT["backend/mvp/_openai_transport.py"]}, (
        f"history comments/docstrings naming bedrock-mantle changed: {protected}; update "
        f"HISTORY_LINE_COUNT if this is a reviewed addition or removal")


def test_no_shell_json_or_requirements_source_names_bedrock_mantle():
    """`.sh` (`#` comments allowed as history). `.json` and `.txt` (including
    `requirements.txt`) have no protected lines at all here — even though pip
    reads a `#` in `requirements.txt` as a comment, nothing in this codebase has
    a reason to name the retired transport there, so any mention is a defect,
    never history."""
    paths = []
    for root, exclude in ((BACKEND, "tests"), (REPO / "scripts", None)):
        for ext in ("*.sh", "*.json", "*.txt"):
            for path in sorted(root.rglob(ext)):
                if _excluded(path) or (exclude and exclude in path.relative_to(root).parts):
                    continue
                paths.append(path)
    assert len(paths) > 0, "the sweep found no .sh/.json files; it is not seeing the tree"
    offenders, protected = _scan(paths, NEEDLE)
    assert offenders == [], offenders
    assert protected == {}, protected


def test_no_ts_or_rust_source_names_the_retired_transport_outside_history():
    """The bare word `mantle`, case-insensitive, not only the hyphenated form:
    `iac/` and `cli/src/` have no other product or vendor by that name (checked
    against `docs/design/price-feeds.md`'s upstream naming collision, which is
    a Python-side pricing concern and does not reach these trees), so the
    broader match catches an identifier like `_mantle_transport` or a bare
    "mantle clients" that the strict needle would miss."""
    for root, name in ((REPO / "iac", "iac"), (REPO / "cli" / "src", "cli/src")):
        pattern = "*.ts" if name == "iac" else "*.rs"
        paths = [p for p in sorted(root.rglob(pattern))
                 if not _excluded(p)]
        assert len(paths) > 0, f"the sweep found no source files under {name}; it is not seeing the tree"
        offenders, protected = _scan(paths, BARE_MANTLE)
        assert offenders == [], offenders
        expected = {k: v for k, v in HISTORY_LINE_COUNT.items() if k.startswith(name + "/")}
        assert protected == expected, (
            f"comment mentions of 'mantle' under {name} changed: {protected}; update "
            f"HISTORY_LINE_COUNT if this is a reviewed addition or removal")


def test_no_markdown_document_outside_the_named_exceptions_names_the_retired_transport():
    """Issue #246's harmful kind: a document stating what `bedrock-mantle`
    cannot do, read as a fact about a transport that is still live.
    `docs/EVIDENCE.md` and `docs/MEASUREMENTS.md`, together with its Japanese
    translation (same basename, a locale segment inserted before the
    extension), are two of the files this check covers; every other Markdown
    document in the repository is covered too, except `MD_EXCEPTIONS`. Matched
    on the bare word, not only the hyphenated form, for the same reason as the
    TypeScript/Rust sweep."""
    offenders = []
    scanned = 0
    for path in sorted(REPO.rglob("*.md")):
        if _excluded(path):
            continue
        rel = path.relative_to(REPO).as_posix()
        if rel in MD_EXCEPTIONS:
            continue
        scanned += 1
        if BARE_MANTLE.search(path.read_text(encoding="utf-8")):
            offenders.append(rel)
    assert scanned > 40, f"the sweep found only {scanned} .md files; it is not seeing the tree"
    assert offenders == [], offenders


def test_the_markdown_exceptions_actually_name_it_and_still_exist():
    """A stale exception (the file was renamed, or no longer names the transport
    at all) would silently stop meaning anything."""
    for rel in MD_EXCEPTIONS:
        path = REPO / rel
        assert path.is_file(), rel
        # BARE_MANTLE matches a strict superset of NEEDLE, so checking it alone is enough.
        assert BARE_MANTLE.search(path.read_text(encoding="utf-8")), rel
