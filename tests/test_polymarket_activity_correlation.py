from analysis.polymarket_activity_correlation import correlate_activity


def trade():
    return {
        "id": "trade-1",
        "market_id": "market-1",
        "asset_id": "token-1",
        "owner": "0xAbC",
        "side": "BUY",
        "size": "5.00000000",
        "transaction_hash": "0xTx",
    }


def activity(**overrides):
    row = {
        "id": "activity-1",
        "type": "TRADE",
        "proxyWallet": "0xabc",
        "conditionId": "market-1",
        "asset": "token-1",
        "side": "BUY",
        "size": "5",
        "price": "0.50",
        "transactionHash": "0xtx",
        "timestamp": "1700000000",
    }
    row.update(overrides)
    return row


def test_matching_activity_is_only_corroboration():
    result = correlate_activity(activity(), trade=trade(), expected={"owner": "0xAbC"})
    assert result["matched"] is True
    assert result["cash_reusable"] is False
    assert result["observation"]["trade_id"] == "trade-1"


def test_public_row_without_owner_scope_is_rejected():
    row = activity()
    row.pop("proxyWallet")
    result = correlate_activity(row, trade=trade(), expected={"owner": "0xAbC"})
    assert result["matched"] is False
    assert "MISSING_OWNER_SCOPE" in {issue["code"] for issue in result["issues"]}


def test_pending_or_non_trade_activity_does_not_correlate():
    result = correlate_activity(activity(type="REDEEM"), trade=trade(), expected={"owner": "0xAbC"})
    assert result["matched"] is False
    assert "UNSUPPORTED_ACTIVITY_TYPE" in {issue["code"] for issue in result["issues"]}


def test_wrong_transaction_hash_is_rejected():
    result = correlate_activity(activity(transactionHash="0xother"), trade=trade(), expected={"owner": "0xAbC"})
    assert result["matched"] is False
    assert "TRANSACTION_HASH_MISMATCH" in {issue["code"] for issue in result["issues"]}


def test_quantity_mismatch_is_rejected():
    result = correlate_activity(activity(size="4.9"), trade=trade(), expected={"owner": "0xAbC"})
    assert result["matched"] is False
    assert "ACTIVITY_QUANTITY_MISMATCH" in {issue["code"] for issue in result["issues"]}
