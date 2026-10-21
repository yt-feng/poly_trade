from analysis.h534_dense_cash_fraction_grid import DENSE_FRACTIONS


def test_h534_grid_is_pre_registered_with_intermediate_fractions():
    assert DENSE_FRACTIONS == (0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90, 0.95, 1.00)
