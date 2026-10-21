import io
import sys
from urllib.error import HTTPError
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "analysis"))

from h300_owner_scoped_payout_transfer_capture import (  # noqa: E402
    PAYOUT_REDEMPTION_TOPIC,
    TRANSFER_TOPIC,
)
import h301_owner_scoped_settlement_history_scan as scan  # noqa: E402
from h301_owner_scoped_settlement_history_scan import capture_account  # noqa: E402


OWNER = "0x" + "11" * 20
EXCHANGE = "0x" + "22" * 20
ADAPTER = "0x" + "33" * 20
COLLATERAL = "0x2791bca1f2de4661ed88a30c99a7a9449aa84174"
CTF = "0x4d97dcd97ec945f40cf65f87097ace5ea0476045"
CONDITION = "0x" + "07" * 32


def word(value: int | str) -> str:
    if isinstance(value, str):
        return value[2:] if value.startswith("0x") else value
    return f"{value:064x}"


def topic_address(value: str) -> str:
    return "0x" + "0" * 24 + value[2:]


def transfer_log(source: str, target: str, amount: int) -> dict:
    return {
        "address": COLLATERAL,
        "topics": [TRANSFER_TOPIC, topic_address(source), topic_address(target)],
        "data": "0x" + word(amount),
    }


def payout_log(amount: int) -> dict:
    return {
        "address": CTF,
        "topics": [PAYOUT_REDEMPTION_TOPIC, topic_address(OWNER), topic_address(COLLATERAL), "0x" + word(0)],
        "data": "0x" + word(CONDITION) + word(96) + word(amount) + word(1) + word(3),
    }


def test_separate_redemption_transaction_is_joined_to_cash_ledger():
    tx_buy = "0x" + "01" * 32
    tx_redeem = "0x" + "02" * 32
    receipts = {
        tx_buy: {
            "transactionHash": tx_buy,
            "status": "0x1",
            "blockNumber": "0x64",
            "logs": [transfer_log(OWNER, EXCHANGE, 5_000_000)],
        },
        tx_redeem: {
            "transactionHash": tx_redeem,
            "status": "0x1",
            "blockNumber": "0x6e",
            "logs": [payout_log(5_500_000), transfer_log(ADAPTER, OWNER, 5_500_000)],
        },
    }

    def rpc(method, params):
        if method == "eth_getTransactionReceipt":
            return receipts.get(params[0])
        if method == "eth_blockNumber":
            return "0x70"
        if method == "eth_getBlockByNumber":
            return {"timestamp": "0x7d0"}
        if method == "eth_getLogs":
            topic0 = params[0]["topics"][0]
            if topic0 == PAYOUT_REDEMPTION_TOPIC:
                return [{**payout_log(5_500_000), "transactionHash": tx_redeem, "blockNumber": "0x6e"}]
            return [{**transfer_log(ADAPTER, OWNER, 5_500_000), "transactionHash": tx_redeem, "blockNumber": "0x6e"}]
        raise AssertionError(method)

    report = capture_account(
        {
            "label": "fixture",
            "owner": OWNER,
            "btc_transaction_hashes": [tx_buy],
            "activity": [{
                "type": "TRADE", "side": "BUY", "transactionHash": tx_buy,
                "timestamp": 1000, "usdcSize": "5", "slug": "btc-updown-5m-123",
                "conditionId": CONDITION,
            }],
        },
        rpc_call=rpc,
        max_history_blocks=20,
        chunk_blocks=10,
    )
    assert report["settlement_receipt_rows"] == 1
    assert report["cash_release_receipt_rows"] == 1
    reconciliation = report["cash_reconciliation_by_collateral_token"][COLLATERAL]
    assert reconciliation["payout_redeem_transaction_matches"] == 1
    assert reconciliation["historical_cash_reuse_evidence"] is True
    assert reconciliation["ending_cash"] == "10.5"
    assert report["verified_canary_fills"] == 0
    assert report["same_ledger_compounding_rows"] == 0


def test_unbound_payout_is_not_admitted_without_btc_condition_id():
    tx_buy = "0x" + "03" * 32
    tx_redeem = "0x" + "04" * 32
    receipts = {
        tx_buy: {"transactionHash": tx_buy, "status": "0x1", "blockNumber": "0x64", "logs": []},
        tx_redeem: {"transactionHash": tx_redeem, "status": "0x1", "blockNumber": "0x6e",
                    "logs": [payout_log(5_500_000), transfer_log(ADAPTER, OWNER, 5_500_000)]},
    }

    def rpc(method, params):
        if method == "eth_getTransactionReceipt":
            return receipts.get(params[0])
        if method == "eth_blockNumber":
            return "0x70"
        if method == "eth_getBlockByNumber":
            return {"timestamp": "0x7d0"}
        if method == "eth_getLogs":
            topic0 = params[0]["topics"][0]
            log = payout_log(5_500_000) if topic0 == PAYOUT_REDEMPTION_TOPIC else transfer_log(ADAPTER, OWNER, 5_500_000)
            return [{**log, "transactionHash": tx_redeem, "blockNumber": "0x6e"}]
        raise AssertionError(method)

    report = capture_account(
        {
            "owner": OWNER,
            "btc_transaction_hashes": [tx_buy],
            "activity": [{"type": "TRADE", "side": "BUY", "transactionHash": tx_buy,
                           "timestamp": 1000, "usdcSize": "5", "slug": "btc-updown-5m-123"}],
        },
        rpc_call=rpc,
        max_history_blocks=20,
        chunk_blocks=10,
    )
    assert report["condition_ids_available"] == 0
    assert report["settlement_receipt_rows"] == 0
    assert report["cash_release_receipt_rows"] == 0
    assert report["verified_canary_fills"] == 0


def test_rpc_retries_transient_http_failure_then_returns_result(monkeypatch):
    attempts = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self):
            return b'{"jsonrpc":"2.0","id":1,"result":{"ok":true}}'

    def flaky(request, timeout):
        attempts.append((request.full_url, timeout))
        if len(attempts) == 1:
            raise HTTPError(request.full_url, 503, "temporary", {}, io.BytesIO())
        return Response()

    monkeypatch.setattr(scan, "urlopen", flaky)
    monkeypatch.setattr(scan.time, "sleep", lambda _seconds: None)
    assert scan._rpc_call("https://rpc.example", "eth_blockNumber", []) == {"ok": True}
    assert len(attempts) == 2


def test_rpc_does_not_retry_non_transient_http_failure(monkeypatch):
    attempts = []

    def rejected(request, timeout):
        attempts.append(request.full_url)
        raise HTTPError(request.full_url, 403, "forbidden", {}, io.BytesIO())

    monkeypatch.setattr(scan, "urlopen", rejected)
    monkeypatch.setattr(scan.time, "sleep", lambda _seconds: None)
    try:
        scan._rpc_call("https://rpc.example", "eth_blockNumber", [])
    except RuntimeError as exc:
        assert str(exc) == "history_rpc_error:http_403"
    else:
        raise AssertionError("expected bounded provider error")
    assert len(attempts) == 1

