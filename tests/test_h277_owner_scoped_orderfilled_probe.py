from analysis.h277_owner_scoped_orderfilled_probe import normalize_owner, summarize_records


OWNER = "0x" + "11" * 20
OTHER = "0x" + "22" * 20


def test_summary_counts_taker_and_maker_without_exposing_owner_text():
    result = summarize_records(
        [
            {"event_id": "a", "taker": OWNER, "maker": OTHER, "tx_hash": "0xa"},
            {"event_id": "b", "taker": OTHER, "maker": OWNER, "tx_hash": "0xb"},
        ],
        [{"label": "key-a", "address": OWNER}],
    )[0]
    assert result["public_orderfilled_taker_events"] == 1
    assert result["public_orderfilled_maker_events"] == 1
    assert result["owner_scoped_account_receipt"] is False
    assert result["settlement_evidence"] is False
    assert OWNER not in result


def test_normalize_owner_rejects_non_address():
    try:
        normalize_owner("wallet-sample")
    except ValueError as exc:
        assert "20-byte" in str(exc)
    else:
        raise AssertionError("invalid owner accepted")


def test_summary_requires_bounded_owner_count():
    try:
        summarize_records([], [])
    except ValueError as exc:
        assert "one to four" in str(exc)
    else:
        raise AssertionError("empty owner set accepted")
