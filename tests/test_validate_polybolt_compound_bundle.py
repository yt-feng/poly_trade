import hashlib
import sys

from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "analysis"))

from validate_polybolt_compound_bundle import (
    FROZEN_STRATEGY_CONFIG_SHA256,
    FROZEN_STRATEGY_ID,
    FROZEN_STRATEGY_VERSION,
    validate_bundle_document,
)


START = 1_800_000_000_000
MARKET_ID = "m-1"


def source_event(seq: int, market_id: str = MARKET_ID, *, event_ms: int | None = None) -> dict[str, object]:
    timestamp = START + (seq - 1) * 1_000 if event_ms is None else event_ms
    digest = hashlib.sha256(f"event-{seq}".encode()).hexdigest()
    return {
        "market_id": market_id,
        "topic": "prices.crypto.twap",
        "type": "update",
        "timestamp": timestamp,
        "seq": seq,
        "dropped": 0,
        "authenticated": True,
        "received_ms": timestamp + 100,
        "connection_id": "conn-a",
        "sequence_scope": "capture-a/conn-a",
        "capture_id": "capture-a",
        "raw_payload_sha256": digest,
        "payload": {"symbol": "btcusd", "timestamp": timestamp, "value": "100.0", "windowSeconds": 60},
    }


def depth_snapshot(seq: int, market_id: str = MARKET_ID, *, event_ms: int | None = None) -> dict[str, object]:
    timestamp = START + (seq - 1) * 1_000 if event_ms is None else event_ms
    digest = hashlib.sha256(f"depth-{seq}".encode()).hexdigest()
    return {
        "market_id": market_id,
        "snapshot_id": f"depth-{seq}",
        "capture_id": "book-capture-a",
        "seq": seq,
        "raw_payload_sha256": digest,
        "event_ms": timestamp,
        "received_ms": timestamp + 50,
        "available_shares": "100",
        "ask_price": "0.50",
    }


def account_event(i: int, *, ledger_id: str = "ledger-1", chain_id: str = "chain-1") -> dict[str, object]:
    entry_ms = START + (i - 1) * 10_000 + 1_000
    release_ms = entry_ms + 5_000
    before = Decimal("10") + Decimal("0.26") * (i - 1)
    after = before - Decimal("5.175")
    digest = hashlib.sha256(f"event-{i}".encode()).hexdigest()
    return {
        "owner": "owner-1",
        "ledger_id": ledger_id,
        "chain_id": chain_id,
        "sequence_no": i,
        "previous_checkpoint_id": "opening" if i == 1 else f"balance-{i - 1}",
        "market_id": MARKET_ID,
        "status": "confirmed",
        "trade_id": f"trade-{i}",
        "transaction_ref": f"tx-{i}",
        "settlement_receipt_id": f"settlement-{i}",
        "cash_release_receipt_id": f"cash-release-{i}",
        "execution_role": "TAKER",
        "fee_rate": "0.07",
        "balance_checkpoint_id": f"balance-{i}",
        "cash_reconciled": True,
        "independent_checkpoint": True,
        "entry_ts_ms": entry_ms,
        "cash_available_ts_ms": release_ms,
        "entry_cost": "5",
        "shares": "10",
        "price": "0.5",
        "fee": "0.175",
        "payout": "5.435",
        "released_cash_since_previous_entry": "0" if i == 1 else "5.435",
        "balance_before": format(before, "f"),
        "balance_after": format(after, "f"),
        "source_ref": {
            "market_id": MARKET_ID,
            "capture_id": "capture-a",
            "seq": i,
            "raw_payload_sha256": digest,
            "event_ms": START + (i - 1) * 1_000,
        },
        "depth_ref": {
            "snapshot_id": f"depth-{i}",
            "capture_id": "book-capture-a",
            "seq": i,
            "raw_payload_sha256": hashlib.sha256(f"depth-{i}".encode()).hexdigest(),
            "event_ms": START + (i - 1) * 1_000,
        },
        "strategy_decision_ref": f"decision-{i}",
        "strategy_decision_sha256": hashlib.sha256(f"decision-{i}".encode()).hexdigest(),
    }


def bundle(events=None, source=None):
    return {
        "schema": "authenticated-polybolt-compound-bundle-v2",
        "required_fills": 10,
        "owner": "owner-1",
        "ledger_id": "ledger-1",
        "chain_id": "chain-1",
        "starting_checkpoint_id": "opening",
        "initial_cash": "10",
        "closing_balance": "12.60",
        "closing_balance_checkpoint_id": "closing",
        "strategy": {
            "id": FROZEN_STRATEGY_ID,
            "version": FROZEN_STRATEGY_VERSION,
            "config_sha256": FROZEN_STRATEGY_CONFIG_SHA256,
        },
        "market_windows": [{
            "market_id": MARKET_ID,
            "asset": "btc",
            "slug": "btc-updown-5m-1800000000",
            "window_start_ms": START,
            "window_end_ms": START + 300_000,
            "min_order_size_unit": "shares",
            "min_order_size": "5",
            "min_order_shares": "5",
            "tick_size": "0.01",
        }],
        "cash_release_receipts": [
            {"receipt_id": f"cash-release-{i}", "sequence_no": i, "amount": "5.435", "release_ts_ms": START + (i - 1) * 10_000 + 6_000}
            for i in range(1, 11)
        ],
        "settlement_receipts": [
            {
                "receipt_id": f"settlement-{i}",
                "sequence_no": i,
                "market_id": MARKET_ID,
                "settled_ts_ms": START + (i - 1) * 10_000 + 5_000,
                "raw_payload_sha256": hashlib.sha256(f"settlement-{i}".encode()).hexdigest(),
                "payout": "5.435",
            }
            for i in range(1, 11)
        ],
        "source_events": [source_event(i) for i in range(1, 11)] if source is None else source,
        "depth_snapshots": [depth_snapshot(i) for i in range(1, 11)],
        "events": [account_event(i) for i in range(1, 11)] if events is None else events,
    }


def test_supplied_source_and_cash_chain_pass_offline_consistency_only():
    result = validate_bundle_document(bundle())
    assert result["offline_consistency_passed"] is True
    assert result["source_continuity_ok"] is True
    assert result["fills"] == 10
    assert result["same_ledger_identity_verified"] is True
    assert result["cash_amounts_reconciled"] is True
    assert result["market_window_count"] == 1
    assert "owner-1" not in str(result)


def test_minimum_share_contract_accepts_five_share_entry():
    result = validate_bundle_document(bundle())
    assert result["offline_consistency_passed"] is True


def test_market_share_floor_is_enforced():
    doc = bundle()
    doc["events"][0]["shares"] = "4"
    doc["events"][0]["entry_cost"] = "2"
    doc["events"][0]["fee"] = "0.056"
    result = validate_bundle_document(doc)
    assert result["offline_consistency_passed"] is False
    assert result["failure_reason"] == "shares_below_market_minimum"


def test_market_share_floor_field_is_required_and_frozen():
    doc = bundle()
    del doc["market_windows"][0]["min_order_shares"]
    result = validate_bundle_document(doc)
    assert result["offline_consistency_passed"] is False
    assert result["failure_reason"] == "min_order_shares_must_be_finite_decimal"

    doc = bundle()
    doc["market_windows"][0]["min_order_shares"] = "4"
    result = validate_bundle_document(doc)
    assert result["offline_consistency_passed"] is False
    assert result["failure_reason"] == "market_minimum_order_shares_below_frozen_floor"


def test_notional_unit_claim_is_rejected_when_share_unit_is_required():
    doc = bundle()
    doc["market_windows"][0]["min_order_size_unit"] = "USDC"
    result = validate_bundle_document(doc)
    assert result["offline_consistency_passed"] is False
    assert result["failure_reason"] == "minimum_order_size_unit_must_be_shares"


def test_five_share_fill_can_use_less_than_five_dollars_of_cash():
    """A share floor must not become an accidental universal notional floor."""
    doc = bundle()
    for snapshot in doc["depth_snapshots"]:
        snapshot["ask_price"] = "0.20"
    payout = Decimal("1.1")
    fee = Decimal("0.056")
    previous_after = Decimal("10")
    for i, event in enumerate(doc["events"], start=1):
        before = previous_after if i == 1 else previous_after + payout
        after = before - Decimal("1") - fee
        event.update(
            entry_cost="1",
            shares="5",
            price="0.2",
            fee=format(fee, "f"),
            payout=format(payout, "f"),
            released_cash_since_previous_entry="0" if i == 1 else format(payout, "f"),
            balance_before=format(before, "f"),
            balance_after=format(after, "f"),
        )
        doc["cash_release_receipts"][i - 1]["amount"] = format(payout, "f")
        doc["settlement_receipts"][i - 1]["payout"] = format(payout, "f")
        previous_after = after
    doc["closing_balance"] = format(previous_after + payout, "f")
    result = validate_bundle_document(doc)
    assert result["offline_consistency_passed"] is True
    assert Decimal(result["ending_cash"]) == Decimal("10.44")


def test_executed_price_must_follow_market_tick_size():
    doc = bundle()
    doc["events"][0]["price"] = "0.503"
    result = validate_bundle_document(doc)
    assert result["offline_consistency_passed"] is False
    assert result["failure_reason"] == "price_not_on_tick_size"


def test_market_constraints_are_required():
    doc = bundle()
    del doc["market_windows"][0]["tick_size"]
    result = validate_bundle_document(doc)
    assert result["offline_consistency_passed"] is False
    assert result["failure_reason"] == "tick_size_must_be_finite_decimal"


def test_depth_evidence_is_required_for_each_compound_fill():
    doc = bundle()
    del doc["depth_snapshots"]
    result = validate_bundle_document(doc)
    assert result["offline_consistency_passed"] is False
    assert result["failure_reason"] == "exactly_ten_depth_snapshots_required"


def test_depth_must_cover_the_executed_share_quantity():
    doc = bundle()
    doc["depth_snapshots"][0]["available_shares"] = "7"
    result = validate_bundle_document(doc)
    assert result["offline_consistency_passed"] is False
    assert result["failure_reason"] == "depth_available_shares_insufficient"


def test_depth_snapshot_must_be_received_before_entry():
    doc = bundle()
    after_entry = doc["events"][0]["entry_ts_ms"] + 1
    doc["depth_snapshots"][0]["event_ms"] = after_entry
    doc["depth_snapshots"][0]["received_ms"] = after_entry
    doc["events"][0]["depth_ref"]["event_ms"] = after_entry
    result = validate_bundle_document(doc)
    assert result["offline_consistency_passed"] is False
    assert result["failure_reason"] == "depth_snapshot_after_entry"


def test_depth_reference_must_match_the_bound_snapshot():
    doc = bundle()
    doc["events"][0]["depth_ref"]["raw_payload_sha256"] = "0" * 64
    result = validate_bundle_document(doc)
    assert result["offline_consistency_passed"] is False
    assert result["failure_reason"] == "depth_ref_not_in_bundle"


def test_taker_execution_cannot_be_better_than_the_bound_ask():
    doc = bundle()
    doc["depth_snapshots"][0]["ask_price"] = "0.51"
    result = validate_bundle_document(doc)
    assert result["offline_consistency_passed"] is False
    assert result["failure_reason"] == "executed_price_below_depth_ask"


def test_settlement_receipt_is_required_and_bound_to_each_fill():
    doc = bundle()
    del doc["settlement_receipts"]
    result = validate_bundle_document(doc)
    assert result["offline_consistency_passed"] is False
    assert result["failure_reason"] == "exactly_ten_settlement_receipts_required"


def test_settlement_payout_must_match_the_cash_release_payout():
    doc = bundle()
    doc["settlement_receipts"][0]["payout"] = "0"
    result = validate_bundle_document(doc)
    assert result["offline_consistency_passed"] is False
    assert result["failure_reason"] == "settlement_payout_mismatch"


def test_unconfirmed_or_non_independent_account_evidence_fails_closed():
    for field, value, reason in (
        ("status", "pending", "authenticated_event_not_confirmed"),
        ("status", "matched", "authenticated_event_not_confirmed"),
        ("status", "delayed", "authenticated_event_not_confirmed"),
        ("status", "mined", "authenticated_event_not_confirmed"),
        ("cash_reconciled", False, "cash_reconciliation_not_confirmed"),
        ("independent_checkpoint", False, "independent_checkpoint_not_confirmed"),
    ):
        doc = bundle()
        doc["events"][0][field] = value
        result = validate_bundle_document(doc)
        assert result["offline_consistency_passed"] is False
        assert result["failure_reason"] == reason


def test_strategy_binding_must_match_frozen_h30_config():
    doc = bundle()
    doc["strategy"]["id"] = "other-strategy"
    result = validate_bundle_document(doc)
    assert result["offline_consistency_passed"] is False
    assert result["failure_reason"] == "strategy_id_not_frozen"


def test_strategy_decision_reference_and_hash_are_required_per_fill():
    doc = bundle()
    del doc["events"][0]["strategy_decision_sha256"]
    result = validate_bundle_document(doc)
    assert result["offline_consistency_passed"] is False
    assert result["failure_reason"] == "strategy_decision_sha256_required"


def test_two_independent_ledgers_cannot_be_concatenated():
    doc = bundle()
    doc["events"][5]["ledger_id"] = "ledger-2"
    result = validate_bundle_document(doc)
    assert result["offline_consistency_passed"] is False
    assert result["failure_reason"] == "ledger_id_mismatch"


def test_two_five_fill_ledgers_with_fresh_rows_cannot_be_concatenated():
    doc = bundle()
    first = [account_event(i, ledger_id="ledger-a", chain_id="chain-a") for i in range(1, 6)]
    second = [account_event(i, ledger_id="ledger-b", chain_id="chain-b") for i in range(6, 11)]
    doc["events"] = first + second
    result = validate_bundle_document(doc)
    assert result["offline_consistency_passed"] is False
    assert result["failure_reason"] == "ledger_id_mismatch"


def test_cash_reset_between_fills_cannot_be_two_compound_ledgers():
    doc = bundle()
    # Keep the ledger, chain and sequence identities intact, but reset the
    # sixth fill to a fresh $10 balance.  The evolving cash ledger must reject
    # this five-plus-five concatenation by its arithmetic link.
    doc["events"][5]["balance_before"] = "10"
    result = validate_bundle_document(doc)
    assert result["offline_consistency_passed"] is False
    assert result["failure_reason"] == "balance_before_arithmetic_mismatch"


def test_v2_requires_explicit_opening_and_closing_ledger_anchors():
    doc = bundle()
    del doc["starting_checkpoint_id"]
    result = validate_bundle_document(doc)
    assert result["offline_consistency_passed"] is False
    assert result["failure_reason"] == "starting_checkpoint_id_required"

    doc = bundle()
    del doc["closing_balance_checkpoint_id"]
    result = validate_bundle_document(doc)
    assert result["offline_consistency_passed"] is False
    assert result["failure_reason"] == "closing_balance_checkpoint_id_required"


def test_sequence_reset_and_checkpoint_link_fail_closed():
    doc = bundle()
    doc["events"][5]["sequence_no"] = 5
    assert validate_bundle_document(doc)["failure_reason"] == "sequence_no_must_be_exactly_one_to_ten"
    doc = bundle()
    doc["events"][4]["previous_checkpoint_id"] = "unrelated"
    assert validate_bundle_document(doc)["failure_reason"] == "previous_checkpoint_link_mismatch"


def test_numeric_balance_mismatch_is_not_hidden_by_boolean_checkpoint_flags():
    doc = bundle()
    doc["events"][3]["balance_after"] = "999"
    result = validate_bundle_document(doc)
    assert result["offline_consistency_passed"] is False
    assert result["failure_reason"] == "balance_after_arithmetic_mismatch"


def test_current_fill_payout_cannot_be_released_before_its_entry():
    doc = bundle()
    doc["cash_release_receipts"][0]["release_ts_ms"] = doc["events"][0]["entry_ts_ms"]
    result = validate_bundle_document(doc)
    assert result["offline_consistency_passed"] is False
    assert result["failure_reason"] == "cash_release_before_entry"


def test_prior_release_at_next_entry_timestamp_is_not_admissible():
    doc = bundle()
    next_entry = doc["events"][1]["entry_ts_ms"]
    doc["cash_release_receipts"][0]["release_ts_ms"] = next_entry
    doc["events"][0]["cash_available_ts_ms"] = next_entry
    result = validate_bundle_document(doc)
    assert result["offline_consistency_passed"] is False
    assert result["failure_reason"] == "cash_release_timestamp_must_precede_next_entry"


def test_source_is_bound_to_each_fill_and_must_precede_entry():
    doc = bundle()
    after_entry = doc["events"][0]["entry_ts_ms"] + 1
    doc["source_events"][0]["timestamp"] = after_entry
    doc["source_events"][0]["received_ms"] = after_entry + 100
    doc["source_events"][0]["payload"]["timestamp"] = after_entry
    doc["events"][0]["source_ref"]["event_ms"] = after_entry
    result = validate_bundle_document(doc)
    assert result["offline_consistency_passed"] is False
    assert result["failure_reason"] == "source_event_after_entry"


def test_source_before_market_window_is_rejected():
    doc = bundle()
    source = [source_event(1, event_ms=START - 1)] + [source_event(i) for i in range(2, 11)]
    result = validate_bundle_document(bundle(source=source))
    assert result["offline_consistency_passed"] is False
    assert result["failure_reason"] == "source_event_after_market_window"


def test_source_gap_blocks_even_when_account_chain_is_valid():
    source = [source_event(1), source_event(3)]
    result = validate_bundle_document(bundle(source=source))
    assert result["offline_consistency_passed"] is False
    assert result["failure_reason"] == "source_continuity_not_admissible"
    assert result["source_continuity_ok"] is False


def test_market_slug_epoch_and_window_identity_are_exact():
    doc = bundle()
    doc["market_windows"][0]["slug"] = "btc-updown-5m-1800000001"
    result = validate_bundle_document(doc)
    assert result["offline_consistency_passed"] is False
    assert result["failure_reason"] == "btc_slug_window_start_mismatch"


def test_non_btc_five_minute_market_cannot_enter():
    doc = bundle()
    doc["market_windows"][0]["slug"] = "btc-updown-15m-1800000000"
    result = validate_bundle_document(doc)
    assert result["offline_consistency_passed"] is False
    assert result["failure_reason"] == "btc_five_minute_slug_required"


def test_zero_fee_counterfactual_is_not_a_canary():
    doc = bundle()
    doc["events"][0]["fee"] = "0"
    result = validate_bundle_document(doc)
    assert result["offline_consistency_passed"] is False
    assert result["failure_reason"] == "zero_fee_not_admissible"


def test_maker_execution_is_not_a_canary():
    doc = bundle()
    doc["events"][0]["execution_role"] = "MAKER"
    result = validate_bundle_document(doc)
    assert result["offline_consistency_passed"] is False
    assert result["failure_reason"] == "execution_role_must_be_taker"


def test_fee_schedule_must_match_frozen_taker_rate():
    doc = bundle()
    doc["events"][0]["fee_rate"] = "0.00"
    result = validate_bundle_document(doc)
    assert result["offline_consistency_passed"] is False
    assert result["failure_reason"] == "fee_rate_must_equal_frozen_taker_rate"


def test_taker_fee_amount_must_match_price_and_shares_formula():
    doc = bundle()
    doc["events"][0]["fee"] = "0.174"
    result = validate_bundle_document(doc)
    assert result["offline_consistency_passed"] is False
    assert result["failure_reason"] == "taker_fee_amount_mismatch"


def test_entry_cost_must_match_executed_shares_and_price():
    doc = bundle()
    doc["events"][0]["shares"] = "9"
    result = validate_bundle_document(doc)
    assert result["offline_consistency_passed"] is False
    assert result["failure_reason"] == "entry_cost_price_arithmetic_mismatch"


def test_required_fills_cannot_be_lowered_to_a_lucky_prefix():
    doc = bundle()
    doc["required_fills"] = 1
    result = validate_bundle_document(doc, required_fills=1)
    assert result["offline_consistency_passed"] is False
    assert result["failure_reason"] == "required_fills_fixed_at_ten"


def test_missing_source_or_market_identity_fails_closed():
    doc = bundle()
    del doc["source_events"]
    assert validate_bundle_document(doc)["failure_reason"] == "source_events_required"
    doc = bundle()
    doc["events"][0]["market_id"] = "other"
    assert validate_bundle_document(doc)["failure_reason"] == "account_event_market_identity_mismatch"


def test_source_must_cover_every_account_market():
    doc = bundle()
    doc["market_windows"].append({
        "market_id": "m-2", "asset": "btc", "slug": "btc-updown-5m-1800000300",
        "window_start_ms": START + 300_000, "window_end_ms": START + 600_000,
        "min_order_size_unit": "shares", "min_order_size": "5", "min_order_shares": "5", "tick_size": "0.01",
    })
    doc["events"][0]["market_id"] = "m-2"
    result = validate_bundle_document(doc)
    assert result["offline_consistency_passed"] is False
    assert result["failure_reason"] == "source_account_market_coverage_mismatch"


def test_unhashable_source_sequence_fails_closed():
    doc = bundle()
    doc["events"][0]["source_ref"]["seq"] = []
    result = validate_bundle_document(doc)
    assert result["offline_consistency_passed"] is False
    assert result["failure_reason"] == "source_ref_invalid"


def test_unhashable_event_market_id_fails_closed():
    doc = bundle()
    doc["events"][0]["market_id"] = []
    result = validate_bundle_document(doc)
    assert result["offline_consistency_passed"] is False
    assert result["failure_reason"] == "account_event_market_identity_mismatch"


def test_mixed_sequence_types_fail_closed_before_sorting():
    doc = bundle()
    doc["events"][0]["sequence_no"] = "1"
    result = validate_bundle_document(doc)
    assert result["offline_consistency_passed"] is False
    assert result["failure_reason"] == "sequence_no_invalid"


def assert_unverified_receipts(result):
    assert result["evidence_scope"] == "offline_bundle_consistency_only"
    assert result["gate_passed"] is False
    assert result["receipt_authenticity_verified"] is False
    assert result["actual_fills"] == 0
    assert result["verified_canary_fills"] == 0


@pytest.mark.parametrize("case", ["consistent", "incomplete", "invalid", "self_attested"])
def test_offline_bundle_never_authenticates_actual_receipts(case):
    doc = bundle()
    if case == "incomplete":
        doc["events"].pop()
    elif case == "invalid":
        doc = {}
    elif case == "self_attested":
        # These are supplied claims, just like owner-1/trade-N/tx-N and the
        # syntactically valid settlement hashes.  No external source is checked.
        doc.update(receipt_authenticity_verified=True, verified_canary_fills=10,
                   actual_fills=10, gate_passed=True, evidence_scope="independently_authenticated")
        for receipt in doc["settlement_receipts"]:
            receipt["raw_payload_sha256"] = hashlib.sha256(
                ("unverified-" + receipt["receipt_id"]).encode()).hexdigest()
    result = validate_bundle_document(doc)
    assert_unverified_receipts(result)
    assert result["offline_consistency_passed"] is (case in {"consistent", "self_attested"})
    if result["offline_consistency_passed"]:
        assert result["fills"] == 10
        assert result["settlement_evidence_verified"] is True  # internal consistency only
