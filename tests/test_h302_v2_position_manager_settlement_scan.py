import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "analysis"))

from h300_owner_scoped_payout_transfer_capture import TRANSFER_TOPIC  # noqa: E402
from h302_v2_position_manager_settlement_scan import (  # noqa: E402
    POSITION_REDEEMED_TOPIC,
    POSITIONS_MERGED_TOPIC,
    V2_COLLATERAL_TOKEN,
    V2_MODULES,
    _adaptive_get_logs,
    capture_account,
    _seed_window_ranges,
)


OWNER = "0x" + "11" * 20
EXCHANGE = "0x" + "22" * 20
CONDITION = "0x" + "07" * 31 + "00"
MODULE = sorted(V2_MODULES)[0]


def test_seed_window_ranges_merge_and_respect_causal_bound():
    assert _seed_window_ranges([100, 105, 1000], latest_block=2000, before_blocks=0, after_blocks=20, max_total_blocks=100) == [(100, 125), (1000, 1020)]
    try:
        _seed_window_ranges([100, 1000], latest_block=2000, before_blocks=0, after_blocks=100, max_total_blocks=50)
    except ValueError as exc:
        assert str(exc) == "seed_windows_exceed_history_bound"
    else:
        raise AssertionError("seed window total must remain bounded")


def word(value: int | str) -> str:
    if isinstance(value, str):
        return value[2:] if value.startswith("0x") else value
    return f"{value:064x}"


def topic_address(value: str) -> str:
    return "0x" + "0" * 24 + value[2:]


def transfer_log(source: str, target: str, amount: int) -> dict:
    return {
        "address": V2_COLLATERAL_TOKEN,
        "topics": [TRANSFER_TOPIC, topic_address(source), topic_address(target)],
        "data": "0x" + word(amount),
    }


def redeemed_log(amount: int, payout: int) -> dict:
    position_id = int(CONDITION, 16) | 1
    return {
        "address": MODULE,
        "topics": [POSITION_REDEEMED_TOPIC, topic_address(MODULE), "0x" + word(position_id), topic_address(OWNER)],
        "data": "0x" + word(amount) + word(payout),
    }


def merged_log(amount: int) -> dict:
    return {
        "address": MODULE,
        "topics": [POSITIONS_MERGED_TOPIC, topic_address(MODULE), "0x" + word(CONDITION), topic_address(OWNER)],
        "data": "0x" + word(amount),
    }


def test_v2_position_redeemed_and_collateral_transfer_join_cash():
    tx_buy = "0x" + "01" * 32
    tx_redeem = "0x" + "02" * 32
    receipts = {
        tx_buy: {"transactionHash": tx_buy, "status": "0x1", "blockNumber": "0x64",
                 "logs": [transfer_log(OWNER, EXCHANGE, 5_000_000)]},
        tx_redeem: {"transactionHash": tx_redeem, "status": "0x1", "blockNumber": "0x6e",
                    "logs": [redeemed_log(5_000_000, 5_500_000), transfer_log(MODULE, OWNER, 5_500_000)]},
    }

    event_kind = "redeemed"

    def rpc(method, params):
        if method == "eth_getTransactionReceipt":
            return receipts.get(params[0])
        if method == "eth_blockNumber":
            return "0x70"
        if method == "eth_getBlockByNumber":
            return {"timestamp": "0x7d0"}
        if method == "eth_getLogs":
            topic0 = params[0]["topics"][0]
            if isinstance(topic0, list):
                topic0 = POSITION_REDEEMED_TOPIC if event_kind == "redeemed" else topic0[0]
            if topic0 == POSITION_REDEEMED_TOPIC:
                log = redeemed_log(5_000_000, 5_500_000)
            elif topic0 == POSITIONS_MERGED_TOPIC:
                log = merged_log(5_500_000)
            else:
                log = transfer_log(MODULE, OWNER, 5_500_000)
            return [{**log, "transactionHash": tx_redeem, "blockNumber": "0x6e"}]
        raise AssertionError(method)

    report = capture_account(
        {"owner": OWNER, "btc_transaction_hashes": [tx_buy], "activity": [{
            "type": "TRADE", "side": "BUY", "transactionHash": tx_buy,
            "timestamp": 1000, "usdcSize": "5", "slug": "btc-updown-5m-123",
            "conditionId": CONDITION,
        }]},
        rpc_call=rpc, max_history_blocks=20, chunk_blocks=10,
    )
    assert report["settlement_receipt_rows"] == 1
    assert report["cash_release_receipt_rows"] == 1
    reconciliation = report["cash_reconciliation_by_collateral_token"][V2_COLLATERAL_TOKEN]
    assert reconciliation["payout_redeem_transaction_matches"] == 1
    assert reconciliation["historical_cash_reuse_evidence"] is True
    assert reconciliation["ending_cash"] == "10.5"
    assert report["verified_canary_fills"] == 0
    assert report["same_ledger_compounding_rows"] == 0


def test_v2_merge_event_is_condition_bound_and_counted_as_cash_release():
    tx_buy = "0x" + "03" * 32
    tx_merge = "0x" + "04" * 32
    receipts = {
        tx_buy: {"transactionHash": tx_buy, "status": "0x1", "blockNumber": "0x64",
                 "logs": [transfer_log(OWNER, EXCHANGE, 5_000_000)]},
        tx_merge: {"transactionHash": tx_merge, "status": "0x1", "blockNumber": "0x6e",
                   "logs": [merged_log(5_500_000), transfer_log(MODULE, OWNER, 5_500_000)]},
    }

    event_kind = "merged"

    def rpc(method, params):
        if method == "eth_getTransactionReceipt":
            return receipts.get(params[0])
        if method == "eth_blockNumber":
            return "0x70"
        if method == "eth_getBlockByNumber":
            return {"timestamp": "0x7d0"}
        if method == "eth_getLogs":
            topic0 = params[0]["topics"][0]
            if isinstance(topic0, list):
                topic0 = POSITIONS_MERGED_TOPIC if event_kind == "merged" else topic0[0]
            log = merged_log(5_500_000) if topic0 == POSITIONS_MERGED_TOPIC else transfer_log(MODULE, OWNER, 5_500_000)
            return [{**log, "transactionHash": tx_merge, "blockNumber": "0x6e"}]
        raise AssertionError(method)

    report = capture_account(
        {"owner": OWNER, "btc_transaction_hashes": [tx_buy], "activity": [{
            "type": "TRADE", "side": "BUY", "transactionHash": tx_buy,
            "timestamp": 1000, "usdcSize": "5", "slug": "btc-updown-5m-123",
            "conditionId": CONDITION,
        }]},
        rpc_call=rpc, max_history_blocks=20, chunk_blocks=10,
    )
    assert report["settlement_receipt_rows"] == 1
    assert report["cash_release_receipt_rows"] == 1
    assert report["verified_canary_fills"] == 0


def test_wide_history_batches_event_and_transfer_filters():
    tx_buy = "0x" + "05" * 32
    calls = []

    def rpc(method, params):
        if method == "eth_getTransactionReceipt":
            return {"transactionHash": tx_buy, "status": "0x1", "blockNumber": "0x64", "logs": []}
        if method == "eth_blockNumber":
            return "0x78"
        if method == "eth_getLogs":
            calls.append(params[0])
            return []
        raise AssertionError(method)

    report = capture_account(
        {"owner": OWNER, "btc_transaction_hashes": [tx_buy], "activity": [{
            "type": "TRADE", "side": "BUY", "transactionHash": tx_buy,
            "timestamp": 1000, "usdcSize": "5", "slug": "btc-updown-5m-123",
            "conditionId": CONDITION,
        }]},
        rpc_call=rpc, max_history_blocks=20, chunk_blocks=10,
    )
    assert report["history_query_count"] == 4
    assert len(calls) == 4
    assert isinstance(calls[0]["topics"][0], list)
    assert isinstance(calls[1]["address"], list)
    assert isinstance(calls[2]["topics"][0], list)
    assert isinstance(calls[3]["address"], list)


def test_scalar_local_decode_uses_provider_compatible_filters_and_owner_caps_rows():
    tx_buy = "0x" + "06" * 32
    calls = []

    def rpc(method, params):
        if method == "eth_getTransactionReceipt":
            return {"transactionHash": tx_buy, "status": "0x1", "blockNumber": "0x64", "logs": []}
        if method == "eth_blockNumber":
            return "0x78"
        if method == "eth_getLogs":
            calls.append(params[0])
            return []
        raise AssertionError(method)

    report = capture_account(
        {"owner": OWNER, "btc_transaction_hashes": [tx_buy], "activity": [{
            "type": "TRADE", "side": "BUY", "transactionHash": tx_buy,
            "timestamp": 1000, "usdcSize": "5", "slug": "btc-updown-5m-123",
            "conditionId": CONDITION,
        }]},
        rpc_call=rpc, max_history_blocks=20, chunk_blocks=10,
        log_filter_mode="scalar_local_decode",
    )
    assert report["history_log_filter_mode"] == "scalar_local_decode"
    assert report["history_query_count"] == 18
    assert calls
    assert all(isinstance(item["address"], str) for item in calls)
    assert all(item["topics"] and isinstance(item["topics"][0], str) for item in calls)
    assert all(None not in item["topics"] for item in calls)


def test_seed_windows_mode_queries_only_post_seed_causal_ranges():
    tx_buy = "0x" + "07" * 32
    calls = []

    def rpc(method, params):
        if method == "eth_getTransactionReceipt":
            return {"transactionHash": tx_buy, "status": "0x1", "blockNumber": "0x64", "logs": []}
        if method == "eth_blockNumber":
            return "0x200"
        if method == "eth_getLogs":
            calls.append(params[0])
            return []
        raise AssertionError(method)

    report = capture_account(
        {"owner": OWNER, "btc_transaction_hashes": [tx_buy], "activity": [{
            "type": "TRADE", "side": "BUY", "transactionHash": tx_buy,
            "timestamp": 1000, "usdcSize": "5", "slug": "btc-updown-5m-123",
            "conditionId": CONDITION,
        }]},
        rpc_call=rpc, max_history_blocks=100, chunk_blocks=10,
        log_filter_mode="scalar_local_decode", history_mode="seed_windows",
        seed_window_before_blocks=0, seed_window_after_blocks=20,
    )
    assert report["history_mode"] == "seed_windows"
    assert report["history_block_from"] == 100
    assert report["history_block_to"] == 120
    assert report["history_range_count"] == 3
    assert all(100 <= int(item["fromBlock"], 16) <= int(item["toBlock"], 16) <= 120 for item in calls)


def test_adaptive_log_query_splits_provider_sensitive_or_filters():
    calls = []
    first = {
        "fromBlock": "0x64",
        "toBlock": "0x78",
        "address": [MODULE, V2_COLLATERAL_TOKEN],
        "topics": [[POSITION_REDEEMED_TOPIC, POSITIONS_MERGED_TOPIC], None, None, topic_address(OWNER)],
    }

    def rpc(method, params):
        assert method == "eth_getLogs"
        query = params[0]
        calls.append(query)
        if isinstance(query.get("address"), list) or isinstance(query["topics"][0], list):
            raise RuntimeError("history_rpc_error:http_400")
        return [{
            "transactionHash": "0x" + "09" * 32,
            "logIndex": "0x0",
            "blockNumber": "0x6e",
            "address": query["address"],
            "topics": [query["topics"][0]],
            "data": "0x",
        }]

    rows, splits = _adaptive_get_logs(rpc, first)
    assert splits > 0
    assert rows
    assert len(calls) > 1
    assert all(not isinstance(item.get("address"), list) for item in calls[-2:])
    assert all(not isinstance(item["topics"][0], list) for item in calls[-2:])


def test_adaptive_log_query_has_bounded_leaf_budget():
    query = {
        "fromBlock": "0x64",
        "toBlock": "0x78",
        "address": MODULE,
        "topics": [POSITION_REDEEMED_TOPIC, None, None, topic_address(OWNER)],
    }

    def rpc(method, params):
        raise RuntimeError("history_rpc_error:http_400")

    try:
        _adaptive_get_logs(rpc, query, max_leaves=1)
    except RuntimeError as exc:
        assert str(exc) == "history_rpc_error:http_400"
    else:
        raise AssertionError("provider failures must stop at the configured leaf budget")

