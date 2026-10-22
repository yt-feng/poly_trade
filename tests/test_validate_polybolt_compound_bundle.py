import hashlib
import json
import sys

from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "analysis"))

from validate_polybolt_compound_bundle import (
    H530_STRATEGY_CONFIG_SHA256,
    H530_STRATEGY_ID,
    H530_STRATEGY_PROFILE,
    H530_STRATEGY_VERSION,
    H534_STRATEGY_CONFIG_SHA256,
    H534_STRATEGY_ID,
    H534_STRATEGY_PROFILE,
    H534_STRATEGY_VERSION,
    H549_STRATEGY_CONFIG_SHA256,
    H549_STRATEGY_ID,
    H549_STRATEGY_PROFILE,
    H549_STRATEGY_VERSION,
    H561_STRATEGY_CONFIG_SHA256,
    H561_STRATEGY_ID,
    H561_STRATEGY_PROFILE,
    H561_STRATEGY_VERSION,
    H578_STRATEGY_CONFIG_SHA256,
    H578_STRATEGY_ID,
    H578_STRATEGY_PROFILE,
    H578_STRATEGY_VERSION,
    H600_STRATEGY_CONFIG_SHA256,
    H600_STRATEGY_ID,
    H600_STRATEGY_PROFILE,
    H600_STRATEGY_VERSION,
    PRIVATE_STRATEGY_PROFILE,
    FROZEN_STRATEGY_CONFIG_SHA256,
    FROZEN_STRATEGY_ID,
    FROZEN_STRATEGY_VERSION,
    _expected_entry_terms,
    validate_bundle_document,
)


START = 1_800_000_000_000
MARKET_ID = "m-1"


def attach_strategy_trace(
    document: dict[str, object],
    fractions: list[str],
    *,
    track_profits: bool = True,
) -> dict[str, object]:
    decisions = []
    prior_profitable = 0
    for event, fraction in zip(document["events"], fractions, strict=True):
        decision = {
            "decision_ref": event["strategy_decision_ref"],
            "sequence_no": event["sequence_no"],
            "cash_fraction": fraction,
            "profitable_released_exits_before": prior_profitable if track_profits else 0,
        }
        event["strategy_decision_sha256"] = hashlib.sha256(
            json.dumps(decision, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        decisions.append(decision)
        if track_profits:
            if Decimal(str(event["payout"])) > Decimal(str(event["entry_cost"])) + Decimal(str(event["fee"])):
                prior_profitable += 1
            else:
                prior_profitable = 0
    document["strategy_decisions"] = decisions
    return document


def attach_depth_proxy_trace(
    document: dict[str, object], depth: str = "1000", cash_fraction: str = "1.0"
) -> dict[str, object]:
    decisions = []
    for event in document["events"]:
        balance_before = Decimal(str(event["balance_before"]))
        price = Decimal(str(event["price"]))
        per_share = price + Decimal("0.07") * price * (Decimal("1") - price)
        full_shares = max(Decimal("5"), balance_before / per_share)
        ratio = Decimal(depth) / full_shares
        decision = {
            "decision_ref": event["strategy_decision_ref"],
            "sequence_no": event["sequence_no"],
            "cash_fraction": cash_fraction,
            "position_side": "up",
            "entry_sell_depth_shares": depth,
            "depth_capacity_ratio": format(ratio, "f"),
        }
        event["strategy_decision_sha256"] = hashlib.sha256(
            json.dumps(decision, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        decisions.append(decision)
    document["strategy_decisions"] = decisions
    return document


def attach_favorite_depth_trace(
    document: dict[str, object], depth: str = "90", favorite_ask: str = "0.50", cash_fraction: str = "1.0"
) -> dict[str, object]:
    decisions = []
    for event in document["events"]:
        balance_before = Decimal(str(event["balance_before"]))
        price = Decimal(str(event["price"]))
        per_share = price + Decimal("0.07") * price * (Decimal("1") - price)
        full_shares = max(Decimal("5"), balance_before / per_share)
        ratio = Decimal(depth) / full_shares
        decision = {
            "decision_ref": event["strategy_decision_ref"],
            "sequence_no": event["sequence_no"],
            "cash_fraction": cash_fraction,
            "position_side": "up",
            "entry_sell_depth_shares": depth,
            "depth_capacity_ratio": format(ratio, "f"),
            "favorite_ask_price": favorite_ask,
        }
        event["strategy_decision_sha256"] = hashlib.sha256(
            json.dumps(decision, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        decisions.append(decision)
    document["strategy_decisions"] = decisions
    return document


def configure_adaptive_fixture(document: dict[str, object], fractions: list[str]) -> dict[str, object]:
    """Build a profitable synthetic chain whose size follows each fraction."""
    cash = Decimal("10")
    previous_payout = Decimal("0")
    for event, receipt, settlement, orderfilled, fraction in zip(
        document["events"], document["cash_release_receipts"], document["settlement_receipts"],
        document["orderfilled_records"], fractions, strict=True
    ):
        fraction_decimal = Decimal(fraction)
        price = Decimal("0.5")
        target = max(Decimal("5"), fraction_decimal * cash)
        shares = max(Decimal("5"), target / price)
        cost = shares * price
        fee = shares * Decimal("0.07") * price * (Decimal("1") - price)
        payout = cost + fee + Decimal("0.50")
        before = cash
        after = before - cost - fee
        event.update({
            "shares": format(shares, "f"),
            "price": format(price, "f"),
            "entry_cost": format(cost, "f"),
            "fee": format(fee, "f"),
            "payout": format(payout, "f"),
            "released_cash_since_previous_entry": format(previous_payout, "f"),
            "balance_before": format(before, "f"),
            "balance_after": format(after, "f"),
        })
        orderfilled.update({
            "size": format(shares, "f"),
            "price": format(price, "f"),
            "cash_amount": format(cost, "f"),
        })
        receipt["amount"] = format(payout, "f")
        settlement["payout"] = format(payout, "f")
        previous_payout = payout
        cash = after + payout
    document["closing_balance"] = format(cash, "f")
    return document


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
        "position_side": "up",
        "sell_available_shares_2m": "1000",
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
        "order_id": f"order-{i}",
        "order_status": "MATCHED",
        "trade_id": f"trade-{i}",
        "transaction_ref": f"tx-{i}",
        "orderfilled_event_id": f"tx-{i}:{i}",
        "settlement_receipt_id": f"settlement-{i}",
        "cash_release_receipt_id": f"cash-release-{i}",
        "execution_role": "TAKER",
        "trader_side": "TAKER",
        "trader_side_source": "SETTLED_TAKER_LEG",
        "position_side": "up",
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
                "settlement_transaction_hash": f"tx-{i}",
                "settlement_status": "CONFIRMED",
                "settlement_reverted": False,
                "payout": "5.435",
            }
            for i in range(1, 11)
        ],
        "orderfilled_records": [
            {
                "event_id": f"tx-{i}:{i}",
                "tx_hash": f"tx-{i}",
                "log_index": i,
                "block_number": 1000 + i,
                "market_id": MARKET_ID,
                "taker": "owner-1",
                "side": "BUY",
                "sign": 1,
                "size": "10",
                "price": "0.5",
                "cash_amount": "5",
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


@pytest.mark.parametrize("field", ["trade_id", "transaction_ref", "orderfilled_event_id"])
def test_execution_identity_is_required_and_unique(field):
    doc = bundle()
    del doc["events"][0][field]
    result = validate_bundle_document(doc)
    assert result["offline_consistency_passed"] is False
    assert result["failure_reason"] == f"{field}_required"

    doc = bundle()
    doc["events"][1][field] = doc["events"][0][field]
    result = validate_bundle_document(doc)
    assert result["offline_consistency_passed"] is False
    assert result["failure_reason"] == f"{field}_reused"


def test_fixed_half_cash_profile_is_explicitly_bound():
    doc = bundle()
    doc["strategy"] = {
        "id": H530_STRATEGY_ID,
        "version": H530_STRATEGY_VERSION,
        "config_sha256": H530_STRATEGY_CONFIG_SHA256,
    }
    configure_adaptive_fixture(doc, ["0.5"] * 10)
    attach_strategy_trace(doc, ["0.5"] * 10, track_profits=False)
    legacy = validate_bundle_document(doc)
    assert legacy["offline_consistency_passed"] is False
    assert legacy["failure_reason"] == "strategy_id_not_frozen"
    fixed = validate_bundle_document(doc, strategy_profile=H530_STRATEGY_PROFILE)
    assert fixed["offline_consistency_passed"] is True


def test_dense_fixed_fraction_profile_is_distinct_and_bound():
    doc = bundle()
    doc["strategy"] = {
        "id": H534_STRATEGY_ID,
        "version": H534_STRATEGY_VERSION,
        "config_sha256": H534_STRATEGY_CONFIG_SHA256,
    }
    configure_adaptive_fixture(doc, ["0.55"] * 10)
    attach_strategy_trace(doc, ["0.55"] * 10, track_profits=False)
    assert validate_bundle_document(doc, strategy_profile=H534_STRATEGY_PROFILE)["offline_consistency_passed"] is True
    doc["strategy_decisions"][0]["cash_fraction"] = "0.5"
    result = validate_bundle_document(doc, strategy_profile=H534_STRATEGY_PROFILE)
    assert result["offline_consistency_passed"] is False
    assert result["failure_reason"] == "strategy_decision_trace_hash_mismatch"


def test_known_profile_requires_a_per_fill_strategy_trace():
    doc = bundle()
    doc["strategy"] = {
        "id": H530_STRATEGY_ID,
        "version": H530_STRATEGY_VERSION,
        "config_sha256": H530_STRATEGY_CONFIG_SHA256,
    }
    result = validate_bundle_document(doc, strategy_profile=H530_STRATEGY_PROFILE)
    assert result["offline_consistency_passed"] is False
    assert result["failure_reason"] == "strategy_decision_trace_required"


def test_strategy_trace_hash_and_state_are_bound_per_fill():
    doc = bundle()
    doc["strategy"] = {
        "id": H530_STRATEGY_ID,
        "version": H530_STRATEGY_VERSION,
        "config_sha256": H530_STRATEGY_CONFIG_SHA256,
    }
    configure_adaptive_fixture(doc, ["0.5"] * 10)
    attach_strategy_trace(doc, ["0.5"] * 10, track_profits=False)
    assert validate_bundle_document(doc, strategy_profile=H530_STRATEGY_PROFILE)["offline_consistency_passed"] is True
    doc["strategy_decisions"][0]["cash_fraction"] = "0.55"
    result = validate_bundle_document(doc, strategy_profile=H530_STRATEGY_PROFILE)
    assert result["offline_consistency_passed"] is False
    assert result["failure_reason"] == "strategy_decision_trace_hash_mismatch"


def test_adaptive_profile_binds_actual_entry_size_to_fraction():
    doc = bundle()
    doc["strategy"] = {
        "id": H549_STRATEGY_ID,
        "version": H549_STRATEGY_VERSION,
        "config_sha256": H549_STRATEGY_CONFIG_SHA256,
    }
    configure_adaptive_fixture(doc, ["0.55"] + ["0.5875"] * 9)
    attach_strategy_trace(doc, ["0.55"] + ["0.5875"] * 9)
    assert validate_bundle_document(doc, strategy_profile=H549_STRATEGY_PROFILE)["offline_consistency_passed"] is True
    doc["events"][0]["shares"] = "10"
    doc["events"][0]["entry_cost"] = "5"
    doc["events"][0]["fee"] = "0.175"
    cash = Decimal("10")
    for index, (event, receipt) in enumerate(zip(doc["events"], doc["cash_release_receipts"], strict=True)):
        event["balance_before"] = format(cash, "f")
        cash = cash - Decimal(str(event["entry_cost"])) - Decimal(str(event["fee"]))
        event["balance_after"] = format(cash, "f")
        cash += Decimal(str(receipt["amount"]))
    doc["closing_balance"] = format(cash, "f")
    result = validate_bundle_document(doc, strategy_profile=H549_STRATEGY_PROFILE)
    assert result["offline_consistency_passed"] is False
    assert result["failure_reason"] == "strategy_entry_size_mismatch"


def test_h561_profile_binds_side_specific_entry_depth_to_fraction():
    doc = bundle()
    doc["strategy"] = {
        "id": H561_STRATEGY_ID,
        "version": H561_STRATEGY_VERSION,
        "config_sha256": H561_STRATEGY_CONFIG_SHA256,
    }
    cash = Decimal("10")
    previous_payout = Decimal("0")
    for event, receipt, settlement in zip(
        doc["events"], doc["cash_release_receipts"], doc["settlement_receipts"], strict=True
    ):
        shares, cost, expected_fee = _expected_entry_terms(cash, Decimal("0.5"), Decimal("1"))
        # Keep the synthetic cash checkpoint exactly nonnegative; the tiny
        # Decimal remainder from the sizing division is within the validator's
        # fee tolerance and is not the behavior under test here.
        fee = cash - cost
        payout = cost + fee + Decimal("0.50")
        event.update({
            "shares": format(shares, "f"), "price": "0.5", "entry_cost": format(cost, "f"),
            "fee": format(fee, "f"), "payout": format(payout, "f"),
            "released_cash_since_previous_entry": format(previous_payout, "f"),
            "balance_before": format(cash, "f"),
            "balance_after": format(cash - cost - fee, "f"),
        })
        receipt["amount"] = format(payout, "f")
        settlement["payout"] = format(payout, "f")
        previous_payout = payout
        cash = cash - cost - fee + payout
    doc["closing_balance"] = format(cash, "f")
    attach_depth_proxy_trace(doc)
    result = validate_bundle_document(doc, strategy_profile=H561_STRATEGY_PROFILE)
    assert result["offline_consistency_passed"] is True

    doc["depth_snapshots"][0]["sell_available_shares_2m"] = "10"
    result = validate_bundle_document(doc, strategy_profile=H561_STRATEGY_PROFILE)
    assert result["offline_consistency_passed"] is False
    assert result["failure_reason"] == "strategy_trace_sell_depth_mismatch"


def test_h578_profile_binds_the_separate_3_5_7_depth_contract():
    assert H578_STRATEGY_PROFILE != H561_STRATEGY_PROFILE
    doc = bundle()
    doc["strategy"] = {
        "id": H578_STRATEGY_ID,
        "version": H578_STRATEGY_VERSION,
        "config_sha256": H578_STRATEGY_CONFIG_SHA256,
    }
    configure_adaptive_fixture(doc, ["0.75"] * 10)
    for snapshot in doc["depth_snapshots"]:
        snapshot["sell_available_shares_2m"] = "90"
    attach_depth_proxy_trace(doc, depth="90", cash_fraction="0.75")
    result = validate_bundle_document(doc, strategy_profile=H578_STRATEGY_PROFILE)
    assert result["offline_consistency_passed"] is True

    # The same trace is intentionally in the H578-only ratio band: H561's
    # higher first threshold must reject it rather than silently reinterpreting
    # the candidate as H561.
    doc["strategy"] = {
        "id": H561_STRATEGY_ID,
        "version": H561_STRATEGY_VERSION,
        "config_sha256": H561_STRATEGY_CONFIG_SHA256,
    }
    result = validate_bundle_document(doc, strategy_profile=H561_STRATEGY_PROFILE)
    assert result["offline_consistency_passed"] is False
    assert result["failure_reason"] == "strategy_decision_trace_fraction_mismatch"


def test_h600_profile_binds_favorite_floor_and_depth_trace_separately():
    assert H600_STRATEGY_PROFILE not in {H561_STRATEGY_PROFILE, H578_STRATEGY_PROFILE}
    doc = bundle()
    doc["strategy"] = {
        "id": H600_STRATEGY_ID,
        "version": H600_STRATEGY_VERSION,
        "config_sha256": H600_STRATEGY_CONFIG_SHA256,
    }
    configure_adaptive_fixture(doc, ["0.75"] * 10)
    for snapshot in doc["depth_snapshots"]:
        snapshot["sell_available_shares_2m"] = "90"
    attach_favorite_depth_trace(doc, depth="90", favorite_ask="0.50", cash_fraction="0.75")
    result = validate_bundle_document(doc, strategy_profile=H600_STRATEGY_PROFILE)
    assert result["offline_consistency_passed"] is True

    for snapshot in doc["depth_snapshots"]:
        snapshot["ask_price"] = "0.49"
    attach_favorite_depth_trace(doc, depth="90", favorite_ask="0.49", cash_fraction="0.75")
    result = validate_bundle_document(doc, strategy_profile=H600_STRATEGY_PROFILE)
    assert result["offline_consistency_passed"] is False
    assert result["failure_reason"] == "strategy_trace_favorite_ask_below_floor"


@pytest.mark.parametrize("field", ["order_id", "order_status"])
def test_h600_profile_requires_matched_order_identity(field):
    doc = bundle()
    doc["strategy"] = {
        "id": H600_STRATEGY_ID,
        "version": H600_STRATEGY_VERSION,
        "config_sha256": H600_STRATEGY_CONFIG_SHA256,
    }
    configure_adaptive_fixture(doc, ["0.75"] * 10)
    for snapshot in doc["depth_snapshots"]:
        snapshot["sell_available_shares_2m"] = "90"
    attach_favorite_depth_trace(doc, depth="90", favorite_ask="0.50", cash_fraction="0.75")
    if field == "order_id":
        del doc["events"][0][field]
    else:
        doc["events"][0][field] = "CONFIRMED"
    result = validate_bundle_document(doc, strategy_profile=H600_STRATEGY_PROFILE)
    assert result["offline_consistency_passed"] is False
    assert result["failure_reason"] == ("order_id_required" if field == "order_id" else "order_status_must_be_matched")


def test_h600_profile_requires_canonical_orderfilled_record_binding():
    doc = bundle()
    doc["strategy"] = {
        "id": H600_STRATEGY_ID,
        "version": H600_STRATEGY_VERSION,
        "config_sha256": H600_STRATEGY_CONFIG_SHA256,
    }
    configure_adaptive_fixture(doc, ["0.75"] * 10)
    for snapshot in doc["depth_snapshots"]:
        snapshot["sell_available_shares_2m"] = "90"
    attach_favorite_depth_trace(doc, depth="90", favorite_ask="0.50", cash_fraction="0.75")

    missing = dict(doc)
    missing.pop("orderfilled_records")
    result = validate_bundle_document(missing, strategy_profile=H600_STRATEGY_PROFILE)
    assert result["failure_reason"] == "exactly_ten_orderfilled_records_required"

    mismatched = bundle()
    mismatched["strategy"] = doc["strategy"]
    configure_adaptive_fixture(mismatched, ["0.75"] * 10)
    for snapshot in mismatched["depth_snapshots"]:
        snapshot["sell_available_shares_2m"] = "90"
    attach_favorite_depth_trace(mismatched, depth="90", favorite_ask="0.50", cash_fraction="0.75")
    mismatched["events"][0]["transaction_ref"] = "tx-other"
    result = validate_bundle_document(mismatched, strategy_profile=H600_STRATEGY_PROFILE)
    assert result["failure_reason"] == "orderfilled_transaction_mismatch"


def test_private_exact_profile_is_supplied_without_public_config():
    private = {
        "id": "opaque-private-candidate",
        "version": "opaque-private-v1",
        "config": {"private_parameter": "opaque", "required_fills": 10},
    }
    digest = hashlib.sha256(json.dumps(private["config"], sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    doc = bundle()
    doc["strategy"] = {
        "id": private["id"],
        "version": private["version"],
        "config_sha256": digest,
    }
    result = validate_bundle_document(
        doc,
        strategy_profile=PRIVATE_STRATEGY_PROFILE,
        private_strategy_config=private,
    )
    assert result["offline_consistency_passed"] is True


def test_private_exact_profile_fails_closed_without_secret_config():
    doc = bundle()
    result = validate_bundle_document(doc, strategy_profile=PRIVATE_STRATEGY_PROFILE)
    assert result["offline_consistency_passed"] is False
    assert result["failure_reason"] == "private_strategy_profile_unavailable"


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


def test_five_share_fill_below_frozen_strategy_notional_is_rejected():
    """The H30 paper protocol requires $5 entry notional as well as 5 shares."""
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
    assert result["offline_consistency_passed"] is False
    assert result["failure_reason"] == "entry_cost_below_frozen_minimum_notional"


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


def test_settlement_grounded_taker_side_provenance_is_required():
    doc = bundle()
    del doc["events"][0]["trader_side_source"]
    result = validate_bundle_document(doc)
    assert result["offline_consistency_passed"] is False
    assert result["failure_reason"] == "trader_side_source_required"

    doc = bundle()
    doc["events"][0]["trader_side_source"] = "PUBLIC_PRINT"
    result = validate_bundle_document(doc)
    assert result["offline_consistency_passed"] is False
    assert result["failure_reason"] == "trader_side_source_must_be_settled_taker_leg"


def test_non_taker_side_cannot_enter_the_compound_bundle():
    doc = bundle()
    doc["events"][0]["trader_side"] = "MAKER"
    result = validate_bundle_document(doc)
    assert result["offline_consistency_passed"] is False
    assert result["failure_reason"] == "trader_side_must_be_taker"


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


def test_settlement_receipt_must_bind_to_the_account_transaction():
    doc = bundle()
    doc["settlement_receipts"][0]["settlement_transaction_hash"] = "unrelated-tx"
    result = validate_bundle_document(doc)
    assert result["offline_consistency_passed"] is False
    assert result["failure_reason"] == "settlement_transaction_mismatch"


def test_reverted_settlement_receipt_is_not_admissible():
    doc = bundle()
    doc["settlement_receipts"][0]["settlement_reverted"] = True
    result = validate_bundle_document(doc)
    assert result["offline_consistency_passed"] is False
    assert result["failure_reason"] == "settlement_must_not_be_reverted"


def test_nonterminal_settlement_receipt_is_not_admissible():
    doc = bundle()
    doc["settlement_receipts"][0]["settlement_status"] = "MATCHED"
    result = validate_bundle_document(doc)
    assert result["offline_consistency_passed"] is False
    assert result["failure_reason"] == "settlement_status_must_be_confirmed"


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
