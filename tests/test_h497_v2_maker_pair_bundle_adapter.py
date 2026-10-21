import pytest

from analysis.h497_v2_maker_pair_bundle_adapter import _event, _leg, audit
from analysis.authenticated_v2_maker_pair_bundle_adapter import assemble_authenticated_v2_maker_pair_event


def test_h497_integrated_adapter_is_offline_only():
    result = audit()
    assert result["all_checks_passed"] is True
    assert result["h490_offline_consistency_passed"] is True
    assert result["h496_offline_consistency_passed"] is True
    assert result["paper_pairs"] == 10
    assert result["paper_ending_cash"] == "60"
    assert result["gate_passed"] is False
    assert result["actual_fills"] == 0


def test_h497_rejects_path_field_conflict():
    row = _event(1)
    assert row["v2_path_contract_validated_offline"] is True
    up_order, up_trade = _leg("up", 1)
    down_order, down_trade = _leg("down", 1)
    pair_keys = (
        "owner", "ledger_id", "chain_id", "market_id", "pair_id", "sequence_no",
        "previous_checkpoint_id", "entry_ts", "cash_available_ts",
        "released_cash_since_previous_entry", "balance_before", "balance_after",
        "entry_cost", "fee", "payout", "balance_checkpoint_id",
        "cash_reconciled", "independent_checkpoint",
    )
    path_keys = (
        "exchange_version", "exchange_contract", "match_path", "maker_order_filled_refs",
        "asset_transfer_refs", "payout_receipt_id", "cash_release_receipt_id",
        "cash_token_balance_checkpoint_id", "maker_order_filled_ts", "payout_ts",
        "cash_available_ts", "cash_token_balance_before", "cash_token_balance_after",
    )
    with pytest.raises(ValueError, match="path_field_conflicts_with_pair:owner"):
        assemble_authenticated_v2_maker_pair_event(
            up_order_event=up_order,
            up_trade_event=up_trade,
            down_order_event=down_order,
            down_trade_event=down_trade,
            pair_fields={key: row[key] for key in pair_keys},
            settlement_fields={key: row[key] for key in ("merge_receipt_id", "settlement_receipt_id", "cash_release_receipt_id")},
            path_fields={**{key: row[key] for key in path_keys}, "owner": "wrong-owner"},
        )
