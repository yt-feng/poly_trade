import importlib.util
from pathlib import Path


MODULE = Path(__file__).parents[1] / "analysis" / "h388_pair_cost_public_wallet.py"
SPEC = importlib.util.spec_from_file_location("h388_pair_cost_public_wallet", MODULE)
MOD = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MOD)


def _market(start, price_up="0.48", price_down="0.48", redeem=True):
    rows = [
        {"slug": f"btc-updown-5m-{start}", "type": "TRADE", "side": "BUY", "outcome": "Up", "size": 5, "usdcSize": 5 * float(price_up), "price": float(price_up), "timestamp": start + 10},
        {"slug": f"btc-updown-5m-{start}", "type": "TRADE", "side": "BUY", "outcome": "Down", "size": 5, "usdcSize": 5 * float(price_down), "price": float(price_down), "timestamp": start + 11},
    ]
    if redeem:
        rows.append({"slug": f"btc-updown-5m-{start}", "type": "REDEEM", "usdcSize": 5, "timestamp": start + 366})
    return rows


def test_pair_cost_is_scaled_to_five_and_replayed_without_reset():
    events = [event for start in range(0, 3000, 300) for event in _market(start)]
    report = MOD.build_report(events, source_commit="abc")
    replay = report["replay_zero_fee_upper_bound"]
    assert replay["first_ten_rows"] == 10
    assert replay["ten_row_same_ledger_gate"] is True
    assert replay["ledger_reset_count"] == 0
    assert replay["concatenation_count"] == 0
    assert report["actual_fills"] == 0


def test_missing_redeem_is_excluded_from_cash_chain():
    report = MOD.build_report(_market(0, redeem=False), source_commit="abc")
    assert report["eligible_pair_markets"] == 0
    assert report["replay_zero_fee_upper_bound"]["first_ten_rows"] == 0


def test_fee_stress_can_fail_even_when_zero_fee_is_positive():
    events = _market(0, price_up="0.50", price_down="0.50")
    report = MOD.build_report(events, source_commit="abc")
    assert report["replay_zero_fee_upper_bound"]["growth_positive"] is False
    assert report["replay_taker_fee_stress"]["growth_positive"] is False
