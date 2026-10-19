import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "analysis"))

from h306_data_api_v2_trades_receipt_reconciliation import capture_account  # noqa: E402


OWNER = "0x" + "11" * 20
CONDITION = "0x" + "07" * 32
TX_BUY = "0x" + "01" * 32


def trade(slug="btc-updown-5m-123", tx=TX_BUY):
    return {
        "side": "BUY", "transaction_hash": tx, "timestamp": 1_729_000_000,
        "size": "5", "price": "0.50", "slug": slug,
        "condition_id": CONDITION,
    }


def test_maker_taker_trades_feed_is_directly_joined_to_receipts():
    calls = []

    def fetcher(_base, path, params):
        calls.append((path, dict(params)))
        assert path == "/v2/trades"
        return {"data": [trade(), trade(slug="eth-updown-5m-123", tx="0x" + "02" * 32)], "pagination": {}}

    def rpc(method, params):
        assert method == "eth_getTransactionReceipt"
        return {"transactionHash": params[0], "status": "0x1", "blockNumber": "0x64", "logs": []}

    report = capture_account(
        {"owner": OWNER, "existing_activity": [trade()]},
        data_fetcher=fetcher, rpc_call=rpc, base_url="https://example.invalid",
    )

    assert calls[0][1]["taker_only"] is False
    assert calls[0][1]["start"] == 1
    assert report["raw_btc_trade_rows"] == 1
    assert report["normalized_btc_trade_rows"] == 1
    assert report["distinct_btc_trade_transactions"] == 1
    assert report["existing_activity_transaction_overlap"] == 1
    assert report["confirmed_trade_receipts"] == 1
    assert report["legacy_settlement_receipt_rows"] == 0
    assert report["v2_settlement_receipt_rows"] == 0
    assert report["cash_release_receipt_rows"] == 0
    assert report["verified_canary_fills"] == 0


def test_trades_feed_failure_does_not_create_execution_or_cash_evidence():
    def fetcher(_base, _path, _params):
        raise TimeoutError("fixture")

    def rpc(_method, _params):
        raise AssertionError("no receipt request without trade rows")

    report = capture_account({"owner": OWNER}, data_fetcher=fetcher, rpc_call=rpc, base_url="https://example.invalid")

    assert report["raw_btc_trade_rows"] == 0
    assert report["normalized_btc_trade_rows"] == 0
    assert report["failure"] == "TimeoutError"
    assert report["confirmed_trade_receipts"] == 0
    assert report["cash_release_receipt_rows"] == 0

