"""The claim documents name the storage the console keeps its sign-in token in.

`frontend/src/lib/cognito.ts` writes the token to one Web Storage area and reads it
back from the same one. Every paragraph of a document the claims lint covers
(`contracts/claims/config.json`) that mentions a token and names a storage area must
name that one and no other.
"""
from __future__ import annotations

import json
import pathlib
import re

REPO = pathlib.Path(__file__).resolve().parents[2]
COGNITO = REPO / "frontend" / "src" / "lib" / "cognito.ts"
CLAIMS_CONFIG = REPO / "contracts" / "claims" / "config.json"
TOKEN = re.compile(r"(?<![A-Za-z])tokens?(?![A-Za-z])", re.I)
STORAGE = re.compile(r"\b(local|session)[ -]?storage\b", re.I)


def _storage_the_code_uses() -> str:
    src = re.sub(r"//[^\n]*|/\*.*?\*/", "", COGNITO.read_text(encoding="utf-8"), flags=re.S)

    def used(call: str) -> set[str]:
        return {m.lower() for m in re.findall(rf"\b(local|session)Storage\.{call}\(\s*TOKEN_STORAGE_KEY", src)}

    writes, reads = used("setItem"), used("getItem")
    assert len(writes) == 1 and reads == writes, (
        f"cognito.ts writes the token to {sorted(writes)} and reads it from {sorted(reads)}; "
        "expected one storage for both")
    return writes.pop()


def _wrong(text: str, used: str) -> list[str]:
    """Paragraphs that mention a token and name a storage set other than {used}."""
    out = []
    for para in re.split(r"\n\s*\n", text):
        flat = re.sub(r"\s+", " ", para)
        named = {m.group(1).lower() for m in STORAGE.finditer(flat)}
        if TOKEN.search(flat) and named and named != {used}:
            m = next(x for x in STORAGE.finditer(flat) if x.group(1).lower() != used)
            out.append(flat[max(0, m.start() - 120):m.end() + 40])
    return out


def _documents() -> list[pathlib.Path]:
    return [REPO / d for d in json.loads(CLAIMS_CONFIG.read_text())["covered_documents"]]


def test_every_paragraph_about_the_token_names_the_storage_the_code_uses():
    used = _storage_the_code_uses()
    wrong = [f"{p.relative_to(REPO)}: ...{w}..." for p in _documents()
             for w in _wrong(p.read_text(encoding="utf-8"), used)]
    assert wrong == [], f"these paragraphs name a token storage other than {used}Storage: {wrong}"
    checked = [p for p in _documents() for para in re.split(r"\n\s*\n", p.read_text(encoding="utf-8"))
               if TOKEN.search(para) and STORAGE.search(para)]
    assert checked, "no covered paragraph names a token storage; the check is not seeing the documents"


def test_the_check_can_fail():
    old = ("(2) the Cognito Hosted UI callback with PKCE, and (3) a previously saved token\n"
           "in `localStorage`. The backend enforces `token_use=access`.")
    assert _wrong(old, "session")
    assert _wrong("The SPA keeps `access_token` in local storage.", "session")
    assert _wrong("Tokens go to `localStorage`; `sessionStorage` holds the PKCE verifier.", "session")
    assert not _wrong("(3) a token bundle read back from `sessionStorage`.", "session")
    assert not _wrong("The language choice is kept in `localStorage`.", "session")
