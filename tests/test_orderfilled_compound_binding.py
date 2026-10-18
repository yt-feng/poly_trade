from analysis.orderfilled_compound_binding import audit_orderfilled_bindings


def fill(i: int, *, tx: str | None = None, token: str = "123", market: str = "m1"):
    return {
        "event_id": f"0x{i}:1",
        "tx_hash": tx or f"0x{i}",
        "market_id": market,
        "taker": "0xtaker",
        "side": "BUY",
        "sign": 1,
        "size": "5",
        "cash_amount": "2.5",
        "price": "0.5",
        "token_id": token,
    }


def row(i: int, **updates):
    value = {
        "sequence_no": i,
        "orderfilled_event_id": f"0x{i}:1",
        "transaction_ref": f"0x{i}",
        "market_id": "m1",
        "execution_role": "TAKER",
        "shares": "5",
        "price": "0.5",
        "entry_cost": "2.5",
    }
    value.update(updates)
    return value


def bundle():
    return [row(i) for i in range(1, 11)], [fill(i) for i in range(1, 11)]


def test_binds_each_ledger_row_to_distinct_orderfilled_event_and_owner():
    rows, fills = bundle()
    result = audit_orderfilled_bindings(rows, fills, owner_address="0xtaker")
    assert result["orderfilled_binding_ready"] is True
    assert result["owner_scope_verified"] is True
    assert result["matched_rows"] == 10
    assert result["issues"] == []


def test_matched_transaction_without_orderfilled_event_fails_closed():
    rows, fills = bundle()
    rows[3]["orderfilled_event_id"] = ""
    result = audit_orderfilled_bindings(rows, fills, owner_address="0xtaker")
    assert result["orderfilled_binding_ready"] is False
    assert any(item["reason"] == "orderfilled_event_id_required_per_row" for item in result["issues"])


def test_duplicate_event_or_wrong_owner_is_not_a_ten_fill_chain():
    rows, fills = bundle()
    rows[1]["orderfilled_event_id"] = rows[0]["orderfilled_event_id"]
    result = audit_orderfilled_bindings(rows, fills, owner_address="0xother")
    assert result["orderfilled_binding_ready"] is False
    reasons = {item["reason"] for item in result["issues"]}
    assert "orderfilled_event_reused" in reasons
    assert "taker_owner_mismatch" in reasons


def test_without_owner_address_binding_is_not_account_scoped():
    rows, fills = bundle()
    result = audit_orderfilled_bindings(rows, fills)
    assert result["matched_rows"] == 10
    assert result["orderfilled_binding_ready"] is False
    assert result["owner_scope_verified"] is False
    assert "owner_address_required_for_owner_scope" in {item["reason"] for item in result["issues"]}


def test_quote_or_account_values_must_match_onchain_fill():
    rows, fills = bundle()
    rows[5]["price"] = "0.51"
    result = audit_orderfilled_bindings(rows, fills, owner_address="0xtaker")
    assert result["matched_rows"] == 9
    assert any(item["reason"] == "execution_price_mismatch" for item in result["issues"])
