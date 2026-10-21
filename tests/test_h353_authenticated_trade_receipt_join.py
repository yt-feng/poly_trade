import sys
from urllib.error import URLError
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "analysis"))

from h353_authenticated_trade_receipt_join import capture  # noqa: E402
from h300_owner_scoped_payout_transfer_capture import PAYOUT_REDEMPTION_TOPIC, TRANSFER_TOPIC  # noqa: E402


WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "h353-authenticated-trade-receipt-join.yml"
OWNER = "0x" + "11" * 20
TX = "0x" + "01" * 32
COLLATERAL = "0x2791bca1f2de4661ed88a30c99a7a9449aa84174"
CTF = "0x4d97dcd97ec945f40cf65f87097ace5ea0476045"
ADAPTER = "0x" + "22" * 20
ZERO = "0x" + "00" * 20


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


def test_h353_defaults_to_the_independent_read_only_receipt_endpoint():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "default: https://polygon.drpc.org" in text


def _word(value: int) -> str:
    return f"{value:064x}"


def _topic_address(value: str) -> str:
    return "0x" + "0" * 24 + value[2:]


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


def test_shared_history_rpc_errors_are_classified_as_rpc_failures():
    def receipt_fetcher(_tx):
        raise RuntimeError("history_rpc_error:transport")

    result = capture(
        [{"label": "btc5m", "owner": OWNER, "activity": [activity()]}],
        receipt_fetcher=receipt_fetcher,
    )
    assert result["receipt_failure_categories"] == {"rpc_error": 1}


def test_missing_receipt_is_distinguished_from_provider_failure():
    def receipt_fetcher(_tx):
        return None

    result = capture(
        [{"label": "btc5m", "owner": OWNER, "activity": [activity()]}],
        receipt_fetcher=receipt_fetcher,
    )
    assert result["receipt_failure_categories"] == {"null_receipt": 1}


def test_transport_failure_is_categorized_without_exposing_error_text():
    def receipt_fetcher(_tx):
        raise URLError("provider detail must not be retained")

    result = capture(
        [{"label": "btc5m", "owner": OWNER, "activity": [activity()]}],
        receipt_fetcher=receipt_fetcher,
    )
    assert result["receipt_failure_categories"] == {"transport_error": 1}


def test_legacy_payout_is_filtered_to_btc_condition_set():
    tx = "0x" + "02" * 32
    btc_condition = int("0x" + "07" * 32, 16)
    non_btc_condition = int("0x" + "08" * 32, 16)
    receipt = {
        "transactionHash": tx,
        "status": "0x1",
        "logs": [
            {
                "address": CTF,
                "topics": [PAYOUT_REDEMPTION_TOPIC, _topic_address(OWNER), _topic_address(COLLATERAL), "0x" + _word(0)],
                "data": "0x" + _word(non_btc_condition) + _word(96) + _word(5_000_000) + _word(1) + _word(1),
            },
            {
                "address": COLLATERAL,
                "topics": [TRANSFER_TOPIC, _topic_address(ADAPTER), _topic_address(OWNER)],
                "data": "0x" + _word(5_000_000),
            },
        ],
    }
    result = capture(
        [{"owner": OWNER, "activity": [activity(tx=tx)]}],
        receipt_fetcher=lambda _tx: receipt,
    )
    assert result["legacy_settlement_receipt_rows"] == 0
    assert result["cash_release_receipt_rows"] == 0
