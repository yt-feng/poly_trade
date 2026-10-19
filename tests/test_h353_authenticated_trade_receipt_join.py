import sys
from urllib.error import URLError
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "analysis"))

from h353_authenticated_trade_receipt_join import capture  # noqa: E402


OWNER = "0x" + "11" * 20
TX = "0x" + "01" * 32


def activity(*, slug="btc-updown-5m-123", tx=TX, kind="TRADE"):
    return {
        "type": kind,
        "side": "BUY" if kind == "TRADE" else "",
        "transactionHash": tx,
        "timestamp": 1_729_000_000,
        "size": "5",
        "price": "0.50",
        "slug": slug,
        "conditionId": "0x" + "07" * 32,
    }


def test_direct_join_fetches_activity_hash_without_history_seed():
    calls = []

    def receipt_fetcher(tx):
        calls.append(tx)
        return {"transactionHash": tx, "status": "0x1", "blockNumber": "0x64", "logs": []}

    result = capture(
        [{"label": "btc5m", "owner": OWNER, "activity": [activity()]}],
        receipt_fetcher=receipt_fetcher,
    )
    assert calls == [TX]
    assert result["transaction_hashes_considered"] == 1
    assert result["confirmed_receipts"] == 1
    assert result["legacy_settlement_receipt_rows"] == 0
    assert result["v2_settlement_receipt_rows"] == 0
    assert result["cash_release_receipt_rows"] == 0
    assert result["verified_canary_fills"] == 0


def test_non_btc_activity_is_excluded_before_receipt_fetch():
    def receipt_fetcher(_tx):
        raise AssertionError("non-BTC activity must not be fetched")

    result = capture(
        [{"label": "btc5m", "owner": OWNER, "activity": [activity(slug="eth-updown-5m-123")]}],
        receipt_fetcher=receipt_fetcher,
    )
    assert result["transaction_hashes_considered"] == 0
    assert result["confirmed_receipts"] == 0


def test_receipt_failure_is_counted_without_creating_cash_or_canary_evidence():
    def receipt_fetcher(_tx):
        raise RuntimeError("provider")

    result = capture(
        [{"label": "btc5m", "owner": OWNER, "activity": [activity()]}],
        receipt_fetcher=receipt_fetcher,
    )
    account = result["accounts"][0]
    assert account["receipt_fetch_failures"] == 1
    assert account["receipt_failure_categories"] == {"unknown_receipt_error": 1}
    assert result["confirmed_receipts"] == 0
    assert result["cash_release_receipt_rows"] == 0
    assert result["same_ledger_compounding_rows"] == 0


def test_transport_failure_is_categorized_without_exposing_error_text():
    def receipt_fetcher(_tx):
        raise URLError("provider detail must not be retained")

    result = capture(
        [{"label": "btc5m", "owner": OWNER, "activity": [activity()]}],
        receipt_fetcher=receipt_fetcher,
    )
    assert result["receipt_failure_categories"] == {"transport_error": 1}
