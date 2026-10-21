import polars as pl

from analysis.h489_pmdata_bilateral_preflow_pair_replay import _run_market


def book(ts=0, slug="m"):
    return {
        "market_slug": slug, "timestamp": ts, "local_timestamp": ts, "event_type": "book",
        "bid_prices": [0.49], "bid_sizes": [20.0], "ask_prices": [0.51], "ask_sizes": [20.0],
        "pc_side": None, "pc_price": None, "pc_size": None,
    }


def trade(ts, outcome, slug="m"):
    return {
        "market_slug": slug, "timestamp": ts, "local_timestamp": ts, "outcome": outcome,
        "side": "SELL", "price": 0.49, "size": 100.0,
    }


def test_bilateral_preflow_is_causal_and_rejects_one_sided_history():
    l2 = pl.DataFrame([book()])
    only_yes = pl.DataFrame([trade(-1, "yes")])
    result = _run_market(l2, only_yes)
    assert result["status"] == "no_fill"
    assert result["preflow_rejected"] == 1

    both = pl.DataFrame([trade(-1, "yes"), trade(-1, "no"), trade(1, "yes"), trade(2, "no")])
    result = _run_market(l2, both)
    assert result["status"] == "filled"
