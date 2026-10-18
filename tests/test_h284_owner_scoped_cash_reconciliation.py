import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "analysis"))

from h284_owner_scoped_cash_reconciliation import reconcile_observed_cash


def activity(kind, tx, ts, amount, side=""):
    return {
        "type": kind,
        "transaction_ref": tx,
        "timestamp": ts,
        "usdc_size": amount,
        "side": side,
    }


def transfer(tx, amount, source="other", target="other", token="cash"):
    return {
        "transaction_ref": tx,
        "token": token,
        "from": source,
        "to": target,
        "amount_base_units": amount,
    }


def test_trade_and_redeem_transfers_join_and_redeem_cash_is_distinct():
    activities = [
        activity("TRADE", "buy", 1, "5", "BUY"),
        activity("TRADE", "sell", 2, "5.5", "SELL"),
        activity("REDEEM", "redeem", 3, "5.5"),
        activity("TRADE", "buy2", 4, "5", "BUY"),
    ]
    transfers = [
        transfer("buy", 5_000_000, source="owner", target="exchange"),
        transfer("sell", 5_500_000, source="exchange", target="owner"),
        transfer("redeem", 5_500_000, source="adapter", target="owner"),
        transfer("buy2", 5_000_000, source="owner", target="exchange"),
    ]
    result = reconcile_observed_cash(
        activities, transfers, owner="owner", collateral_token="cash",
        initial_cash="10", payout_receipts=[
            {"transaction_ref": "redeem", "amount_base_units": 5_500_000}
        ],
    )
    assert result["exact_collateral_delta_join_transactions"] == 4
    assert result["payout_redeem_transaction_matches"] == 1
    assert result["cash_release_transaction_rows"] == 1
    assert result["historical_cash_reuse_evidence"] is True
    assert result["same_ledger_10_canary_eligible"] is False


def test_transfer_amount_mismatch_cannot_be_admitted():
    result = reconcile_observed_cash(
        [activity("TRADE", "buy", 1, "5", "BUY")],
        [transfer("buy", 4_999_999, source="owner", target="exchange")],
        owner="owner", collateral_token="cash",
    )
    assert result["exact_collateral_delta_join_transactions"] == 0
    assert result["historical_cash_reuse_evidence"] is False


def test_ten_dollar_path_records_first_negative_event_and_required_cash():
    rows = [activity("TRADE", f"b{i}", i, "5", "BUY") for i in range(1, 4)]
    rows += [activity("TRADE", f"s{i}", i + 3, "1", "SELL") for i in range(3)]
    transfers = []
    for row in rows:
        amount = 5_000_000 if row["side"] == "BUY" else 1_000_000
        transfers.append(transfer(row["transaction_ref"], amount,
                                   source="owner" if row["side"] == "BUY" else "exchange",
                                   target="exchange" if row["side"] == "BUY" else "owner"))
    result = reconcile_observed_cash(rows, transfers, owner="owner", collateral_token="cash")
    assert result["first_negative_event_index"] == 3
    assert result["required_initial_cash"] == "15"
    assert result["same_ledger_10_canary_eligible"] is False
