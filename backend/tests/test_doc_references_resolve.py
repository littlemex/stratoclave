"""A document this repository cites has to exist in this repository.

Forty-four code comments cited `CONTRACT-hard-ceiling.md` as the normative source
for the dollar ceiling, and that file was not in the repository — it had been
written outside it and never published. Nothing failed, because nothing checks. A
reader following the citation found nothing, and the rules the code says it
implements were unreviewable.

The same class of defect covers the other direction: a documentation link that
points at a file someone renamed or removed. Both are cheap to check and invisible
otherwise, so they are checked here.

A citation by TEST NAME rots the same way and is worse, because it is the form the
evidence document uses to say a claim is verified. Three had already rotted:
`docs/EVIDENCE.md` cited a Z3 proof of "a version read after its rows cannot dangle"
under a name no test has, and a monotonicity proof under a name that was pluralised
away, while a docstring pointed at a reaper invariant without the `reaper_` in its
name. Each read as evidence and resolved to nothing.

Two deliberate limits. This looks at paths, not at content: a citation can point at
the right file and describe it wrongly, and only a reader catches that. And it
resolves `docs/...` paths from the repository root, because that is the form a
citation should take — a bare filename is ambiguous the moment two directories hold
the same name, which is exactly how `pending-protocol.md` came to be cited without
its directory.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Directories that are not ours to police.
IGNORED = {"node_modules", ".git", "__pycache__", "build", "dist", "cdk.out", ".venv"}

#: Where citations may appear. Markdown is checked separately, as links.
CODE_SUFFIXES = {".py", ".ts", ".tsx", ".rs", ".sh", ".yml", ".yaml"}

#: A repo-relative documentation path, the form a citation should take.
DOC_PATH = re.compile(r'\b(docs/[A-Za-z0-9_./-]+\.md)\b')

#: A bare `Something.md` with no directory. Allowed only for names that exist at the
#: repository root (README.md, SECURITY.md …) or beside the file that cites them.
BARE_MD = re.compile(r'(?<![/\w-])([A-Za-z][A-Za-z0-9_-]*\.md)\b')

#: Names that are templates or examples rather than references to a real file.
TEMPLATE_NAMES = {"_ja.md", "template.md", "NAME.md", "example.md"}

#: A cited test function. Long enough not to match a fixture called `test_x`, and
#: required not to end in `_` so a deliberately truncated family reference
#: (`test_inv2_`) is read as the prefix it is rather than as a name.
TEST_NAME = re.compile(r'\btest_[a-z0-9]+(?:_[a-z0-9]+)+\b')


#: This file names examples of the very citations it rejects, so it cannot scan
#: itself. Any other exclusion would be a hole.
SELF = Path(__file__).resolve()


def _repo_files(suffixes: set[str]) -> list[Path]:
    return [
        p for p in REPO_ROOT.rglob("*")
        if p.is_file()
        and p.suffix in suffixes
        and p.resolve() != SELF
        and not any(part in IGNORED for part in p.relative_to(REPO_ROOT).parts)
    ]


def _md_basenames() -> set[str]:
    return {
        p.name for p in REPO_ROOT.rglob("*.md")
        if not any(part in IGNORED for part in p.relative_to(REPO_ROOT).parts)
    }


def test_every_cited_docs_path_exists():
    """A `docs/...md` citation in code must resolve from the repository root."""
    missing: list[str] = []
    for path in _repo_files(CODE_SUFFIXES):
        rel = path.relative_to(REPO_ROOT)
        for match in DOC_PATH.finditer(path.read_text(errors="replace")):
            cited = match.group(1)
            if not (REPO_ROOT / cited).exists():
                missing.append(f"{rel} cites {cited}")
    assert not missing, (
        "code cites documents that are not in this repository:\n  "
        + "\n  ".join(sorted(set(missing)))
        + "\n\nPublish the document, or cite the one that replaced it."
    )


def test_no_code_cites_a_document_that_exists_nowhere():
    """A bare `Something.md` in code must at least name a file that exists.

    Catches the shape the hard-ceiling contract had: a confident citation, by name,
    of a document that lived only on the author's machine.
    """
    known = _md_basenames()
    orphans: list[str] = []
    for path in _repo_files(CODE_SUFFIXES):
        rel = path.relative_to(REPO_ROOT)
        for match in BARE_MD.finditer(path.read_text(errors="replace")):
            name = match.group(1)
            if name in TEMPLATE_NAMES or name in known:
                continue
            orphans.append(f"{rel} cites {name}")
    assert not orphans, (
        "code cites documents that exist nowhere in this repository:\n  "
        + "\n  ".join(sorted(set(orphans)))
        + "\n\nA citation is a promise that a reader can follow it."
    )


def _markdown_links() -> list[tuple[Path, str]]:
    link = re.compile(r'\[[^\]]*\]\(([^)\s]+)(?:\s+"[^"]*")?\)')
    out: list[tuple[Path, str]] = []
    for path in _repo_files({".md"}):
        for match in link.finditer(path.read_text(errors="replace")):
            out.append((path, match.group(1)))
    return out


def test_every_relative_documentation_link_resolves():
    broken: list[str] = []
    for path, target in _markdown_links():
        if target.startswith(("http://", "https://", "mailto:", "#")):
            continue
        rel_target = target.split("#", 1)[0]
        if not rel_target:
            continue
        if not (path.parent / rel_target).resolve().exists():
            broken.append(f"{path.relative_to(REPO_ROOT)} -> {target}")
    assert not broken, (
        "documentation links that do not resolve:\n  " + "\n  ".join(sorted(broken))
    )


@pytest.mark.parametrize("doc", [
    "docs/design/hard-ceiling.md",
    "docs/design/calibrated-mode.md",
    "docs/design/charge-loss.md",
])
def test_a_published_contract_says_where_it_stands(doc):
    """A contract is normative for a behaviour, not a claim that it is switched on.

    Publishing the design without its status invites the opposite reading, which is
    the failure mode of a specification kept next to the code: a reader takes
    "the ceiling binds" for a description of today rather than of the contract.
    """
    text = (REPO_ROOT / doc).read_text()
    assert "## Status in the shipped code" in text, (
        f"{doc} has no status section, so a reader cannot tell which of its rules "
        "are switched on"
    )


def _defined_test_names() -> set[str]:
    """Every test this repository defines, in both suites.

    The Rust CLI's `#[test] fn` names are included because a Python docstring may
    legitimately cite one, and a check that flagged those would be noise a reader
    learns to ignore — which is how a guard stops guarding.
    """
    names: set[str] = set()
    for path in REPO_ROOT.rglob("*.py"):
        if any(part in IGNORED for part in path.relative_to(REPO_ROOT).parts):
            continue
        names |= set(re.findall(
            r'^\s*(?:async\s+)?def\s+(test_[A-Za-z0-9_]+)', path.read_text(errors="replace"), re.M,
        ))
    for path in REPO_ROOT.rglob("*.rs"):
        if any(part in IGNORED for part in path.relative_to(REPO_ROOT).parts):
            continue
        names |= set(re.findall(
            r'fn\s+(test_[A-Za-z0-9_]+)', path.read_text(errors="replace"),
        ))
    return names


def _test_module_stems() -> set[str]:
    return {
        p.stem for p in REPO_ROOT.rglob("test_*.py")
        if not any(part in IGNORED for part in p.relative_to(REPO_ROOT).parts)
    }


def test_every_cited_test_name_exists():
    """A claim that names its own proof has to name one that exists.

    This is the citation form `docs/EVIDENCE.md` is built out of, so a rotted name
    there is a claim of verification with nothing behind it. Renaming a test is
    normal and fine — leaving a document asserting the old name is what this fails
    on.
    """
    defined = _defined_test_names()
    modules = _test_module_stems()
    dangling: dict[str, set[str]] = {}
    for path in _repo_files(CODE_SUFFIXES | {".md"}):
        text = path.read_text(errors="replace")
        for name in set(TEST_NAME.findall(text)):
            if name in defined or name in modules:
                continue
            # `test_foo.py` is a module reference rather than a function name. The
            # path checks cover `.md` citations, NOT `.py` ones, so skipping it
            # outright let a citation to a deleted test MODULE through — which is
            # how `docs/design/CONTRACTS.md` could have named a test file that does
            # not exist and still read as enforced. Check the module instead of
            # exempting it.
            if re.search(re.escape(name) + r'\.py', text):
                if name not in modules:
                    dangling.setdefault(name + ".py", set()).add(
                        str(path.relative_to(REPO_ROOT)))
                continue
            dangling.setdefault(name, set()).add(str(path.relative_to(REPO_ROOT)))
    assert not dangling, (
        "citations to tests that do not exist:\n  "
        + "\n  ".join(
            f"{name} — cited in {', '.join(sorted(where))}"
            for name, where in sorted(dangling.items())
        )
        + "\n\nRename the citation to the test that replaced it, or write the test."
    )
