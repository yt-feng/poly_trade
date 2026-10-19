import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "analysis"))

from h304_data_api_v2_receipt_join import capture_account  # noqa: E402


OWNER = "0x" + "11" * 20
CONDITION = "0x" + "07" * 32
TX_BUY = "0x" + "01" * 32


def trade(tx=TX_BUY):
    return {
        "type": "TRADE", "side": "BUY", "transactionHash": tx,
        "timestamp": 1_729_000_000, "usdcSize": "5",
        "slug": "btc-updown-5m-123", "conditionId": CONDITION,
    }


def test_status_activity_are_joined_once_and_only_receipt_evidence_can_close_cash():
    calls = []

    def fetcher(_base, path, params):
        calls.append((path, dict(params)))
        if path == "/v2/positions":
            return {"data": [{"slug": "btc-updown-5m-123", "condition_id": CONDITION}], "pagination": {}}
        return {"data": [trade()], "pagination": {}}

    def rpc(method, params):
        if method == "eth_getTransactionReceipt":
            return {"transactionHash": params[0], "status": "0x1", "blockNumber": "0x64", "logs": []}
        if method == "eth_blockNumber":
            return "0x70"
        if method == "eth_getLogs":
            return []
        if method == "eth_getBlockByNumber":
            return {"timestamp": "0x7d0"}
        raise AssertionError(method)

    report = capture_account(
        {"owner": OWNER, "existing_activity": [trade()]},
        data_fetcher=fetcher, rpc_call=rpc, base_url="https://example.invalid",
        max_history_blocks=20, chunk_blocks=10,
    )

    assert len(calls) == 3
    assert report["redeemable_btc_rows"] == 1
    assert report["closed_btc_rows"] == 1
    assert report["activity_btc_rows"] == 1
    assert report["joined_btc_activity_rows"] == 1
    assert report["v2_receipt_join"]["btc_five_minute_activity_rows"] == 1
    assert report["v2_receipt_join"]["settlement_receipt_rows"] == 0
    assert report["v2_receipt_join"]["cash_release_receipt_rows"] == 0
    assert report["verified_canary_fills"] == 0


def test_data_api_failure_stays_source_failure_and_does_not_admit_cash():
    def fetcher(_base, _path, _params):
        raise TimeoutError("fixture")

    def rpc(_method, _params):
        raise AssertionError("RPC must not run without status/activity rows")

    report = capture_account(
        {"owner": OWNER, "existing_activity": []},
        data_fetcher=fetcher, rpc_call=rpc, base_url="https://example.invalid",
    )

    assert report["joined_btc_activity_rows"] == 0
    assert report["v2_receipt_join"]["settlement_receipt_rows"] == 0
    assert report["v2_receipt_join"]["cash_release_receipt_rows"] == 0

