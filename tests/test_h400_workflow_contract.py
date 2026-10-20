from pathlib import Path


WORKFLOW = Path(__file__).parents[1] / ".github" / "workflows" / "h400-polyorderbooks-capacity-replay.yml"


def test_h400_is_a_bounded_capacity_replay_with_two_predeclared_caps():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "H400-fixed-l2-budget-capacity" in text
    assert '--fractions "0.5,1.0"' in text
    assert "no order, wallet sample, settlement receipt or Canary row" in text
    assert "new_market_data_collected = False" not in text
    assert "report['new_market_data_collected'] = False" in text


def test_h400_publishes_only_sealed_delivery():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "research_vault.seal" in text
    assert "publish_quality_first_journal.py" in text
    assert "plaintext_published': False" in text
    assert "live_orders_enabled': False" in text
