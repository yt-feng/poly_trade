from analysis.h534_dense_cash_fraction_grid import DENSE_FRACTIONS, _select_largest_survivor


def test_h534_grid_is_pre_registered_with_intermediate_fractions():
    assert DENSE_FRACTIONS == (0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90, 0.95, 1.00)


def test_h534_selection_chooses_largest_surviving_fraction():
    rows = []
    for fraction, passed in ((0.50, True), (0.55, True), (0.60, False)):
        for window_index in range(1, 18):
            rows.append({
                "gross_cash_fraction": fraction,
                "window_index": window_index,
                "first_ten_passed": passed,
                "first_ten_ending_cash": 11.0 + fraction,
                "first_ten_fills": 10 if passed else 9,
            })
    selected, policy = _select_largest_survivor(rows)
    assert selected == 0.55
    assert policy["selection_all_ten"] is True
    assert policy["holdout_all_ten"] is True
