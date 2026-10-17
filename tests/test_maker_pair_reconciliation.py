import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "analysis"))

from maker_pair_reconciliation import reconcile_maker_pair


def trade(asset, trade_id, *, price, size="20", owner="acct", market="m1", side="MAKER", fee="0", status="TRADE_STATUS_CONFIRMED", match="100"):
    return {
        "id": trade_id,
        "market": market,
        "asset_id": asset,
        "owner": owner,
        "status": status,
        "trader_side": side,
        "fee_rate_bps": fee,
        "size": str(size),
        "price": str(price),
        "match_time": match,
        "transaction_hash": f"0x{trade_id}",
    }


def test_complementary_maker_pair_stays_unreleased_until_checkpoint():
    result = reconcile_maker_pair(
        [trade("up", "u", price="0.25"), trade("down", "d", price="0.40")],
        expected_owner="acct", expected_market_id="m1", up_asset_id="up", down_asset_id="down",
    )
    assert result["issues"] == []
    assert result["pair_status"] == "settlement_candidate"
    assert result["pair_edge"] == "7.00"
    assert result["cash_reusable"] is False
    assert result["settlement_checkpoint_required"] is True


def test_pair_rejects_taker_or_nonzero_fee():
    result = reconcile_maker_pair(
        [trade("up", "u", price="0.40", side="TAKER"), trade("down", "d", price="0.50", fee="1")],
        expected_owner="acct", expected_market_id="m1", up_asset_id="up", down_asset_id="down",
    )
    assert "PAIR_TRADE_NOT_MAKER" in result["issues"]
    assert "PAIR_MAKER_FEE_NOT_ZERO" in result["issues"]


def test_pair_rejects_mismatched_market_and_nonpositive_edge():
    bad_market = reconcile_maker_pair(
        [trade("up", "u", price="0.60"), trade("down", "d", price="0.50", market="m2")],
        expected_owner="acct", expected_market_id="m1", up_asset_id="up", down_asset_id="down",
    )
    assert "PAIR_MARKET_MISMATCH" in bad_market["issues"]
    no_edge = reconcile_maker_pair(
        [trade("up", "u", price="0.50"), trade("down", "d", price="0.50")],
        expected_owner="acct", expected_market_id="m1", up_asset_id="up", down_asset_id="down",
    )
    assert no_edge["pair_status"] == "rejected"
    assert no_edge["issues"] == ["PAIR_NON_POSITIVE_EDGE"]
