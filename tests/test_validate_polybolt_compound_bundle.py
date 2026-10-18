import hashlib
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "analysis"))

from validate_polybolt_compound_bundle import validate_bundle_document


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


def account_event(i: int, *, ledger_id: str = "ledger-1", chain_id: str = "chain-1") -> dict[str, object]:
    entry_ms = START + (i - 1) * 10_000 + 1_000
    release_ms = entry_ms + 5_000
    before = Decimal("10") + Decimal("0.43") * (i - 1)
    after = before - Decimal("5.07")
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
        "fee": "0.07",
        "payout": "5.50",
        "released_cash_since_previous_entry": "0" if i == 1 else "5.50",
        "balance_before": format(before, "f"),
        "balance_after": format(after, "f"),
        "source_ref": {
            "market_id": MARKET_ID,
            "capture_id": "capture-a",
            "seq": i,
            "raw_payload_sha256": digest,
            "event_ms": START + (i - 1) * 1_000,
        },
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
        "closing_balance": "14.3",
        "closing_balance_checkpoint_id": "closing",
        "market_windows": [{
            "market_id": MARKET_ID,
            "asset": "btc",
            "slug": "btc-updown-5m-1800000000",
            "window_start_ms": START,
            "window_end_ms": START + 300_000,
        }],
        "cash_release_receipts": [
            {"receipt_id": f"cash-release-{i}", "sequence_no": i, "amount": "5.50", "release_ts_ms": START + (i - 1) * 10_000 + 6_000}
            for i in range(1, 11)
        ],
        "source_events": [source_event(i) for i in range(1, 11)] if source is None else source,
        "events": [account_event(i) for i in range(1, 11)] if events is None else events,
    }


def test_polybolt_source_and_authenticated_cash_chain_admit_together():
    result = validate_bundle_document(bundle())
    assert result["gate_passed"] is True
    assert result["source_continuity_ok"] is True
    assert result["fills"] == 10
    assert result["same_ledger_identity_verified"] is True
    assert result["cash_amounts_reconciled"] is True
    assert result["market_window_count"] == 1
    assert "owner-1" not in str(result)


def test_two_independent_ledgers_cannot_be_concatenated():
    doc = bundle()
    doc["events"][5]["ledger_id"] = "ledger-2"
    result = validate_bundle_document(doc)
    assert result["gate_passed"] is False
    assert result["failure_reason"] == "ledger_id_mismatch"


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
    assert result["gate_passed"] is False
    assert result["failure_reason"] == "balance_after_arithmetic_mismatch"


def test_current_fill_payout_cannot_be_released_before_its_entry():
    doc = bundle()
    doc["cash_release_receipts"][0]["release_ts_ms"] = doc["events"][0]["entry_ts_ms"]
    result = validate_bundle_document(doc)
    assert result["gate_passed"] is False
    assert result["failure_reason"] == "cash_release_before_entry"


def test_source_is_bound_to_each_fill_and_must_precede_entry():
    doc = bundle()
    after_entry = doc["events"][0]["entry_ts_ms"] + 1
    doc["source_events"][0]["timestamp"] = after_entry
    doc["source_events"][0]["received_ms"] = after_entry + 100
    doc["source_events"][0]["payload"]["timestamp"] = after_entry
    doc["events"][0]["source_ref"]["event_ms"] = after_entry
    result = validate_bundle_document(doc)
    assert result["gate_passed"] is False
    assert result["failure_reason"] == "source_event_after_entry"


def test_source_gap_blocks_even_when_account_chain_is_valid():
    source = [source_event(1), source_event(3)]
    result = validate_bundle_document(bundle(source=source))
    assert result["gate_passed"] is False
    assert result["failure_reason"] == "source_continuity_not_admissible"
    assert result["source_continuity_ok"] is False


def test_market_slug_epoch_and_window_identity_are_exact():
    doc = bundle()
    doc["market_windows"][0]["slug"] = "btc-updown-5m-1800000001"
    result = validate_bundle_document(doc)
    assert result["gate_passed"] is False
    assert result["failure_reason"] == "btc_slug_window_start_mismatch"


def test_non_btc_five_minute_market_cannot_enter():
    doc = bundle()
    doc["market_windows"][0]["slug"] = "btc-updown-15m-1800000000"
    result = validate_bundle_document(doc)
    assert result["gate_passed"] is False
    assert result["failure_reason"] == "btc_five_minute_slug_required"


def test_zero_fee_counterfactual_is_not_a_canary():
    doc = bundle()
    doc["events"][0]["fee"] = "0"
    result = validate_bundle_document(doc)
    assert result["gate_passed"] is False
    assert result["failure_reason"] == "zero_fee_not_admissible"


def test_maker_execution_is_not_a_canary():
    doc = bundle()
    doc["events"][0]["execution_role"] = "MAKER"
    result = validate_bundle_document(doc)
    assert result["gate_passed"] is False
    assert result["failure_reason"] == "execution_role_must_be_taker"


def test_fee_schedule_must_match_frozen_taker_rate():
    doc = bundle()
    doc["events"][0]["fee_rate"] = "0.00"
    result = validate_bundle_document(doc)
    assert result["gate_passed"] is False
    assert result["failure_reason"] == "fee_rate_must_equal_frozen_taker_rate"


def test_required_fills_cannot_be_lowered_to_a_lucky_prefix():
    doc = bundle()
    doc["required_fills"] = 1
    result = validate_bundle_document(doc, required_fills=1)
    assert result["gate_passed"] is False
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
    })
    doc["events"][0]["market_id"] = "m-2"
    result = validate_bundle_document(doc)
    assert result["gate_passed"] is False
    assert result["failure_reason"] == "source_account_market_coverage_mismatch"


def test_unhashable_source_sequence_fails_closed():
    doc = bundle()
    doc["events"][0]["source_ref"]["seq"] = []
    result = validate_bundle_document(doc)
    assert result["gate_passed"] is False
    assert result["failure_reason"] == "source_ref_invalid"


def test_unhashable_event_market_id_fails_closed():
    doc = bundle()
    doc["events"][0]["market_id"] = []
    result = validate_bundle_document(doc)
    assert result["gate_passed"] is False
    assert result["failure_reason"] == "account_event_market_identity_mismatch"


def test_mixed_sequence_types_fail_closed_before_sorting():
    doc = bundle()
    doc["events"][0]["sequence_no"] = "1"
    result = validate_bundle_document(doc)
    assert result["gate_passed"] is False
    assert result["failure_reason"] == "sequence_no_invalid"
