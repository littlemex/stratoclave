"""PricingConfig table: admin-editable per-model dollar rates.

Layout (single partition, `CONFIG#pricing`):

    PK = "CONFIG#pricing", SK = "CURRENT"
        -> { active_version: "<version>" }        # the pointer
    PK = "CONFIG#pricing", SK = "__ratever__<version>__<pricing_key>"
        -> { pricing_key, version, input_per_mtok_microusd,
             output_per_mtok_microusd, cache_read_per_mtok_microusd,
             cache_write_per_mtok_microusd }

The rate rows share the `__ratever__<version>__` SK prefix so that all rows
for one version are a single `begins_with` query.

Versioning is copy-on-write: an admin writes a full set of rows under a new
version string, then flips `CURRENT.active_version` to it in one update. The
pricing module reads only `CURRENT` on its refresh tick and pulls the rows for
a version once, so hot-reload costs one point read in the steady state.

All money is integer micro-USD; this module never introduces a float.
"""
from __future__ import annotations

from typing import Optional

from boto3.dynamodb.conditions import Key

from .client import get_dynamodb_resource, pricing_config_table_name


_PK = "CONFIG#pricing"
_CURRENT_SK = "CURRENT"


def _manifest_sk(version: str) -> str:
    """Sort key for a version's manifest row (how many rate rows it has).

    A distinct namespace from the rate rows so it is never mistaken for one, and
    per-version rather than on the CURRENT pointer so it stays true for a version
    that is no longer active."""
    return f"__ratemanifest__{version}"


class RateDocumentInvalid(ValueError):
    """A rate document is not a complete, non-negative rate table.

    A ValueError so existing `set_rates` callers keep their contract, and its own
    type so the read path can tell it apart from a transient. That distinction is
    load-bearing: the rate cache is deliberately fail-static, keeping the last good
    map when a read throws, which is right for a throttle and wrong for a document
    that will never become valid — absorbing it would charge the bundled floor for
    as long as nobody reads the logs, the silent fallback this module forbids.
    """


_CHARGE_LEGS = (
    "input_per_mtok_microusd",
    "output_per_mtok_microusd",
    "cache_read_per_mtok_microusd",
    "cache_write_per_mtok_microusd",
)


def validate_rate_row_legs(row, *, version: str, pricing_key: str) -> None:
    """Refuse a rate row that is not a complete, non-negative rate document.

    Shared by the bulk load and by the point read that builds the frozen snapshot a
    request is admitted and charged at, because a missing leg must not become a rate
    of zero at either boundary — one of them is where the money actually happens.
    """
    missing = [leg for leg in _CHARGE_LEGS if row.get(leg) is None]
    if missing:
        raise RateDocumentInvalid(
            f"pricing version {version!r} key {str(pricing_key)!r} is missing "
            f"{missing}; a missing leg is not a zero rate"
        )
    for leg in _CHARGE_LEGS:
        _nonneg_int(row[leg], version, pricing_key, leg)


def _nonneg_int(value, version: str, pricing_key, column: str) -> int:
    """`value` as a non-negative int, or ValueError naming where it came from.

    Rejects bool (a `True` that becomes 1 micro-USD is not a price), rejects
    anything with a fractional part, and rejects negatives. Shared by the write and
    the read boundary so a row that could never be accepted cannot be served
    either — the document is validated wherever it is handled, not once.
    """
    if isinstance(value, bool):
        raise RateDocumentInvalid(
            f"pricing version {version!r} key {str(pricing_key)!r}: {column} is a "
            "boolean, not a rate"
        )
    try:
        ival = int(value)
    except (TypeError, ValueError) as e:
        raise RateDocumentInvalid(
            f"pricing version {version!r} key {str(pricing_key)!r}: {column}="
            f"{value!r} is not an integer micro-USD rate"
        ) from e
    if ival != value and float(ival) != float(value):
        raise RateDocumentInvalid(
            f"pricing version {version!r} key {str(pricing_key)!r}: {column}="
            f"{value!r} is not a whole micro-USD amount"
        )
    if ival < 0:
        raise RateDocumentInvalid(
            f"pricing version {version!r} key {str(pricing_key)!r}: {column}="
            f"{ival} is negative; a rate that credits an account is not a price"
        )
    return ival


class PricingConfigRepository:
    def __init__(self, table_name: Optional[str] = None) -> None:
        self._table = get_dynamodb_resource().Table(
            table_name or pricing_config_table_name()
        )

    # ----- read -----
    def current_version(self) -> Optional[str]:
        """Return the active pricing version, or None if none is set."""
        resp = self._table.get_item(Key={"pk": _PK, "sk": _CURRENT_SK})
        item = resp.get("Item")
        if not item:
            return None
        version = item.get("active_version")
        return str(version) if version else None

    def load_rates(self, version: str):
        """Return {pricing_key: Rate} for a version — all of it, or raise.

        A DynamoDB Query response is a PAGE, not a result set: it stops at 1 MB and
        hands back a cursor. Reading only the first page turned a large version into
        a silently different document — the keys past the cut were absent, so their
        requests were charged at the floor rate while the operator console still
        reported this version as active. Every row of a money document is
        load-bearing, so this follows the cursor and then checks the count the
        writer recorded: a version that cannot be read whole is refused rather than
        served in part. A row missing its `pricing_key`, or missing any of the four
        charge legs, is likewise a partial document and not a zero-priced key.

        Imported lazily to avoid a circular import with `mvp.pricing` (which
        imports this module).
        """
        from mvp.pricing import Rate

        rates: dict[str, Rate] = {}
        kwargs = {
            "KeyConditionExpression": Key("pk").eq(_PK)
            & Key("sk").begins_with(f"__ratever__{version}__"),
        }
        while True:
            resp = self._table.query(**kwargs)
            for item in resp.get("Items", []):
                key = item.get("pricing_key")
                if not key:
                    raise RateDocumentInvalid(
                        f"pricing version {version!r} has a rate row with no "
                        "pricing_key; the document is not readable as written"
                    )
                validate_rate_row_legs(item, version=version, pricing_key=key)
                rates[str(key)] = Rate(
                    input_per_mtok_microusd=_nonneg_int(
                        item["input_per_mtok_microusd"], version, key,
                        "input_per_mtok_microusd"),
                    output_per_mtok_microusd=_nonneg_int(
                        item["output_per_mtok_microusd"], version, key,
                        "output_per_mtok_microusd"),
                    cache_read_per_mtok_microusd=_nonneg_int(
                        item["cache_read_per_mtok_microusd"], version, key,
                        "cache_read_per_mtok_microusd"),
                    cache_write_per_mtok_microusd=_nonneg_int(
                        item["cache_write_per_mtok_microusd"], version, key,
                        "cache_write_per_mtok_microusd"),
                )
            cursor = resp.get("LastEvaluatedKey")
            if not cursor:
                break
            kwargs["ExclusiveStartKey"] = cursor
        expected = self._expected_row_count(version)
        if expected is not None and len(rates) != expected:
            raise RateDocumentInvalid(
                f"pricing version {version!r} read {len(rates)} rows but the "
                f"pointer records {expected}; refusing a partial rate document"
            )
        return rates

    def _expected_row_count(self, version: str):
        """The row count the writer stamped for `version`, or None if it predates
        the stamp. None means "cannot check", never "check passed".

        Read from the version's own MANIFEST row, not from the CURRENT pointer. The
        pointer is rewritten on every flip, so a count kept there described whichever
        version was active last: re-running `set_rates` for an already-active version
        with a different key set would leave the old rows in place and overwrite the
        count, and `load_rates` would then refuse the active document — which, with
        pricing failing closed, refuses every admission on the gateway. The manifest
        is written with the same create-or-identical condition as the rate rows, so a
        version's row set is immutable in the same way its rates are.
        """
        resp = self._table.get_item(Key={"pk": _PK, "sk": _manifest_sk(version)})
        item = resp.get("Item")
        if not item:
            return None
        count = item.get("row_count")
        return int(count) if count is not None else None

    def get_rates_for_version(self, version: str, pricing_key: str):
        """Return the rate-row item for one (version, pricing_key), or None.

        Rating (Layer 5) freezes the exact rate a reservation was admitted at, by
        version — so this reads ONE immutable row (a version's rows never change
        after `set_rates` flips CURRENT). No TTL cache is needed: the row is
        immutable, so the caller (mvp.pricing) caches it forever by
        (version, pricing_key).

        This is the boto3 RESOURCE Table, so the returned Item is a high-level
        dict of Python types (numbers arrive as `Decimal`) — the caller `int()`s
        the rate fields. `ConsistentRead=True`: a snapshot taken just after
        `set_rates` flips CURRENT must not miss the freshly-written row (a stale
        read would drop rating to a mislabeled default — Fable review M5). The
        row is immutable so this strong read happens at most once per version.
        """
        resp = self._table.get_item(
            Key={"pk": _PK, "sk": f"__ratever__{version}__{pricing_key}"},
            ConsistentRead=True,
        )
        return resp.get("Item")

    # ----- write (admin) -----
    #
    # IMMUTABLE-VERSION CONTRACT (Layer 5 rating): a version's rate rows must
    # NEVER change once written, and rows must NEVER be deleted. Rating freezes
    # the rate a charge was computed at BY VALUE on the ledger terminal (the
    # normative dispute evidence), but PricingConfig is the secondary record and
    # must stay reproducible too. There is deliberately NO delete API here; to
    # change a price, write a NEW version and flip CURRENT. Immutability is
    # enforced at the DB layer below by `attribute_not_exists(sk)` on each row
    # Put — NOT by IAM (IAM cannot express "create-only PutItem", Fable review M3).
    def set_rates(self, *, version: str, rates: dict, costs: Optional[dict] = None) -> None:
        """Write a full rate set under a NEW `version` and flip CURRENT to it.

        `rates` maps pricing_key -> object exposing the four per-MTok integer
        fields (a `mvp.pricing.Rate` or any duck-typed equivalent). The rows
        are written first (each gated by `attribute_not_exists(sk)` so an
        existing version can NEVER be silently overwritten — the immutable
        contract is DB-enforced), then the pointer, so a reader never sees
        CURRENT pointing at a half-written version.

        `costs` (Layer 5-d, optional) maps pricing_key -> object with the same
        four per-MTok fields expressing the PROVIDER COST (Bedrock's price to us).
        When present for a key, its four values are written as `cost_*` columns on
        that key's row (record-only — they never affect the charged amount, only
        the frozen provider_cost/margin on the ledger). A key absent from `costs`
        keeps null cost columns ("unknown", distinct from zero). Costs may exceed
        the charged rate (loss-leader) — no margin>=0 constraint.

        `version` MUST be fresh and well-formed: reusing an existing version, or
        using the reserved `builtin` sentinel, or a string containing the `__`
        delimiter, is rejected (raises ValueError) — these would corrupt version
        labels, the sentinel check, or the composite sort key.
        """
        import re

        from mvp.pricing import RESERVED_VERSIONS

        if not version or version in RESERVED_VERSIONS:
            raise ValueError(f"reserved/empty pricing version: {version!r}")
        # `__` is the sk delimiter; leading/trailing `_` would make version/key
        # boundaries ambiguous (Fable review-2 N4), so forbid `_` at the edges.
        if (
            "__" in version
            or version.startswith("_")
            or version.endswith("_")
            or not re.fullmatch(r"[A-Za-z0-9._:-]+", version)
        ):
            raise ValueError(f"malformed pricing version: {version!r}")
        for key in rates:
            if "__" in str(key):
                raise ValueError(f"malformed pricing_key (contains '__'): {key!r}")
        # A cost for a key not in `rates` is almost certainly a typo. Since the
        # version is immutable it could never be corrected, so reject it up front
        # (Fable L5-d review-2 L-1) rather than freeze an orphaned/unknown cost.
        if costs:
            orphan = set(costs) - set(rates)
            if orphan:
                raise ValueError(f"costs for keys not in rates: {sorted(orphan)}")

        from botocore.exceptions import ClientError

        costs = costs or {}
        for key, rate in rates.items():
            # A rate is a value, not whatever `int()` accepts. A negative leg mints
            # credit (`_mtok_cost` refuses one now, so such a row would refuse every
            # charge for that key instead), a bool is not a price, and a float has no
            # place in integer micro-USD. The version is immutable once written, so a
            # bad row can never be corrected — only superseded — which is why this is
            # checked here rather than repaired later.
            vin = _nonneg_int(rate.input_per_mtok_microusd, version, key,
                              "input_per_mtok_microusd")
            vout = _nonneg_int(rate.output_per_mtok_microusd, version, key,
                               "output_per_mtok_microusd")
            vcr = _nonneg_int(rate.cache_read_per_mtok_microusd, version, key,
                              "cache_read_per_mtok_microusd")
            vcw = _nonneg_int(rate.cache_write_per_mtok_microusd, version, key,
                              "cache_write_per_mtok_microusd")
            item = {
                "pk": _PK,
                "sk": f"__ratever__{version}__{key}",
                "pricing_key": key,
                "version": version,
                "input_per_mtok_microusd": vin,
                "output_per_mtok_microusd": vout,
                "cache_read_per_mtok_microusd": vcr,
                "cache_write_per_mtok_microusd": vcw,
            }
            # Optional record-only provider-cost columns (L5-d). Non-negative int.
            _cost_cols = (
                ("cost_input_per_mtok_microusd", ":ci"),
                ("cost_output_per_mtok_microusd", ":co"),
                ("cost_cache_read_per_mtok_microusd", ":ccr"),
                ("cost_cache_write_per_mtok_microusd", ":ccw"),
            )
            cost = costs.get(key)
            cost_vals: dict[str, int] = {}
            if cost is not None:
                for (col, ph), val in zip(
                    _cost_cols,
                    (cost.input_per_mtok_microusd, cost.output_per_mtok_microusd,
                     cost.cache_read_per_mtok_microusd, cost.cache_write_per_mtok_microusd),
                ):
                    iv = int(val)
                    if iv < 0:
                        raise ValueError(f"provider cost must be non-negative: {col}={iv}")
                    item[col] = iv
                    cost_vals[ph] = iv
            try:
                # IDEMPOTENT immutability (Fable review-2 N1 + L5-d H1): allow the
                # row to be (re)written iff it does not exist OR already holds the
                # SAME values — for the four CHARGE rates AND the four cost_*
                # columns. Guarding ONLY the charge rates (the earlier version)
                # let a re-`set_rates` with the same rates but different/absent
                # costs silently mutate the record-only cost, breaking the "one
                # pricing_version → one provider_cost" audit guarantee. Each cost
                # column is matched as "absent-on-both OR equal", so costs-present
                # and costs-absent are each immutable, and crash-retry with the
                # SAME payload still succeeds.
                _cost_clause = " AND ".join(
                    (
                        f"{col} = {ph}" if ph in cost_vals
                        else f"attribute_not_exists({col})"
                    )
                    for col, ph in _cost_cols
                )
                _values = {":i": vin, ":o": vout, ":cr": vcr, ":cw": vcw}
                _values.update(cost_vals)
                self._table.put_item(
                    Item=item,
                    ConditionExpression=(
                        "attribute_not_exists(sk) OR "
                        "(input_per_mtok_microusd = :i AND output_per_mtok_microusd = :o "
                        "AND cache_read_per_mtok_microusd = :cr "
                        "AND cache_write_per_mtok_microusd = :cw "
                        f"AND {_cost_clause})"
                    ),
                    ExpressionAttributeValues=_values,
                )
            except ClientError as e:
                if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                    raise ValueError(
                        f"pricing version {version!r} already exists with DIFFERENT "
                        f"rates or costs for {key!r} (immutable) — use a fresh version"
                    ) from e
                raise
        # The version's manifest: how many rate rows it has, so a reader can tell a
        # complete document from a truncated read of one (see `load_rates`). Written
        # with the same create-or-identical condition as the rows themselves, which
        # makes a version's KEY SET immutable too: re-running `set_rates` for an
        # existing version with a different set of keys is refused here rather than
        # leaving a row union behind that the reader would then refuse to load.
        try:
            self._table.put_item(
                Item={"pk": _PK, "sk": _manifest_sk(version),
                      "version": version, "row_count": len(rates)},
                ConditionExpression="attribute_not_exists(sk) OR row_count = :n",
                ExpressionAttributeValues={":n": len(rates)},
            )
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                raise ValueError(
                    f"pricing version {version!r} already exists with a DIFFERENT "
                    "number of rate rows (immutable) — use a fresh version"
                ) from e
            raise
        # Flip CURRENT last. Written unconditionally so a crash AFTER the rows but
        # BEFORE this flip is recoverable: re-running set_rates with the same
        # (version, rates) idempotently re-writes the rows and completes the flip.
        self._table.put_item(
            Item={"pk": _PK, "sk": _CURRENT_SK, "active_version": version}
        )
