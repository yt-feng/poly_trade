from analysis.h542_adaptive_cash_boost import (
    BASE_FRACTION,
    BOOST_AFTER_PROFITS,
    BOOST_FRACTION,
    _fraction_for_state,
)


def test_h542_starts_at_h534_fraction():
    assert _fraction_for_state(0) == BASE_FRACTION
    assert _fraction_for_state(BOOST_AFTER_PROFITS - 1) == BASE_FRACTION


def test_h542_boosts_only_after_two_prior_profits():
    assert _fraction_for_state(BOOST_AFTER_PROFITS) == BOOST_FRACTION
    assert _fraction_for_state(BOOST_AFTER_PROFITS + 3) == BOOST_FRACTION


def test_h542_state_is_reset_by_caller_after_nonprofit():
    # The policy is intentionally a pure state transition; a non-profit exit
    # must be represented by the caller as zero consecutive profits.
    assert _fraction_for_state(0) == BASE_FRACTION


def test_h542_rejects_invalid_parameters():
    import pytest

    with pytest.raises(ValueError):
        _fraction_for_state(0, base_fraction=0.0)
    with pytest.raises(ValueError):
        _fraction_for_state(0, boost_fraction=1.1)
    with pytest.raises(ValueError):
        _fraction_for_state(0, boost_after_profits=0)
