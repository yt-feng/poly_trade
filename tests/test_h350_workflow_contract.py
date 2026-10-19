from pathlib import Path


WORKFLOW = Path(__file__).parents[1] / ".github" / "workflows" / "h350-share-floor-quantml-old-clean.yml"


def test_h350_is_frozen_share_floor_counterfactual_and_paper_only():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "bbda5f2050685f63153621a54641ebdfab943487" in text
    assert "--minimum-order-notional 0" in text
    assert "--protocol-variant conditional_share_floor_quantml" in text
    assert "live_orders_enabled': False" in text
    assert "strategy_evaluated': True" in text
    assert "live_orders_enabled': False" in text


def test_h348_default_protocol_remains_dollar_and_share_floor():
    text = (Path(__file__).parents[1] / "analysis" / "h348_fixed_notional_quantml_old_clean.py").read_text(encoding="utf-8")
    assert 'minimum_order_notional: float = DEFAULT_MINIMUM_ORDER' in text
    assert 'protocol_variant: str = "dollar_and_share_floor"' in text
