"""Assemble an H600 compound bundle from already captured private evidence.

H600 is the H578 depth policy plus the selected-side two-minute ask floor. The
builder keeps that extra observation in every hashed decision trace so a plain
H578 bundle cannot be relabelled as H600. It is offline only and never fetches
receipts or submits an order.
"""
from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

try:
    from .validate_polybolt_compound_bundle import (
        H600_STRATEGY_CONFIG_SHA256,
        H600_STRATEGY_ID,
        H600_STRATEGY_PROFILE,
        H600_STRATEGY_VERSION,
        validate_bundle_document,
    )
except ImportError:  # pragma: no cover - direct script/test invocation
    from validate_polybolt_compound_bundle import (
        H600_STRATEGY_CONFIG_SHA256,
        H600_STRATEGY_ID,
        H600_STRATEGY_PROFILE,
        H600_STRATEGY_VERSION,
        validate_bundle_document,
    )


REQUIRED_FILLS = 10
TRACE_KEYS = {
    "decision_ref", "sequence_no", "cash_fraction", "position_side",
    "entry_sell_depth_shares", "depth_capacity_ratio", "favorite_ask_price",
}


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field}_required")
    return value.strip()


def _required(mapping: Mapping[str, Any], field: str) -> Any:
    if field not in mapping or mapping[field] in (None, ""):
        raise ValueError(f"{field}_required")
    return mapping[field]


def _canonical_hash(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    ).hexdigest()


ORDERFILLED_TOLERANCE = Decimal("0.000001")


def _orderfilled_index(records: Any) -> dict[str, dict[str, Any]]:
    """Index the canonical records used by the streaming H600 buffer.

    The final v2 validator repeats the full structural check.  This smaller
    index is intentionally run before the first row is accepted so a capture
    cannot accumulate nine rows around an unknown or malformed event id.
    """
    if not isinstance(records, list) or len(records) != REQUIRED_FILLS:
        raise ValueError("exactly_ten_orderfilled_records_required")
    out: dict[str, dict[str, Any]] = {}
    for record in records:
        if not isinstance(record, Mapping):
            raise ValueError("invalid_orderfilled_record")
        event_id = _text(record.get("event_id"), "orderfilled_event_id")
        tx_hash = _text(record.get("tx_hash"), "orderfilled_tx_hash")
        try:
            log_index = int(record.get("log_index"))
            block_number = int(record.get("block_number"))
        except (TypeError, ValueError) as exc:
            raise ValueError("orderfilled_identity_invalid") from exc
        if log_index < 0 or block_number < 0 or event_id != f"{tx_hash}:{log_index}":
            raise ValueError("orderfilled_event_id_mismatch")
        if event_id in out:
            raise ValueError("duplicate_orderfilled_event_id")
        if _text(record.get("side"), "orderfilled_side").upper() != "BUY" or record.get("sign") != 1:
            raise ValueError("orderfilled_not_buy_entry")
        try:
            size = Decimal(str(record.get("size")))
            price = Decimal(str(record.get("price")))
            cash_amount = Decimal(str(record.get("cash_amount")))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError("orderfilled_amount_or_price_invalid") from exc
        if not size.is_finite() or not cash_amount.is_finite() or not price.is_finite():
            raise ValueError("orderfilled_amount_or_price_invalid")
        if size <= 0 or cash_amount <= 0 or not (Decimal("0") < price < Decimal("1")):
            raise ValueError("orderfilled_amount_or_price_invalid")
        out[event_id] = {
            "event_id": event_id,
            "tx_hash": tx_hash,
            "market_id": _text(record.get("market_id"), "orderfilled_market_id"),
            "taker": _text(record.get("taker"), "orderfilled_taker").lower(),
            "size": size,
            "price": price,
            "cash_amount": cash_amount,
        }
    return out


def _check_capture_orderfilled_binding(
    row: Mapping[str, Any],
    record: Mapping[str, Any] | None,
    *,
    owner: str,
) -> None:
    if record is None:
        raise ValueError("orderfilled_event_not_in_bundle")
    if record["tx_hash"] != _text(row.get("transaction_ref"), "transaction_ref"):
        raise ValueError("orderfilled_transaction_mismatch")
    if record["market_id"] != _text(row.get("market_id"), "event_market_id"):
        raise ValueError("orderfilled_market_mismatch")
    if record["taker"] != owner.lower():
        raise ValueError("orderfilled_taker_owner_mismatch")
    try:
        if abs(record["size"] - Decimal(str(row.get("shares")))) > ORDERFILLED_TOLERANCE:
            raise ValueError("orderfilled_size_mismatch")
        if abs(record["price"] - Decimal(str(row.get("price")))) > ORDERFILLED_TOLERANCE:
            raise ValueError("orderfilled_price_mismatch")
        if abs(record["cash_amount"] - Decimal(str(row.get("entry_cost")))) > ORDERFILLED_TOLERANCE:
            raise ValueError("orderfilled_cash_amount_mismatch")
    except (InvalidOperation, TypeError, ValueError) as exc:
        if isinstance(exc, ValueError) and str(exc).startswith("orderfilled_"):
            raise
        raise ValueError("orderfilled_amount_or_price_invalid") from exc


def _strategy_trace(row: Mapping[str, Any]) -> dict[str, Any]:
    nested = row.get("strategy_decision")
    if nested is not None and not isinstance(nested, Mapping):
        raise ValueError("strategy_decision_must_be_object")
    source = dict(nested) if isinstance(nested, Mapping) else dict(row)
    trace = {
        "decision_ref": _text(source.get("decision_ref", row.get("strategy_decision_ref")), "strategy_decision_ref"),
        "sequence_no": _required(source, "sequence_no"),
        "cash_fraction": _required(source, "cash_fraction"),
        "position_side": _text(source.get("position_side"), "position_side").lower(),
        "entry_sell_depth_shares": _required(source, "entry_sell_depth_shares"),
        "depth_capacity_ratio": _required(source, "depth_capacity_ratio"),
        "favorite_ask_price": _required(source, "favorite_ask_price"),
    }
    if set(trace) != TRACE_KEYS:
        raise ValueError("strategy_decision_trace_invalid")
    if type(trace["sequence_no"]) is not int or not 1 <= trace["sequence_no"] <= REQUIRED_FILLS:
        raise ValueError("strategy_decision_sequence_invalid")
    if trace["position_side"] not in {"up", "down"}:
        raise ValueError("strategy_decision_position_side_invalid")
    return trace


def _normalize_row(raw: Mapping[str, Any], trace: dict[str, Any]) -> dict[str, Any]:
    row = deepcopy(dict(raw))
    row.pop("strategy_decision", None)
    if "entry_ts_ms" not in row and "entry_ts" in row:
        row["entry_ts_ms"] = row["entry_ts"]
    if "cash_available_ts_ms" not in row and "cash_available_ts" in row:
        row["cash_available_ts_ms"] = row["cash_available_ts"]
    row["strategy_decision_ref"] = trace["decision_ref"]
    row["strategy_decision_sha256"] = _canonical_hash(trace)
    # Keep the direct builder path aligned with H600CompoundCapture.  A
    # caller that bypasses the streaming buffer must fail before producing a
    # half-assembled document whose missing execution identity or cash links
    # are discovered only by the later v2 validator.
    for field in (
        "order_id", "order_status", "trade_id", "transaction_ref", "orderfilled_event_id",
        "settlement_receipt_id", "cash_release_receipt_id",
        "balance_checkpoint_id", "previous_checkpoint_id",
        "cash_reconciled", "independent_checkpoint",
        "entry_ts_ms", "cash_available_ts_ms", "source_ref", "depth_ref", "market_id",
    ):
        _required(row, field)
    return row


def build_h600_bundle(capture: Mapping[str, Any]) -> dict[str, Any]:
    """Build one H600 document from ten pre-captured evidence rows."""
    if not isinstance(capture, Mapping):
        raise ValueError("capture_must_be_object")
    raw_rows = capture.get("rows")
    if not isinstance(raw_rows, list) or len(raw_rows) != REQUIRED_FILLS:
        raise ValueError("exactly_ten_rows_required")
    rows: list[dict[str, Any]] = []
    traces: list[dict[str, Any]] = []
    for raw in raw_rows:
        if not isinstance(raw, Mapping):
            raise ValueError("row_must_be_object")
        trace = _strategy_trace(raw)
        traces.append(trace)
        rows.append(_normalize_row(raw, trace))
    if sorted(trace["sequence_no"] for trace in traces) != list(range(1, REQUIRED_FILLS + 1)):
        raise ValueError("strategy_decisions_must_cover_one_to_ten")

    strategy = capture.get("strategy")
    if strategy is None:
        strategy = {"id": H600_STRATEGY_ID, "version": H600_STRATEGY_VERSION, "config_sha256": H600_STRATEGY_CONFIG_SHA256}
    elif not isinstance(strategy, Mapping):
        raise ValueError("strategy_must_be_object")
    elif (
        strategy.get("id") != H600_STRATEGY_ID
        or strategy.get("version") != H600_STRATEGY_VERSION
        or strategy.get("config_sha256") != H600_STRATEGY_CONFIG_SHA256
    ):
        raise ValueError("strategy_must_be_h600")

    required = (
        "owner", "ledger_id", "chain_id", "starting_checkpoint_id", "closing_balance_checkpoint_id",
        "initial_cash", "closing_balance", "market_windows", "source_events", "depth_snapshots",
        "settlement_receipts", "cash_release_receipts", "orderfilled_records",
    )
    for field in required:
        _required(capture, field)
    # Keep the direct builder fail-closed at the same boundary as the
    # streaming capture.  A malformed canonical record must not produce a
    # half-assembled H600 document that only fails after construction.
    _orderfilled_index(capture["orderfilled_records"])
    return {
        "schema": "authenticated-polybolt-compound-bundle-v2",
        "required_fills": REQUIRED_FILLS,
        "owner": capture["owner"], "ledger_id": capture["ledger_id"], "chain_id": capture["chain_id"],
        "starting_checkpoint_id": capture["starting_checkpoint_id"], "initial_cash": capture["initial_cash"],
        "closing_balance": capture["closing_balance"], "closing_balance_checkpoint_id": capture["closing_balance_checkpoint_id"],
        "strategy": dict(strategy), "market_windows": deepcopy(capture["market_windows"]),
        "cash_release_receipts": deepcopy(capture["cash_release_receipts"]),
        "settlement_receipts": deepcopy(capture["settlement_receipts"]),
        "orderfilled_records": deepcopy(capture["orderfilled_records"]),
        "source_events": deepcopy(capture["source_events"]), "depth_snapshots": deepcopy(capture["depth_snapshots"]),
        "events": rows, "strategy_decisions": traces,
    }


def validate_built_h600_bundle(capture: Mapping[str, Any]) -> dict[str, Any]:
    return validate_bundle_document(build_h600_bundle(capture), strategy_profile=H600_STRATEGY_PROFILE)


class H600CompoundCapture:
    """Accumulate one owner-scoped H600 chain without allowing concatenation.

    Rows are accepted only in sequence 1..10 on the same owner, ledger and
    chain.  Each row must point at the immediately preceding balance
    checkpoint, and evidence identities may not be reused.  This is an
    offline capture buffer; it never reads a wallet or submits an order.
    """

    def __init__(self, header: Mapping[str, Any]):
        if not isinstance(header, Mapping):
            raise ValueError("capture_header_must_be_object")
        required = (
            "owner", "ledger_id", "chain_id", "starting_checkpoint_id",
            "closing_balance_checkpoint_id", "initial_cash", "closing_balance",
            "market_windows", "source_events", "depth_snapshots",
            "settlement_receipts", "cash_release_receipts", "orderfilled_records",
        )
        for field in required:
            _required(header, field)
        self._header = deepcopy(dict(header))
        self._header.pop("rows", None)
        self._orderfilled_by_id = _orderfilled_index(self._header["orderfilled_records"])
        self._rows: list[dict[str, Any]] = []
        self._seen: dict[str, set[str]] = {
            "order_id": set(),
            "trade_id": set(),
            "transaction_ref": set(),
            "orderfilled_event_id": set(),
            "source_ref": set(),
            "depth_ref": set(),
            "strategy_decision_ref": set(),
        }

    @property
    def rows(self) -> tuple[dict[str, Any], ...]:
        return tuple(deepcopy(self._rows))

    def append(self, row: Mapping[str, Any]) -> None:
        if len(self._rows) >= REQUIRED_FILLS:
            raise ValueError("capture_already_complete")
        if not isinstance(row, Mapping):
            raise ValueError("capture_row_must_be_object")
        candidate = deepcopy(dict(row))
        expected_sequence = len(self._rows) + 1
        if candidate.get("sequence_no") != expected_sequence:
            raise ValueError("sequence_no_must_advance_without_reset")
        # Reject an incomplete row at capture time.  Waiting until the tenth
        # row reaches the bundle validator wastes a whole observation window
        # and can leave a historical lifecycle row looking more admissible
        # than it is.  These fields are the minimum owner-bundle join needed
        # before a row may enter the continuous H600 buffer.
        for field in (
            "order_id", "order_status", "trade_id", "transaction_ref", "orderfilled_event_id",
            "source_ref", "depth_ref", "strategy_decision",
            "strategy_decision_ref", "settlement_receipt_id",
            "cash_release_receipt_id", "balance_checkpoint_id",
            "previous_checkpoint_id", "cash_reconciled",
            "independent_checkpoint", "entry_ts_ms", "cash_available_ts_ms",
            "shares", "price", "entry_cost",
        ):
            _required(candidate, field)
        try:
            entry_ts_ms = int(str(candidate["entry_ts_ms"]))
            cash_available_ts_ms = int(str(candidate["cash_available_ts_ms"]))
        except (TypeError, ValueError) as exc:
            raise ValueError("capture_timestamps_must_be_integer") from exc
        if cash_available_ts_ms < entry_ts_ms:
            raise ValueError("cash_available_timestamp_before_entry")
        for field in ("owner", "ledger_id", "chain_id"):
            if candidate.get(field) != self._header.get(field):
                raise ValueError(f"{field}_mismatch")
        _check_capture_orderfilled_binding(
            candidate,
            self._orderfilled_by_id.get(str(candidate["orderfilled_event_id"]).strip()),
            owner=_text(self._header["owner"], "owner"),
        )
        previous = self._header["starting_checkpoint_id"] if expected_sequence == 1 else self._rows[-1].get("balance_checkpoint_id")
        if candidate.get("previous_checkpoint_id") != previous:
            raise ValueError("previous_checkpoint_link_mismatch")
        for field in self._seen:
            if field == "source_ref":
                ref = candidate.get(field)
                if not isinstance(ref, Mapping):
                    raise ValueError(f"{field}_reused_or_missing")
                key = json.dumps(ref, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
            elif field == "depth_ref":
                ref = candidate.get(field)
                if not isinstance(ref, Mapping):
                    raise ValueError(f"{field}_reused_or_missing")
                key = json.dumps(ref, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
            else:
                key = str(candidate.get(field) or "").strip()
            if not key or key in self._seen[field]:
                raise ValueError(f"{field}_reused_or_missing")
            self._seen[field].add(key)
        if self._rows:
            try:
                if entry_ts_ms <= int(str(self._rows[-1]["entry_ts_ms"])):
                    raise ValueError("entry_timestamps_must_increase")
            except KeyError as exc:
                raise ValueError("entry_ts_ms_required") from exc
        self._rows.append(candidate)

    def build(self) -> dict[str, Any]:
        if len(self._rows) != REQUIRED_FILLS:
            raise ValueError("exactly_ten_rows_required")
        return build_h600_bundle({**deepcopy(self._header), "rows": list(self.rows)})


__all__ = ["build_h600_bundle", "validate_built_h600_bundle", "H600CompoundCapture"]
