"""The local development script creates exactly the tables the IaC declares.

`scripts/local/create_tables.py` says, in a comment above its spec list, "one entry per
`iac/lib/dynamodb-stack.ts` table". That sentence was false: the script created 23 of 24,
and the missing one — `quota-events` — is a table the backend reads. The failure it produced
was a `ResourceNotFoundException` from a route that looked unrelated to table setup, in an
environment whose whole purpose is to be a faithful local stand-in.

A comment cannot hold that invariant, so this test does. It compares the two sets in both
directions, because a table in the script that the IaC does not declare is the same class of
drift arriving from the other side: local development would then exercise a table that will
not exist once deployed.

The parse is deliberately textual rather than importing the script (which requires the
backend package and a local-endpoint guard) or synthesising the CDK app (which requires a
node toolchain in a Python test run). Both files write these names in one fixed shape, and a
change to that shape breaks this test loudly rather than silently reducing it to comparing
two empty sets — which the emptiness assertions below are what rule out.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
IAC_STACK = REPO_ROOT / "iac" / "lib" / "dynamodb-stack.ts"
LOCAL_SCRIPT = REPO_ROOT / "scripts" / "local" / "create_tables.py"


def _iac_table_suffixes() -> set[str]:
    """Every `tableName: `${prefix}-<suffix>`` in the DynamoDB stack."""
    text = IAC_STACK.read_text()
    return set(re.findall(r"tableName:\s*`\$\{prefix\}-([a-z0-9-]+)`", text))


def _local_table_suffixes() -> set[str]:
    """Every `"stratoclave-<suffix>"` name in the local script's spec list."""
    text = LOCAL_SCRIPT.read_text()
    return set(re.findall(r'"stratoclave-([a-z0-9-]+)"', text))


def test_both_files_still_parse_at_all() -> None:
    """Guards the guard: an empty set on either side would make the comparison below
    pass no matter how far apart the two files had drifted."""
    assert IAC_STACK.is_file(), f"{IAC_STACK} is missing"
    assert LOCAL_SCRIPT.is_file(), f"{LOCAL_SCRIPT} is missing"
    iac, local = _iac_table_suffixes(), _local_table_suffixes()
    assert len(iac) >= 20, (
        f"only {len(iac)} table names parsed out of {IAC_STACK.name}; the declaration "
        f"shape this test matches on has probably changed, and the comparison below "
        f"would now be vacuous"
    )
    assert len(local) >= 20, (
        f"only {len(local)} table names parsed out of {LOCAL_SCRIPT.name}; same concern"
    )


def test_the_local_script_creates_every_table_the_iac_declares() -> None:
    missing = sorted(_iac_table_suffixes() - _local_table_suffixes())
    assert not missing, (
        f"{LOCAL_SCRIPT.relative_to(REPO_ROOT)} does not create {missing}, which "
        f"{IAC_STACK.relative_to(REPO_ROOT)} declares. A local environment missing a "
        f"table the backend reads fails as a ResourceNotFoundException from whichever "
        f"route touches it first, which is nowhere near the setup that omitted it"
    )


def test_the_local_script_creates_nothing_the_iac_does_not_declare() -> None:
    extra = sorted(_local_table_suffixes() - _iac_table_suffixes())
    assert not extra, (
        f"{LOCAL_SCRIPT.relative_to(REPO_ROOT)} creates {extra}, which "
        f"{IAC_STACK.relative_to(REPO_ROOT)} does not declare. Local development would "
        f"exercise a table that will not exist once deployed"
    )
