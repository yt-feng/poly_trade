from analysis.authoritative_orderfilled_quote_join import join_orderfilled_quotes


def fill(event_id="0xabc:1", ts=10_000, token="101", market="m1"):
    return {"event_id": event_id, "timestamp_ms": ts, "token_id": token, "market_id": market}


def quote(quote_id, ts, token="101", market="m1"):
    return {"quote_id": quote_id, "timestamp_ms": ts, "token_id": token, "market_id": market, "mid_up_cents": 40}


def test_chooses_latest_causal_quote_and_never_uses_future_quote():
    result = join_orderfilled_quotes(
        [fill()],
        [quote("old", 7_000), quote("latest", 9_500), quote("future", 10_100)],
        tolerance_ms=2_500,
    )
    assert result["joined_count"] == 1
    assert result["joined"][0]["quote"]["quote_id"] == "latest"
    assert result["joined"][0]["quote_age_ms"] == 500
    assert result["future_quote_leakage_allowed"] is False


def test_rejects_market_or_token_mismatch_and_stale_quote():
    result = join_orderfilled_quotes(
        [fill(), fill("0xabc:2", token="202"), fill("0xabc:3", market="m2")],
        [quote("wrong-token", 9_900, token="303"), quote("wrong-market", 9_900, market="m3"), quote("stale", 1_000)],
        tolerance_ms=500,
    )
    assert result["joined_count"] == 0
    assert result["unmatched_count"] == 3


def test_duplicate_fill_is_idempotent_but_conflict_fails_closed():
    item = fill()
    result = join_orderfilled_quotes([item, dict(item)], [quote("q", 9_900)])
    assert result["fill_count"] == 1
    assert result["joined_count"] == 1
    conflict = dict(item, price="0.4")
    try:
        join_orderfilled_quotes([item, conflict], [quote("q", 9_900)])
    except ValueError as exc:
        assert str(exc) == "conflicting_duplicate_fill"
    else:
        raise AssertionError("conflicting duplicate must fail closed")


def test_missing_fill_timestamp_is_reported_not_dropped():
    result = join_orderfilled_quotes([dict(fill(), timestamp_ms=None)], [quote("q", 9_900)])
    assert result["fill_count"] == 1
    assert result["joined_count"] == 0
    assert result["unmatched"][0]["reason"] == "fill_timestamp_missing_or_invalid"

