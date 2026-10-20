import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "analysis"))

from h389_pair_cost_contract_gate import build_report, pair_contract_budget


def event(ts, outcome, *, size, price, slug="btc-updown-5m-1000", kind="TRADE"):
    return {
        "timestamp": ts,
        "type": kind,
        "side": "BUY" if kind == "TRADE" else "",
        "outcome": outcome,
        "size": str(size),
        "price": str(price),
        "usdcSize": str(size * price),
        "slug": slug,
        "transactionHash": f"tx-{ts}-{outcome}",
    }


def test_per_leg_floor_is_not_h388_total_pair_floor():
    budget = pair_contract_budget("0.5", "0.5")
    assert budget["matched_shares"] == 10
    assert budget["up_cost"] == 5
    assert budget["down_cost"] == 5
    assert budget["pair_cost_maker"] == 10
    assert budget["pair_fee_taker"] > 0


def test_online_pair_waits_until_both_legs_meet_current_contract():
    events = [
        event(1, "Up", size=10, price=0.5),
        event(2, "Down", size=10, price=0.5),
        event(100, "", size=0, price=0, kind="REDEEM"),
    ]
    report = build_report(events, source_commit="pinned")
    assert report["maker_replay"]["accepted_rows"] == 1
    assert report["taker_replay"]["accepted_rows"] == 0
    assert report["actual_fills"] == 0
    assert report["verified_canary_fills"] == 0


def test_low_price_leg_raises_required_matched_size_and_budget():
    budget = pair_contract_budget("0.2", "0.3")
    assert budget["matched_shares"] == 25
    assert budget["pair_cost_maker"] == 12.5
