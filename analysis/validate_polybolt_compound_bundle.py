"""Offline admission for a PolyBolt-backed authenticated compound bundle.

This is a stricter v2 boundary than the legacy authenticated bundle validator.
It accepts an already captured artifact only; it never connects to Polymarket,
reads credentials, submits orders, or turns public observations into fills.
"""
from __future__ import annotations

import argparse
import json
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from authenticated_compound_reconciler import reconcile_authenticated_chain
from polybolt_twap_adapter import (
    PolyBoltContractError,
    build_continuity_manifest,
    normalize_polybolt_event,
)


SCHEMA = "authenticated-polybolt-compound-bundle-v2"
MARKET_SLUG = re.compile(r"^btc-updown-5m-[0-9]+$")
REQUIRED_FILLS = 10


def _fail(reason: str, *, event_count: int = 0, source_event_count: int = 0) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "required_fills": 10,
        "event_count": int(event_count),
        "source_event_count": int(source_event_count),
        "source_continuity_ok": False,
        "gate_passed": False,
        "failure_reason": reason,
    }


def _integer(value: Any) -> bool:
    return type(value) is int and value >= 0


def _decimal(value: Any, field: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{field}_must_be_finite_decimal") from exc
    if not parsed.is_finite():
        raise ValueError(f"{field}_must_be_finite_decimal")
    return parsed


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field}_required")
    return value.strip()


def _market_windows(document: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], str | None]:
    windows = document.get("market_windows")
    if not isinstance(windows, list) or not windows:
        return {}, "market_windows_required"
    out: dict[str, dict[str, Any]] = {}
    for item in windows:
        if not isinstance(item, dict):
            return {}, "invalid_market_window"
        market_id = item.get("market_id")
        slug = item.get("slug")
        start = item.get("window_start_ms")
        end = item.get("window_end_ms")
        if not isinstance(market_id, str) or not market_id.strip():
            return {}, "market_id_required"
        if not isinstance(slug, str) or not MARKET_SLUG.fullmatch(slug):
            return {}, "btc_five_minute_slug_required"
        if not _integer(start) or not _integer(end) or end - start != 300_000:
            return {}, "btc_five_minute_window_required"
        if item.get("asset") != "btc":
            return {}, "btc_asset_required"
        try:
            slug_epoch_ms = int(slug.rsplit("-", 1)[1]) * 1000
        except (ValueError, IndexError):
            return {}, "btc_slug_epoch_invalid"
        if slug_epoch_ms != start:
            return {}, "btc_slug_window_start_mismatch"
        if market_id in out:
            return {}, "duplicate_market_id"
        out[market_id] = {"market_id": market_id, "slug": slug, "window_start_ms": start, "window_end_ms": end}
    return out, None


def _validate_same_ledger(
    document: dict[str, Any],
    events: list[dict[str, Any]],
    windows: dict[str, dict[str, Any]],
    source_by_ref: dict[tuple[str, str, int], dict[str, Any]],
    *,
    required_fills: int,
) -> str | None:
    """Require a literal ten-row, linked, amount-reconciled cash chain.

    The older v1 boundary replayed rows from a fresh initial balance and could
    therefore accept two independently reset ledgers concatenated together.
    v2 carries the ledger/chain identity, sequence links, numeric balance
    checkpoints and per-fill PolyBolt references needed to reject that case.
    """
    if required_fills != REQUIRED_FILLS:
        return "required_fills_fixed_at_ten"
    try:
        ledger_id = _text(document.get("ledger_id"), "ledger_id")
        chain_id = _text(document.get("chain_id"), "chain_id")
        starting_checkpoint = _text(document.get("starting_checkpoint_id"), "starting_checkpoint_id")
        initial_cash = _decimal(document.get("initial_cash"), "initial_cash")
        closing_balance = _decimal(document.get("closing_balance"), "closing_balance")
        _text(document.get("closing_balance_checkpoint_id"), "closing_balance_checkpoint_id")
    except ValueError as exc:
        return str(exc)
    if initial_cash != Decimal("10"):
        return "initial_cash_must_equal_ten"
    if len(events) != REQUIRED_FILLS:
        return "exactly_ten_account_events_required"
    ordered = sorted(events, key=lambda row: row.get("sequence_no", -1))
    sequence_values = [row.get("sequence_no") for row in ordered]
    if sequence_values != list(range(1, REQUIRED_FILLS + 1)):
        return "sequence_no_must_be_exactly_one_to_ten"
    seen_source_refs: set[tuple[str, str, int]] = set()
    seen_checkpoints: set[str] = set()
    seen_receipts: set[str] = set()
    previous_after = initial_cash
    previous_checkpoint = starting_checkpoint
    previous_entry_ms: int | None = None
    released_receipt_ids: set[str] = set()
    # The receipts are the authoritative release records.  A receipt can only
    # be consumed once, and its amount must equal the corresponding payout.
    receipts = document.get("cash_release_receipts")
    if not isinstance(receipts, list) or len(receipts) != REQUIRED_FILLS:
        return "exactly_ten_cash_release_receipts_required"
    receipt_by_id: dict[str, dict[str, Any]] = {}
    for receipt in receipts:
        if not isinstance(receipt, dict):
            return "invalid_cash_release_receipt"
        try:
            rid = _text(receipt.get("receipt_id"), "cash_release_receipt_id")
            seq = receipt.get("sequence_no")
            if type(seq) is not int or seq < 1 or seq > REQUIRED_FILLS:
                return "cash_release_receipt_sequence_invalid"
            amount = _decimal(receipt.get("amount"), "cash_release_amount")
            release_ms = receipt.get("release_ts_ms")
            if not _integer(release_ms):
                return "cash_release_timestamp_invalid"
        except ValueError as exc:
            return str(exc)
        if rid in receipt_by_id:
            return "duplicate_cash_release_receipt_id"
        receipt_by_id[rid] = {"receipt_id": rid, "sequence_no": seq, "amount": amount, "release_ts_ms": release_ms}
    for row in ordered:
        try:
            if _text(row.get("ledger_id"), "event_ledger_id") != ledger_id:
                return "ledger_id_mismatch"
            if _text(row.get("chain_id"), "event_chain_id") != chain_id:
                return "chain_id_mismatch"
            sequence = row.get("sequence_no")
            if type(sequence) is not int or sequence < 1 or sequence > REQUIRED_FILLS:
                return "sequence_no_invalid"
            entry_ms = row.get("entry_ts_ms")
            release_ms = row.get("cash_available_ts_ms")
            if not _integer(entry_ms) or not _integer(release_ms) or release_ms < entry_ms:
                return "account_timestamp_invalid"
            if previous_entry_ms is not None and entry_ms <= previous_entry_ms:
                return "entry_timestamps_must_increase"
            previous_entry_ms = entry_ms
            market_id = _text(row.get("market_id"), "event_market_id")
            window = windows.get(market_id)
            if window is None or not (window["window_start_ms"] <= entry_ms <= window["window_end_ms"]):
                return "entry_outside_market_window"
            source_ref = row.get("source_ref")
            if not isinstance(source_ref, dict):
                return "source_ref_required_per_fill"
            capture_id = _text(source_ref.get("capture_id"), "source_capture_id")
            source_seq = source_ref.get("seq")
            source_hash = _text(source_ref.get("raw_payload_sha256"), "source_raw_payload_sha256").lower()
            source_event_ms = source_ref.get("event_ms")
            key = (market_id, capture_id, source_seq)
            if type(source_seq) is not int or source_seq < 0 or not _integer(source_event_ms):
                return "source_ref_invalid"
            if key in seen_source_refs:
                return "source_ref_reused"
            seen_source_refs.add(key)
            source = source_by_ref.get(key)
            if source is None or source.get("raw_payload_sha256") != source_hash or source.get("event_ms") != source_event_ms:
                return "source_ref_not_in_bundle"
            if not (window["window_start_ms"] <= source_event_ms <= window["window_end_ms"]):
                return "source_event_outside_market_window"
            if source_event_ms > entry_ms:
                return "source_event_after_entry"
            checkpoint = _text(row.get("balance_checkpoint_id"), "balance_checkpoint_id")
            if checkpoint in seen_checkpoints:
                return "duplicate_balance_checkpoint_id"
            seen_checkpoints.add(checkpoint)
            if _text(row.get("previous_checkpoint_id"), "previous_checkpoint_id") != previous_checkpoint:
                return "previous_checkpoint_link_mismatch"
            receipt_id = _text(row.get("cash_release_receipt_id"), "cash_release_receipt_id")
            if receipt_id in seen_receipts or receipt_id not in receipt_by_id:
                return "cash_release_receipt_link_invalid"
            seen_receipts.add(receipt_id)
            receipt = receipt_by_id[receipt_id]
            if receipt["sequence_no"] != sequence:
                return "cash_release_receipt_sequence_mismatch"
            entry_cost = _decimal(row.get("entry_cost"), "entry_cost")
            fee = _decimal(row.get("fee"), "fee")
            payout = _decimal(row.get("payout"), "payout")
            balance_before = _decimal(row.get("balance_before"), "balance_before")
            balance_after = _decimal(row.get("balance_after"), "balance_after")
            released_cash = _decimal(row.get("released_cash_since_previous_entry"), "released_cash_since_previous_entry")
            if entry_cost < 0 or fee < 0 or payout < 0 or released_cash < 0:
                return "negative_cash_amount"
            if receipt["amount"] != payout or receipt["release_ts_ms"] != release_ms:
                return "cash_release_amount_mismatch"
            due = Decimal("0")
            due_receipt_ids: set[str] = set()
            for prior in ordered:
                if prior is row:
                    break
                prior_receipt = receipt_by_id.get(prior.get("cash_release_receipt_id"))
                if prior_receipt and prior_receipt["release_ts_ms"] <= entry_ms and prior_receipt["receipt_id"] not in released_receipt_ids:
                    due += prior_receipt["amount"]
                    due_receipt_ids.add(prior_receipt["receipt_id"])
            if released_cash != due:
                return "released_cash_amount_mismatch"
            expected_before = previous_after + due
            if balance_before != expected_before:
                return "balance_before_arithmetic_mismatch"
            expected_after = balance_before - entry_cost - fee
            if balance_after != expected_after:
                return "balance_after_arithmetic_mismatch"
            if balance_after < 0:
                return "negative_balance_after_entry"
            released_receipt_ids.update(due_receipt_ids)
            if receipt["release_ts_ms"] <= entry_ms:
                released_receipt_ids.add(receipt_id)
            previous_after = balance_after
            previous_checkpoint = checkpoint
        except ValueError as exc:
            return str(exc)
    # All ten fills must have a release receipt, and the closing balance must
    # include every receipt that was not released before the tenth entry.
    if seen_receipts != set(receipt_by_id):
        return "cash_release_receipt_coverage_mismatch"
    final_expected = previous_after + sum(
        receipt["amount"] for rid, receipt in receipt_by_id.items() if rid not in released_receipt_ids
    )
    if closing_balance != final_expected:
        return "closing_balance_arithmetic_mismatch"
    return None


def validate_bundle_document(document: Any, *, required_fills: int = 10) -> dict[str, Any]:
    """Validate a source-and-account bundle without exposing account identifiers."""
    if not isinstance(document, dict) or document.get("schema") != SCHEMA:
        return _fail("invalid_bundle_schema")
    if type(required_fills) is not int or required_fills <= 0:
        return _fail("invalid_required_fills")
    if document.get("required_fills") != REQUIRED_FILLS or required_fills != REQUIRED_FILLS:
        return _fail("required_fills_fixed_at_ten", event_count=len(document.get("events", [])) if isinstance(document.get("events"), list) else 0)
    owner = document.get("owner")
    if not isinstance(owner, str) or not owner.strip():
        return _fail("bundle_owner_required")
    source_events = document.get("source_events")
    events = document.get("events")
    if not isinstance(source_events, list) or not source_events:
        return _fail("source_events_required", event_count=len(events) if isinstance(events, list) else 0)
    if not isinstance(events, list):
        return _fail("bundle_events_required", source_event_count=len(source_events))
    windows, error = _market_windows(document)
    if error:
        return _fail(error, event_count=len(events), source_event_count=len(source_events))
    account_market_ids: set[str] = set()
    for row in events:
        if not isinstance(row, dict) or row.get("market_id") not in windows:
            return _fail("account_event_market_identity_mismatch", event_count=len(events), source_event_count=len(source_events))
        account_market_ids.add(row["market_id"])
    source_market_ids: set[str] = set()
    normalized: list[dict[str, Any]] = []
    source_by_ref: dict[tuple[str, str, int], dict[str, Any]] = {}
    for raw in source_events:
        if not isinstance(raw, dict) or raw.get("market_id") not in windows:
            return _fail("source_event_market_identity_mismatch", event_count=len(events), source_event_count=len(source_events))
        source_market_ids.add(raw["market_id"])
        try:
            record = normalize_polybolt_event(raw)
        except PolyBoltContractError:
            return _fail("source_contract_violation", event_count=len(events), source_event_count=len(source_events))
        window = windows[raw["market_id"]]
        if not (window["window_start_ms"] <= record["event_ms"] <= window["window_end_ms"]):
            return _fail("source_event_after_market_window", event_count=len(events), source_event_count=len(source_events))
        record["market_id"] = raw["market_id"]
        normalized.append(record)
        if record["kind"] == "update":
            source_by_ref[(record["market_id"], record["capture_id"], record["seq"])] = record
    if source_market_ids != account_market_ids:
        return _fail("source_account_market_coverage_mismatch", event_count=len(events), source_event_count=len(source_events))
    try:
        manifest = build_continuity_manifest(normalized)
    except PolyBoltContractError:
        return _fail("source_continuity_missing", event_count=len(events), source_event_count=len(source_events))
    if not manifest["continuity_ok"]:
        return {
            **_fail("source_continuity_not_admissible", event_count=len(events), source_event_count=len(source_events)),
            "source_continuity_manifest": manifest,
        }
    ledger_failure = _validate_same_ledger(
        document,
        events,
        windows,
        source_by_ref,
        required_fills=required_fills,
    )
    if ledger_failure:
        return {
            **_fail(ledger_failure, event_count=len(events), source_event_count=len(source_events)),
            "source_continuity_manifest": manifest,
        }
    reconcile_events = []
    for row in events:
        converted = dict(row)
        converted["entry_ts"] = row["entry_ts_ms"]
        converted["cash_available_ts"] = row["cash_available_ts_ms"]
        reconcile_events.append(converted)
    result = reconcile_authenticated_chain(
        reconcile_events,
        owner=owner,
        required_fills=REQUIRED_FILLS,
    )
    # Keep the source manifest compact and omit owner/account identifiers.
    return {
        "schema": SCHEMA,
        "required_fills": int(required_fills),
        "event_count": len(events),
        "source_event_count": len(source_events),
        "source_update_count": sum(record.get("kind") == "update" for record in normalized),
        "source_continuity_ok": True,
        "source_reconnect_segments": manifest["reconnect_segments"],
        "market_window_count": len(windows),
        "fills": int(result.get("fills", 0)),
        "consecutive": bool(result.get("consecutive", False)),
        "paper_replay_passed": bool(result.get("paper_replay_passed", False)),
        "gate_passed": bool(result.get("gate_passed", False)),
        "failure_reason": result.get("failure_reason"),
        "ending_cash": result.get("ending_cash"),
        "net_pnl": result.get("net_pnl"),
        "owner_scoped": bool(result.get("owner_scoped", False)),
        "identity_complete": bool(result.get("identity_complete", False)),
        "same_ledger_identity_verified": True,
        "cash_amounts_reconciled": True,
        "source_fill_references_verified": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--required-fills", type=int, default=10)
    args = parser.parse_args()
    try:
        document = json.loads(args.bundle.read_text(encoding="utf-8"))
        result = validate_bundle_document(document, required_fills=args.required_fills)
    except (OSError, json.JSONDecodeError):
        result = _fail("bundle_read_error")
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("gate_passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
