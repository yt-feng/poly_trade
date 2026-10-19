import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "analysis"))

from h307_condition_scoped_trades_receipt_reconciliation import capture_account  # noqa: E402


OWNER = "0x" + "11" * 20
OTHER = "0x" + "22" * 20
CONDITION = "0x" + "07" * 32
TX = "0x" + "01" * 32


def position(status_slug="btc-updown-5m-123"):
    return {"slug": status_slug, "condition_id": CONDITION, "current_value": "5", "last_event_at": "2026-10-19T00:00:00Z"}


def trade(proxy=OWNER, slug="btc-updown-5m-123", tx=TX):
    return {"proxy_wallet": proxy, "slug": slug, "condition_id": CONDITION,
            "transaction_hash": tx, "side": "BUY", "timestamp": 1_729_000_000,
            "size": "5", "price": "0.50"}


def test_condition_scoped_trades_filter_owner_and_join_direct_receipt():
    calls = []

    def fetcher(_base, path, params):
        calls.append((path, dict(params)))
        if path == "/v2/positions":
            return {"data": [position()], "pagination": {}}
        assert path == "/v2/trades"
        assert params["condition"] == CONDITION
        return {"data": [trade(), trade(proxy=OTHER), trade(slug="eth-updown-5m-123", tx="0x" + "02" * 32)], "pagination": {}}

    def rpc(method, params):
        assert method == "eth_getTransactionReceipt"
        return {"transactionHash": params[0], "status": "0x1", "blockNumber": "0x64", "logs": []}

    report = capture_account({"owner": OWNER}, data_fetcher=fetcher, rpc_call=rpc, base_url="https://example.invalid")

    assert sum(path == "/v2/positions" for path, _ in calls) == 2
    assert sum(path == "/v2/trades" for path, _ in calls) == 1
    assert report["condition_ids"] == 1
    assert report["condition_chunks"] == 1
    assert report["owner_trade_rows"] == 2
    assert report["btc_trade_rows"] == 1
    assert report["normalized_btc_trade_rows"] == 1
    assert report["confirmed_trade_receipts"] == 1
    assert report["cash_release_receipt_rows"] == 0
    assert report["verified_canary_fills"] == 0


def test_no_btc_condition_ids_means_no_trade_or_receipt_requests():
    def fetcher(_base, path, _params):
        assert path == "/v2/positions"
        return {"data": [{"slug": "eth-updown-5m-123", "condition_id": CONDITION}], "pagination": {}}

    def rpc(_method, _params):
        raise AssertionError("no receipt without a condition-scoped BTC trade")

    report = capture_account({"owner": OWNER}, data_fetcher=fetcher, rpc_call=rpc, base_url="https://example.invalid")

    assert report["condition_ids"] == 0
    assert report["condition_chunks"] == 0
    assert report["btc_trade_rows"] == 0
    assert report["cash_release_receipt_rows"] == 0

