from analysis.h308_orderbook_subgraph_receipt_reconciliation import (
    EVENT_QUERY,
    V1_ORDER_FILLED_TOPIC,
    capture_account,
    _event_to_activity,
    _known_btc_hashes,
    _subgraph_events,
)
from analysis.h300_owner_scoped_payout_transfer_capture import (
    PAYOUT_REDEMPTION_TOPIC,
    TRANSFER_TOPIC,
)


OWNER = "0x" + "11" * 20
OTHER = "0x" + "22" * 20
TX = "0x" + "aa" * 32
TX2 = "0x" + "bb" * 32
TX_REDEEM = "0x" + "cc" * 32
CONDITION = "0x" + "cc" * 32
SLUG = "btc-updown-5m-2026-10-19-1200"
ADAPTER = "0x" + "33" * 20
PUSD = "0xc011a7e12a19f7b1f670d46f03b03f3342e82dfb"
CTF = "0x4d97dcd97ec945f40cf65f87097ace5ea0476045"
ZERO = "0x" + "00" * 20


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


def word(value):
    return f"{value:064x}"


def receipt(tx=TX, maker=OWNER):
    return {
        "status": "0x1",
        "transactionHash": tx,
        "blockNumber": "0x10",
        "logs": [{
            "address": "0x4bfb41d5b3570defd03c39a9a4d8de6bd8b8982e",
            "topics": [
                V1_ORDER_FILLED_TOPIC,
                "0x" + "dd" * 32,
                "0x" + "0" * 24 + maker[2:],
                "0x" + "0" * 24 + OTHER[2:],
            ],
            "data": "0x" + "".join(word(value) for value in (0, 123, 5_000_000, 10_000_000, 0)),
            "transactionHash": tx,
            "blockNumber": "0x10",
            "logIndex": "0x1",
        }],
    }


def topic_address(value):
    return "0x" + "0" * 24 + value[2:]


def redemption_receipt():
    payout = 5_500_000
    condition = int(CONDITION, 16)
    return {
        "status": "0x1",
        "transactionHash": TX_REDEEM,
        "blockNumber": "0x10",
        "logs": [
            {
                "address": CTF,
                "topics": [PAYOUT_REDEMPTION_TOPIC, topic_address(ADAPTER), topic_address("0x2791bca1f2de4661ed88a30c99a7a9449aa84174"), "0x" + word(0)],
                "data": "0x" + "".join(word(value) for value in (condition, 96, payout, 2, 1, 2)),
            },
            {
                "address": PUSD,
                "topics": [TRANSFER_TOPIC, topic_address(ZERO), topic_address(OWNER)],
                "data": "0x" + word(payout),
            },
        ],
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
        source_mode="subgraph",
    )
    assert result["known_btc_transaction_hashes"] == 2
    assert result["subgraph_raw_event_rows"] == 2
    assert result["subgraph_owner_event_rows"] == 1
    assert result["normalized_btc_trade_rows"] == 1
    assert result["receipt_fetch_failures"] == 1
    assert result["cash_release_receipt_rows"] == 0
    assert result["verified_canary_fills"] == 0
    assert result["same_ledger_compounding_rows"] == 0


def test_receipt_mode_decodes_owner_v1_orderfilled_and_uses_block_timestamp():
    calls = []

    def rpc(method, params):
        calls.append((method, params))
        if method == "eth_getTransactionReceipt":
            return receipt()
        if method == "eth_getBlockByNumber":
            return {"timestamp": "0x100"}
        raise AssertionError(method)

    result = capture_account(
        {"owner": OWNER, "account_trades": [source_row()]},
        fetcher=lambda *args: {"data": {"orderFilledEvents": []}},
        rpc_call=rpc,
        endpoint="https://deprecated.example",
    )
    assert result["source_mode"] == "receipt"
    assert result["receipt_fill_raw_event_rows"] == 1
    assert result["receipt_fill_owner_event_rows"] == 1
    assert result["normalized_btc_trade_rows"] == 1
    assert result["private_normalized_btc_trades"][0]["side"] == "BUY"
    assert result["private_normalized_btc_trades"][0]["usdc_size"] == "5"
    assert result["subgraph_queries"] == 0
    assert result["cash_release_receipt_rows"] == 0
    assert any(method == "eth_getBlockByNumber" for method, _ in calls)


def test_receipt_mode_rejects_other_maker_and_does_not_count_as_canary():
    result = capture_account(
        {"owner": OWNER, "account_trades": [source_row()]},
        fetcher=lambda *args: {"data": {"orderFilledEvents": []}},
        rpc_call=lambda method, params: receipt(maker=OTHER) if method == "eth_getTransactionReceipt" else {"timestamp": "0x100"},
    )
    assert result["receipt_fill_raw_event_rows"] == 1
    assert result["receipt_fill_owner_event_rows"] == 0
    assert result["normalized_btc_trade_rows"] == 0
    assert result["verified_canary_fills"] == 0
    assert result["same_ledger_compounding_rows"] == 0


def test_receipt_mode_scans_existing_redeem_hash_for_partial_settlement():
    def rpc(method, params):
        if method == "eth_getTransactionReceipt":
            if params[0] == TX_REDEEM:
                return redemption_receipt()
            return receipt()
        if method == "eth_getBlockByNumber":
            return {"timestamp": "0x100"}
        raise AssertionError(method)

    redeem = {
        "type": "REDEEM",
        "transactionHash": TX_REDEEM,
        "timestamp": 1_797_000_010,
        "usdcSize": "5.5",
        "slug": SLUG,
        "conditionId": CONDITION,
    }
    result = capture_account(
        {"owner": OWNER, "account_trades": [source_row()], "public_data_activity": [redeem]},
        fetcher=lambda *args: {"data": {"orderFilledEvents": []}},
        rpc_call=rpc,
    )
    assert result["settlement_candidate_hashes"] == 1
    assert result["legacy_settlement_receipt_rows"] == 1
    assert result["receipt_fetch_failures"] == 0
    assert result["verified_canary_fills"] == 0
    assert result["same_ledger_compounding_rows"] == 0
