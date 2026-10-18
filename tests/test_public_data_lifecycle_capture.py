import pytest

from analysis.public_data_lifecycle_capture import capture


def test_capture_keeps_public_activity_separate_and_bounded():
    calls = []

    def fake_fetch(url):
        calls.append(url)
        if "/positions?" in url:
            return [{"conditionId": "c1"}]
        return [{"type": "REDEEM"}, {"type": "TRADE"}]

    result = capture("0x" + "1" * 40, fetcher=fake_fetch)
    assert result["positions_count"] == 1
    assert result["activity_count"] == 2
    assert result["positions_truncated"] is False
    assert result["activity_truncated"] is False
    assert len(calls) == 2
    assert "user=0x" + "1" * 40 in calls[0]
    assert "authenticated" in result["evidence_boundary"]


def test_invalid_wallet_fails_before_fetch():
    with pytest.raises(ValueError, match="wallet_address"):
        capture("not-an-address", fetcher=lambda _url: [])


def test_non_list_response_fails_closed():
    with pytest.raises(ValueError, match="response_must_be_list"):
        capture("0x" + "2" * 40, fetcher=lambda _url: {})
