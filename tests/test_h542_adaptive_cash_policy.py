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


def test_h544_intermediate_fraction_is_online_and_bounded():
    assert _fraction_for_state(0, base_fraction=0.55, boost_fraction=0.575) == 0.55
    assert _fraction_for_state(2, base_fraction=0.55, boost_fraction=0.575) == 0.575


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


def test_h542_workflow_does_not_export_recipient_material_as_step_env():
    from pathlib import Path

    workflow = Path(__file__).parents[1] / ".github" / "workflows" / "h542-adaptive-cash-boost.yml"
    text = workflow.read_text(encoding="utf-8")
    assert "QUALITY_FIRST_RECIPIENT_JSON_B64: ${{ vars." not in text
    assert "QUALITY_FIRST_RECIPIENT_ID: ${{ vars." not in text
    assert "gh', 'variable', 'get'" not in text
    assert "research_vault/recipient.json" in text


def test_h544_workflow_uses_committed_recipient_without_variable_permission():
    from pathlib import Path

    workflow = Path(__file__).parents[1] / ".github" / "workflows" / "h544-intermediate-cash-boost.yml"
    text = workflow.read_text(encoding="utf-8")
    assert "gh', 'variable', 'get'" not in text
    assert "research_vault/recipient.json" in text


def test_parameterized_adaptive_workflow_exposes_registered_policy_inputs():
    from pathlib import Path

    workflow = Path(__file__).parents[1] / ".github" / "workflows" / "h544-intermediate-cash-boost.yml"
    text = workflow.read_text(encoding="utf-8")
    for input_name in (
        "hypothesis_id",
        "result_format",
        "base_fraction",
        "boost_fraction",
        "boost_after_profitable_exits",
    ):
        assert f"      {input_name}:" in text
    assert "--hypothesis-id \"$HYPOTHESIS_ID\"" in text
    assert "--boost-after-profits \"$BOOST_AFTER_PROFITS\"" in text
    assert "'hypothesis_id': os.environ['HYPOTHESIS_ID']" in text
