import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "analysis"))
import h717_owner_orderbook_join as h717


def _h279():
    return {
        "accounts": [{
            "label": "paper",
            "account_trades": [{
                "condition_id": "0x" + "a" * 64,
                "asset_id": "123",
                "transaction_hash": "0x" + "b" * 64,
                "matched_at": "2026-09-03T12:00:01Z",
                "price": "0.50",
                "size": "5",
                "side": "BUY",
                "fee_rate_bps": "200",
            }],
            "public_data_activity": [{
                "conditionId": "0x" + "a" * 64,
                "asset": "123",
                "transactionHash": "0x" + "b" * 64,
                "slug": "btc-updown-5m-1788436800",
            }],
        }]
    }


def test_normalize_trades_requires_explicit_btc_slug():
    rows = h717.normalize_trades(_h279())
    assert len(rows) == 1
    assert rows[0]["side"] == "buy"
    assert rows[0]["trade_size"] == 5.0
    assert rows[0]["matched_at_us"] == 1788436801000000


def test_ladder_cover_is_conservative():
    result = h717._ladder_cover([(0.50, 3), (0.51, 3)], asks=True, trade_price=0.51, trade_size=5)
    assert result["touch_price"] == 0.50
    assert result["ladder_depth_to_trade_price"] == 6
    assert result["displayed_size_covers_trade"] is True


def test_align_redacts_no_owner_address_and_keeps_paper_boundary(monkeypatch):
    def fake_query(_url, _trades):
        return [{
            "market": "0x" + "a" * 64,
            "asset_id": "123",
            "event_type": "book",
            "event_us": 1788436800000000,
            "received_us": 1788436800000000,
            "bids": "[[0.49, 20]]",
            "asks": "[[0.50, 10]]",
        }]

    monkeypatch.setattr(h717, "_query_hour", fake_query)
    result = h717.align(_h279(), base_url="https://example.invalid")
    assert result["counts"]["book_snapshot_matched"] == 1
    assert result["counts"]["paper_fill_admitted"] == 1
    assert result["actual_fills"] == 0
    assert result["live_orders_enabled"] is False
    assert "private_key" not in result
    assert "owner_address" not in result["matches"][0]


def test_missing_book_is_not_admitted(monkeypatch):
    monkeypatch.setattr(h717, "_query_hour", lambda _url, _trades: [])
    result = h717.align(_h279(), base_url="https://example.invalid")
    assert result["counts"]["book_snapshot_matched"] == 0
    assert result["counts"]["paper_fill_admitted"] == 0
    assert result["matches"][0]["missing_reason"] == "no_prior_book_snapshot"
