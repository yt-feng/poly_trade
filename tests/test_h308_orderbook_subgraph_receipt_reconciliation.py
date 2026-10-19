from analysis.h308_orderbook_subgraph_receipt_reconciliation import (
    EVENT_QUERY,
    capture_account,
    _event_to_activity,
    _known_btc_hashes,
    _subgraph_events,
)


OWNER = "0x" + "11" * 20
OTHER = "0x" + "22" * 20
TX = "0x" + "aa" * 32
TX2 = "0x" + "bb" * 32
CONDITION = "0x" + "cc" * 32
SLUG = "btc-updown-5m-2026-10-19-1200"


def source_row(tx=TX):
    return {
        "type": "TRADE",
        "side": "BUY",
        "transaction_hash": tx,
        "timestamp": 1_797_000_000,
        "size": "10",
        "price": "0.50",
        "slug": SLUG,
        "condition_id": CONDITION,
    }


def event(tx=TX, maker=OWNER):
    return {
        "id": tx + "order",
        "transactionHash": tx,
        "timestamp": "1797000001",
        "orderHash": "0x" + "dd" * 32,
        "maker": maker,
        "taker": "0x" + "00" * 20,
        "makerAssetId": "0",
        "takerAssetId": "123",
        "makerAmountFilled": "5000000",
        "takerAmountFilled": "10000000",
        "fee": "0",
    }


def test_query_is_transaction_bounded_and_requests_event_identity_fields():
    assert "transactionHash_in" in EVENT_QUERY
    for name in ("transactionHash", "timestamp", "orderHash", "maker", "makerAmountFilled"):
        assert name in EVENT_QUERY


def test_known_hashes_only_use_explicit_btc_activity():
    account = {"account_trades": [source_row(), source_row(TX2)]}
    assert set(_known_btc_hashes(account)) == {TX, TX2}
    assert _known_btc_hashes({"account_trades": [{**source_row(), "slug": "eth-updown-5m-x"}]}) == {}


def test_subgraph_chunks_hashes_and_preserves_failures():
    calls = []

    def fetcher(endpoint, query, variables):
        calls.append((endpoint, query, variables))
        return {"data": {"orderFilledEvents": [event(tx=variables["hashes"][0])]}}

    rows, queries, failures = _subgraph_events([TX, TX2], fetcher=fetcher, endpoint="https://example.test")
    assert queries == 1
    assert failures == []
    assert len(rows) == 1
    assert calls[0][2]["hashes"] == [TX, TX2]


def test_event_maps_to_owner_trade_with_source_market_identity():
    normalized = _event_to_activity(event(), source_row())
    assert normalized["transaction_ref"] == TX
    assert normalized["side"] == "BUY"
    assert normalized["slug"] == SLUG
    assert normalized["condition_id"] == CONDITION
    assert normalized["usdc_size"] == "5"


def test_capture_filters_other_makers_and_does_not_admit_missing_receipts():
    def fetcher(endpoint, query, variables):
        return {"data": {"orderFilledEvents": [event(), event(TX2, OTHER)]}}

    result = capture_account(
        {"owner": OWNER, "account_trades": [source_row(), source_row(TX2)]},
        fetcher=fetcher,
        rpc_call=lambda method, params: None,
        endpoint="https://example.test",
    )
    assert result["known_btc_transaction_hashes"] == 2
    assert result["subgraph_raw_event_rows"] == 2
    assert result["subgraph_owner_event_rows"] == 1
    assert result["normalized_btc_trade_rows"] == 1
    assert result["receipt_fetch_failures"] == 1
    assert result["cash_release_receipt_rows"] == 0
    assert result["verified_canary_fills"] == 0
    assert result["same_ledger_compounding_rows"] == 0
