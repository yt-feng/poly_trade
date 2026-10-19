from analysis.onchain_orderfilled_normalizer import adapt_rocklabs_orderfilled, normalize_orderfilled


def rocklabs(log_index=3):
    return {
        "message_type": "onchain.OrderFilled",
        "content": {
            "block_number": 83558320,
            "tx_hash": "0x" + "aa" * 32,
            "chain_id": 137,
            "event_name": "OrderFilled",
            "ts_block": 1772236671,
            "decoded": {
                "logIndex": log_index,
                "orderHash": "0x" + "bb" * 32,
                "maker": "0x" + "11" * 20,
                "taker": "0x" + "22" * 20,
                "makerAssetId": "0",
                "takerAssetId": "8629673",
                "makerAmountFilled": "154000",
                "takerAmountFilled": "350000",
                "fee": "35000",
            },
        },
    }


def test_rocklabs_adapter_normalizes_nested_v1_sell():
    raw = adapt_rocklabs_orderfilled(rocklabs())
    normalized = normalize_orderfilled(raw, exchange_version="v1")
    assert normalized["event_id"].endswith(":3")
    assert normalized["side"] == "SELL"
    assert normalized["maker"] == "0x" + "11" * 20
    assert normalized["tx_hash"] == "0x" + "aa" * 32


def test_rocklabs_adapter_rejects_missing_log_index_instead_of_inventing_identity():
    record = rocklabs()
    del record["content"]["decoded"]["logIndex"]
    try:
        adapt_rocklabs_orderfilled(record)
    except ValueError as exc:
        assert str(exc) == "rocklabs_field_required:log_index"
    else:
        raise AssertionError("missing log index must fail closed")
