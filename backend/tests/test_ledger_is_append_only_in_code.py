"""C4.3 — the ledger module cannot mutate or delete, as a mechanism.

The deployed task role DENIES `UpdateItem`, `DeleteItem` and `BatchWriteItem` on
the credit-ledger table (`CreditLedgerNoMutateOrDelete` in `iac/lib/ecs-stack.ts`),
and `iac/test` pins that policy. What was NOT pinned was the other side of the same
invariant: whether the Python that writes the ledger only ever issues writes the
policy permits.

That gap was not hypothetical. A status finalize on the idempotency row called
`update_item`, wrapped in a best-effort `except` that swallowed the failure — so in
production it was denied on every call, silently, while the contract document cited
it as the one legitimate exception to append-only. Tests never saw it because moto
does not enforce IAM. A test that reads the code catches exactly the class of defect
a test that runs the code cannot: a write whose failure is designed to be invisible.

The check is deliberately static rather than behavioural for that reason. It is also
why it lives here and not in an integration suite: the property is "this module does
not contain such a call", which is decidable by reading it.
"""
from __future__ import annotations

import ast
import pathlib


FORBIDDEN = {"update_item", "delete_item", "batch_write_item"}
MODULE = pathlib.Path(__file__).resolve().parents[1] / "dynamo" / "credit_ledger.py"


def test_the_ledger_repository_issues_no_mutating_write():
    tree = ast.parse(MODULE.read_text())
    offenders = [
        f"{MODULE.name}:{node.lineno} calls {node.func.attr}()"
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in FORBIDDEN
    ]
    assert not offenders, (
        "the credit ledger is append-only and the deployed IAM policy DENIES these "
        "actions on its table, so such a call cannot succeed in production — it can "
        "only fail somewhere its failure is not surfaced:\n  " + "\n  ".join(offenders)
    )


def test_transact_write_items_carries_no_delete_or_update():
    """The permitted write path is `TransactWriteItems` of Put / ConditionCheck, so
    a transaction item keyed `Delete` or `Update` would be denied at the same policy
    while looking, in the code, like every other ledger write."""
    text = MODULE.read_text()
    bad = [k for k in ('"Delete":', "'Delete':", '"Update":', "'Update':") if k in text]
    assert not bad, (
        f"transaction item types the ledger policy denies appear in {MODULE.name}: {bad}"
    )
