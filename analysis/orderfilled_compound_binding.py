"""Bind one continuous compound ledger to canonical on-chain OrderFilled rows.

This is a read-only evidence join.  It consumes already decoded and normalized
OrderFilled records; it never fetches a chain, reads an account, or treats a
CLOB ``MATCHED`` status as an on-chain fill.  The existing paper and
authenticated settlement gates remain unchanged.  This layer only answers
whether each ledger row has a distinct, field-consistent OrderFilled identity
that can be carried into a future owner-scoped admission bundle.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Iterable


TOLERANCE = Decimal("0.000001")


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _decimal(value: Any, field: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{field}_must_be_finite_decimal") from exc
    if not result.is_finite():
        raise ValueError(f"{field}_must_be_finite_decimal")
    return result


def _issue(reason: str, sequence_no: Any = None, **details: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"reason": reason}
    if sequence_no is not None:
        out["sequence_no"] = sequence_no
    out.update(details)
    return out


def _same_decimal(left: Any, right: Any, field: str) -> bool:
    try:
        return abs(_decimal(left, field) - _decimal(right, field)) <= TOLERANCE
    except ValueError:
        return False


def audit_orderfilled_bindings(
    rows: Iterable[dict[str, Any]],
    orderfilled: Iterable[dict[str, Any]],
    *,
    owner_address: str | None = None,
) -> dict[str, Any]:
    """Audit ten ledger rows against distinct canonical OrderFilled records.

    ``orderfilled`` must already be normalized by
    :func:`onchain_orderfilled_normalizer.normalize_orderfilled`.  The
    ``orderfilled_event_id`` field on each ledger row is mandatory so that two
    fills in one transaction cannot be silently collapsed.  Owner scope is
    reported separately and requires an explicit address supplied by the
    caller; a successful field join alone never proves account ownership.
    """
    raw_rows = list(rows)
    raw_fills = list(orderfilled)
    issues: list[dict[str, Any]] = []
    if len(raw_rows) != 10:
        issues.append(_issue("exactly_ten_ledger_rows_required", count=len(raw_rows)))
    if len(raw_fills) != 10:
        issues.append(_issue("exactly_ten_orderfilled_records_required", count=len(raw_fills)))

    by_event: dict[str, dict[str, Any]] = {}
    for fill in raw_fills:
        if not isinstance(fill, dict):
            issues.append(_issue("orderfilled_record_must_be_object"))
            continue
        event_id = _text(fill.get("event_id"))
        if not event_id:
            issues.append(_issue("orderfilled_event_id_required"))
            continue
        if event_id in by_event:
            issues.append(_issue("duplicate_orderfilled_event_id", event_id=event_id))
            continue
        by_event[event_id] = fill

    sequence_rows: dict[int, dict[str, Any]] = {}
    for row in raw_rows:
        if not isinstance(row, dict):
            issues.append(_issue("ledger_row_must_be_object"))
            continue
        sequence = row.get("sequence_no")
        if type(sequence) is not int or not 1 <= sequence <= 10:
            issues.append(_issue("sequence_no_invalid", sequence_no=sequence))
            continue
        if sequence in sequence_rows:
            issues.append(_issue("duplicate_sequence_no", sequence_no=sequence))
            continue
        sequence_rows[sequence] = row

    if set(sequence_rows) != set(range(1, 11)):
        issues.append(_issue("sequence_no_must_cover_one_to_ten", observed=sorted(sequence_rows)))

    used_events: set[str] = set()
    matched = 0
    owner_checked = bool(_text(owner_address))
    owner_address_text = _text(owner_address).lower()
    for sequence in range(1, 11):
        row = sequence_rows.get(sequence)
        if row is None:
            continue
        event_id = _text(row.get("orderfilled_event_id"))
        if not event_id:
            issues.append(_issue("orderfilled_event_id_required_per_row", sequence))
            continue
        if event_id in used_events:
            issues.append(_issue("orderfilled_event_reused", sequence, event_id=event_id))
            continue
        used_events.add(event_id)
        fill = by_event.get(event_id)
        if fill is None:
            issues.append(_issue("orderfilled_event_not_in_bundle", sequence, event_id=event_id))
            continue

        row_issues: list[dict[str, Any]] = []
        if _text(fill.get("tx_hash")) != _text(row.get("transaction_ref")):
            row_issues.append(_issue("transaction_ref_mismatch", sequence))
        if _text(fill.get("market_id")) != _text(row.get("market_id")):
            row_issues.append(_issue("market_id_mismatch_or_missing", sequence))
        if _text(fill.get("side")).upper() != "BUY" or fill.get("sign") != 1:
            row_issues.append(_issue("orderfilled_not_buy_entry", sequence))
        if _text(row.get("execution_role")).upper() != "TAKER":
            row_issues.append(_issue("ledger_execution_role_not_taker", sequence))
        if not _same_decimal(fill.get("size"), row.get("shares"), "shares"):
            row_issues.append(_issue("share_quantity_mismatch", sequence))
        if not _same_decimal(fill.get("price"), row.get("price"), "price"):
            row_issues.append(_issue("execution_price_mismatch", sequence))
        if not _same_decimal(fill.get("cash_amount"), row.get("entry_cost"), "entry_cost"):
            row_issues.append(_issue("entry_cost_mismatch", sequence))
        if owner_checked and _text(fill.get("taker")).lower() != owner_address_text:
            row_issues.append(_issue("taker_owner_mismatch", sequence))
        if row_issues:
            issues.extend(row_issues)
            continue
        matched += 1

    if not owner_checked:
        issues.append(_issue("owner_address_required_for_owner_scope"))

    return {
        "schema_version": 1,
        "required_rows": 10,
        "ledger_rows": len(raw_rows),
        "orderfilled_records": len(raw_fills),
        "matched_rows": matched,
        "distinct_orderfilled_events": len(used_events),
        "owner_scope_checked": owner_checked,
        "owner_scope_verified": owner_checked and matched == 10 and not issues,
        "orderfilled_binding_ready": matched == 10 and not issues,
        "issues": issues,
        "evidence_boundary": "canonical on-chain event binding only; no account authentication or settlement/cash proof",
    }
