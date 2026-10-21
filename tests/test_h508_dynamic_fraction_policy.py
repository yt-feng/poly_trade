from analysis.h508_dynamic_fraction_policy import select_gross_cash_fraction


def test_policy_matches_conservative_h508_cells():
    assert select_gross_cash_fraction(0.10, 8.0, 0.01) == 0.50
    assert select_gross_cash_fraction(0.15, 8.0, 0.01) == 0.75
    assert select_gross_cash_fraction(0.20, 2.0, 0.01) == 1.00
    assert select_gross_cash_fraction(0.20, 4.0, 0.01) == 0.95
    assert select_gross_cash_fraction(0.20, 8.0, 0.01) == 0.90
    assert select_gross_cash_fraction(0.25, 8.0, 0.01) == 1.00


def test_policy_vetoes_unseen_execution_envelope():
    assert select_gross_cash_fraction(0.09, 2.0, 0.01) is None
    assert select_gross_cash_fraction(0.25, 8.01, 0.01) is None
    assert select_gross_cash_fraction(0.25, 8.0, 0.0101) is None


def test_policy_rejects_invalid_observations():
    for kwargs in (
        {"effective_depth_haircut": 0.0, "latency_seconds": 1.0, "slippage": 0.01},
        {"effective_depth_haircut": 0.25, "latency_seconds": -1.0, "slippage": 0.01},
        {"effective_depth_haircut": 0.25, "latency_seconds": 1.0, "slippage": -0.01},
    ):
        try:
            select_gross_cash_fraction(**kwargs)
        except ValueError:
            pass
        else:
            raise AssertionError("invalid observation must fail closed")
