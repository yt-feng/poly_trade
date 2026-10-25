import pandas as pd

from analysis.h716_h30_max_edge_outlier_veto import MAX_EDGE_AFTER_FEE, chosen_edge_mask


def test_max_edge_veto_rejects_only_large_finite_edge():
    side = pd.Series(["buy_up", "buy_down", "skip", "buy_up"])
    edge = pd.Series([MAX_EDGE_AFTER_FEE, MAX_EDGE_AFTER_FEE + 1e-4, 0.9, float("nan")])
    assert chosen_edge_mask(side, edge).tolist() == [True, False, False, False]


def test_max_edge_veto_does_not_add_a_lower_edge_floor():
    side = pd.Series(["buy_up", "buy_down"])
    edge = pd.Series([-0.50, -0.01])
    assert chosen_edge_mask(side, edge).tolist() == [True, True]

