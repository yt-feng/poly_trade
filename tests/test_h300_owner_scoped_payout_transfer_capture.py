import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "analysis"))

from h300_owner_scoped_payout_transfer_capture import (  # noqa: E402
    PAYOUT_REDEMPTION_TOPIC,
    TRANSFER_TOPIC,
    capture,
    capture_account,
)


OWNER = "0x" + "11" * 20
EXCHANGE = "0x" + "22" * 20
ADAPTER = "0x" + "33" * 20
COLLATERAL = "0x2791bca1f2de4661ed88a30c99a7a9449aa84174"
CTF = "0x4d97dcd97ec945f40cf65f87097ace5ea0476045"


def word(value: int) -> str:
    return f"{value:064x}"


def topic_address(value: str) -> str:
    return "0x" + ("0" * 24) + value[2:]


def transfer_log(source: str, target: str, amount: int) -> dict:
    return {
        "address": COLLATERAL,
        "topics": [TRANSFER_TOPIC, topic_address(source), topic_address(target)],
        "data": "0x" + word(amount),
    }


def payout_log(amount: int) -> dict:
    # PayoutRedemption(redeemer, collateral, parentCollectionId,
    #                  conditionId, indexSets, payout)
    data = word(7) + word(96) + word(amount) + word(1) + word(3)
    return {
        "address": CTF,
        "topics": [PAYOUT_REDEMPTION_TOPIC, topic_address(OWNER), topic_address(COLLATERAL), "0x" + word(0)],
        "data": "0x" + data,
    }


def test_existing_btc_activity_decodes_payout_and_cash_release():
    tx_buy = "0x" + "01" * 32
    tx_redeem = "0x" + "02" * 32
    receipts = {
        tx_buy: {
            "transactionHash": tx_buy,
            "status": "0x1",
            "logs": [transfer_log(OWNER, EXCHANGE, 5_000_000)],
        },
        tx_redeem: {
            "transactionHash": tx_redeem,
            "status": "0x1",
            "logs": [payout_log(5_500_000), transfer_log(ADAPTER, OWNER, 5_500_000)],
        },
    }
    account = {
        "label": "fixture",
        "owner": OWNER,
        "activity": [
            {"type": "TRADE", "side": "BUY", "transactionHash": tx_buy, "timestamp": 1,
             "usdcSize": "5", "slug": "btc-updown-5m-123"},
            {"type": "REDEEM", "transactionHash": tx_redeem, "timestamp": 2,
             "usdcSize": "5.5", "slug": "btc-updown-5m-123"},
            {"type": "TRADE", "side": "BUY", "transactionHash": tx_buy, "timestamp": 3,
             "usdcSize": "5", "slug": "eth-updown-5m-123"},
        ],
    }
    report = capture_account(account, receipt_fetcher=receipts.__getitem__)
    assert report["btc_five_minute_activity_rows"] == 2
    assert report["transaction_hashes_considered"] == 2
    assert report["collateral_transfer_rows"] == 2
    assert report["payout_redemption_rows"] == 1
    reconciliation = report["cash_reconciliation_by_collateral_token"][COLLATERAL]
    assert reconciliation["exact_collateral_delta_join_transactions"] == 2
    assert reconciliation["cash_release_transaction_rows"] == 1
    assert reconciliation["historical_cash_reuse_evidence"] is True
    assert report["verified_canary_fills"] == 0
    assert report["same_ledger_compounding_rows"] == 0


def test_reverted_receipt_cannot_supply_cash_evidence():
    tx = "0x" + "03" * 32
    account = {
        "owner": OWNER,
        "activity": [{"type": "REDEEM", "transactionHash": tx, "timestamp": 1,
                       "usdcSize": "5.5", "slug": "btc-updown-5m-123"}],
    }
    report = capture_account(
        account,
        receipt_fetcher=lambda _tx: {
            "transactionHash": tx,
            "status": "0x0",
            "logs": [payout_log(5_500_000), transfer_log(ADAPTER, OWNER, 5_500_000)],
        },
    )
    assert report["confirmed_receipts"] == 0
    assert report["collateral_transfer_rows"] == 0
    assert report["payout_redemption_rows"] == 0
    assert report["cash_reconciliation_by_collateral_token"] == {}


def test_capture_keeps_ten_fill_compounding_gate_closed():
    tx = "0x" + "04" * 32
    result = capture(
        [{"owner": OWNER, "activity": [{"type": "TRADE", "side": "BUY", "transactionHash": tx,
                                            "timestamp": 1, "usdcSize": "5", "slug": "btc-updown-5m-123"}]}],
        receipt_fetcher=lambda _tx: {
            "transactionHash": tx,
            "status": "0x1",
            "logs": [transfer_log(OWNER, EXCHANGE, 5_000_000)],
        },
    )
    assert result["settlement_receipt_rows"] == 0
    assert result["cash_release_receipt_rows"] == 0
    assert result["verified_canary_fills"] == 0
    assert result["same_ledger_compounding_rows"] == 0

