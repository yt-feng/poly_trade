import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "analysis"))

from h303_data_api_v2_status_reconciliation import (  # noqa: E402
    capture_account,
)


OWNER = "0x" + "11" * 20
CONDITION = "0x" + "07" * 32
TX_BUY = "0x" + "01" * 32
TX_REDEEM = "0x" + "02" * 32


def position(slug: str, *, condition: str = CONDITION, realized: str = "0") -> dict:
    return {
        "slug": slug,
        "condition_id": condition,
        "current_value": "5.50",
        "realized_pnl": realized,
        "last_event_at": "2026-10-19T00:00:00Z",
        "redeemable": True,
    }


def activity(kind: str, slug: str, tx: str) -> dict:
    return {
        "type": kind,
        "side": "BUY" if kind == "TRADE" else "",
        "transactionHash": tx,
        "timestamp": 1_729_000_000,
        "usdcSize": "5",
        "slug": slug,
        "conditionId": CONDITION,
    }


def test_v2_status_pages_filter_btc_and_reconcile_existing_transactions():
    calls = []
    btc_slug = "btc-updown-5m-123"
    other_slug = "eth-updown-5m-123"

    def fetcher(_base, path, params):
        calls.append((path, dict(params)))
        status = params.get("status")
        cursor = params.get("cursor")
        if path == "/v2/positions" and status == "REDEEMABLE":
            if cursor == "next":
                return {"data": [position(btc_slug, realized="0.50")], "pagination": {}}
            return {"data": [position(other_slug)], "pagination": {"has_more": True, "next_cursor": "next"}}
        if path == "/v2/positions" and status == "CLOSED":
            return {"data": [position(btc_slug, realized="0.50")], "pagination": {}}
        if path == "/v2/activity":
            return {"data": [activity("REDEEM", btc_slug, TX_REDEEM), activity("TRADE", other_slug, TX_BUY)], "pagination": {}}
        raise AssertionError((path, params))

    report = capture_account(
        {"owner": OWNER, "existing_activity": [{"transactionHash": TX_REDEEM}]},
        fetcher=fetcher,
        base_url="https://example.invalid",
    )

    assert report["redeemable_btc_rows"] == 1
    assert report["closed_btc_rows"] == 1
    assert report["activity_btc_redeem_rows"] == 1
    assert report["activity"]["btc_existing_transaction_overlap"] == 1
    assert report["redeemable_pages"] == 2
    assert report["closed_pages"] == 1
    assert report["activity_pages"] == 1
    assert report["cash_receipt_rows"] == 0
    assert report["balance_continuity_proven"] is False
    assert report["same_ledger_compounding_rows"] == 0
    assert any(path == "/v2/positions" and params.get("status") == "REDEEMABLE" and params.get("cursor") == "next"
               for path, params in calls)


def test_api_failure_is_bounded_and_does_not_create_settlement_evidence():
    attempts = []

    def fetcher(_base, path, _params):
        attempts.append(path)
        raise TimeoutError("fixture")

    report = capture_account({"owner": OWNER}, fetcher=fetcher, base_url="https://example.invalid")

    assert attempts == ["/v2/positions", "/v2/positions", "/v2/activity"]
    assert report["redeemable_failure"] == "TimeoutError"
    assert report["closed_failure"] == "TimeoutError"
    assert report["activity_failure"] == "TimeoutError"
    assert report["cash_receipt_rows"] == 0
    assert report["verified_canary_fills"] == 0

